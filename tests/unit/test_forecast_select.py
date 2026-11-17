from __future__ import annotations

import re
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.forecast_dispatch import (  # noqa: E402
    dispatch_envelope,
    dispatch_published,
    post_repository_dispatch,
)
from scripts.forecast_job import (  # noqa: E402
    ForecastJob,
    load_targets,
    native_timeout_s,
    repository_parts,
    timeout_minutes,
    verify_manifest,
    write_manifest,
)
from scripts.forecast_select import drop_published, select_runs  # noqa: E402

from av_wrf.contracts import format_iso_utc, parse_iso_utc  # noqa: E402
from av_wrf.core import is_cycle_live_fresh  # noqa: E402
from av_wrf.runner import load_bundle, published_cycle_prefix  # noqa: E402

_OPS = ROOT / "config" / "operations" / "forecasts.yaml"
_WORKFLOWS = ROOT / ".github" / "workflows"
_CRON_RE = re.compile(r"cron:\s*['\"]([^'\"]+)['\"]")
_IMAGE_PATHS = (
    "docker/**",
    "src/av_wrf/**",
    "scripts/**",
    "config/**",
    "pyproject.toml",
    "uv.lock",
    ".dockerignore",
    ".github/workflows/images.yml",
)


def _product_menu(workflow: str) -> tuple[str, tuple[str, ...]]:
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "product_id:")
    options: list[str] = []
    default = ""
    in_options = False
    for line in lines[start + 1 :]:
        if line.startswith("      ") and not line.startswith("        "):
            break
        stripped = line.strip()
        if stripped == "options:":
            in_options = True
            continue
        if in_options and stripped.startswith("- "):
            options.append(stripped[2:].strip().strip("\"'"))
            continue
        in_options = False
        if stripped.startswith("default:"):
            default = stripped.split(":", 1)[1].strip().strip("\"'")
    return default, tuple(options)


def _push_paths(workflow: str) -> tuple[str, ...]:
    push = workflow.split("\njobs:", 1)[0].split("\n  workflow_dispatch:", 1)[0]
    found = [
        line.strip()[2:].strip("\"'") for line in push.splitlines() if line.strip().startswith("- ")
    ]
    return tuple(found)


def _crons(path: Path) -> set[str]:
    head = path.read_text(encoding="utf-8").split("\njobs:", 1)[0]
    return set(_CRON_RE.findall(head))


def _trigger(cron: str) -> datetime:
    minute_s, hour_s, dom, month, dow = cron.split()
    if [dom, month, dow] != ["*", "*", "*"]:
        raise AssertionError(cron)
    return datetime(2026, 6, 2, int(hour_s), int(minute_s), tzinfo=UTC)


class TestForecastSelect(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_targets(_OPS)
        self.target = self.targets[0]
        self.bundle = load_bundle(ROOT / "config", self.target.product_id)
        self.cron = self.target.schedule_cron_utc
        self.trigger = _trigger(self.cron)

    def test_timeout_is_one_and_a_half_estimate(self) -> None:
        self.assertEqual(timeout_minutes(5), 8)
        budget = timeout_minutes(self.target.estimate_minutes)
        self.assertEqual(budget, (self.target.estimate_minutes * 3 + 1) // 2)
        self.assertEqual(native_timeout_s(budget), (budget - 15) * 60)
        with self.assertRaises(ValueError):
            native_timeout_s(15)

    def test_schedule_snap_lateness_and_explicit_cycle(self) -> None:
        on_time = self._select("schedule", self.trigger, cron=self.cron)
        origin = parse_iso_utc(on_time.cycle_iso)
        self.assertEqual(origin.hour, self.bundle.product.publish_origin_hour_utc)
        self.assertTrue(on_time.cycle_partition.endswith(f"/{origin.hour:02d}"))
        late_ok = self.trigger + timedelta(hours=3)
        self.assertEqual(
            self._select("schedule", late_ok, cron=self.cron).cycle_iso, on_time.cycle_iso
        )
        with self.assertRaises(ValueError):
            self._select("schedule", late_ok + timedelta(minutes=1), cron=self.cron)
        explicit_at = self.trigger + timedelta(hours=5)
        age_h = (explicit_at - origin).total_seconds() / 3600
        self.assertLess(age_h, self.bundle.product.max_cycle_age_hours)
        self.assertTrue(
            is_cycle_live_fresh(
                self.bundle.product,
                self.bundle.output,
                on_time.cycle_iso,
                format_iso_utc(explicit_at),
            )
        )
        explicit = self._select(
            "dispatch",
            explicit_at,
            product_id=self.target.product_id,
            cycle=on_time.cycle_iso,
        )
        self.assertEqual(explicit.cycle_iso, on_time.cycle_iso)
        on_demand = self._select("dispatch", explicit_at, product_id=self.target.product_id)
        self.assertEqual(on_demand.cycle_iso, on_time.cycle_iso)

    def test_each_operation_owns_its_schedule(self) -> None:
        other = "0 0 * * *"
        self.assertNotEqual(other, self.cron)
        text = _OPS.read_text(encoding="utf-8").replace(self.cron, other, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecasts.yaml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                select_runs(
                    ROOT / "config",
                    path,
                    mode="schedule",
                    now=self.trigger,
                    run_id="run-1",
                    cron=self.cron,
                )
            shifted = select_runs(
                ROOT / "config",
                path,
                mode="schedule",
                now=_trigger(other),
                run_id="run-1",
                cron=other,
            )
        self.assertEqual(len(shifted), 1)
        self.assertEqual(shifted[0].product_id, self.target.product_id)

    def test_unknown_dispatch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            select_runs(
                ROOT / "config",
                _OPS,
                mode="dispatch",
                now=self.trigger,
                run_id="run-1",
                product_id="not-a-configured-product",
            )

    def test_solver_label_uses_the_run_spot_flag(self) -> None:
        job = self._select("schedule", self.trigger, cron=self.cron)
        self.assertIn("runs-on=run-1/", job.solver_label)
        self.assertIn(f"spot={'true' if self.target.spot else 'false'}", job.solver_label)
        self.assertIn("image=ubuntu24-full-arm64", job.solver_label)
        self.assertIn("extras=s3-cache+ecr-cache", job.solver_label)
        self.assertIn("+".join(self.target.families), job.solver_label)
        self.assertEqual(job.timeout_minutes, timeout_minutes(self.target.estimate_minutes))

    def test_manifest_rejects_a_short_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("hi", encoding="utf-8")
            nested = root / "sub"
            nested.mkdir()
            (nested / "b.txt").write_text("yo", encoding="utf-8")
            write_manifest(root)
            verify_manifest(root)
            (root / "a.txt").write_text("hix", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_manifest(root)

    def test_workflow_owns_enabled_crons_once(self) -> None:
        enabled = {target.schedule_cron_utc for target in self.targets}
        forecast = _WORKFLOWS / "forecast.yml"
        text = forecast.read_text(encoding="utf-8")
        self.assertEqual(_crons(forecast), enabled)
        self.assertFalse((_WORKFLOWS / "daily-forecast.yml").exists())
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("workflow_dispatch:", text)
        default, options = _product_menu(text)
        ids = tuple(target.product_id for target in self.targets)
        self.assertEqual(options, ids)
        self.assertEqual(default, ids[0])
        mirror = text.split("\n  prepare:", 1)[0]
        self.assertIn("docker manifest inspect", mirror)
        self.assertLess(
            mirror.index("docker manifest inspect"),
            mirror.index('docker pull "$src"'),
        )
        self.assertIn("workflows/images.yml", mirror)
        self.assertIn('"${api}/dispatches"', mirror)
        self.assertIn("group: forecast", text)
        self.assertLess(text.index("runs-on/action@"), text.index("actions/cache"))
        self.assertIn("extras=s3-cache+ecr-cache", text)
        self.assertIn("scripts.forecast_dispatch", text)
        self.assertNotIn("AV_WRF_WEBHOOK", text)
        notify = (_WORKFLOWS / "notify.yml").read_text(encoding="utf-8")
        self.assertIn("scripts.forecast_dispatch", notify)
        self.assertNotIn("AV_WRF_WEBHOOK", notify)
        for cron in enabled:
            owners = [path.name for path in _WORKFLOWS.glob("*.yml") if cron in _crons(path)]
            self.assertEqual(owners, ["forecast.yml"])
        images = (_WORKFLOWS / "images.yml").read_text(encoding="utf-8")
        self.assertNotIn("pull_request:", images)
        self.assertIn("if: github.ref == 'refs/heads/main'", images)
        self.assertEqual(_push_paths(images), _IMAGE_PATHS)
        self.assertIn("runner=2cpu-linux-arm64/ram=8", images)
        self.assertIn("JOBS=2", images)
        self.assertNotIn("cpu=8", images)
        self.assertIn("push: false", images)
        self.assertIn("docker push", images)
        action_at = images.index("runs-on/action@")
        buildx_at = images.index("docker/setup-buildx-action@")
        self.assertLess(action_at, buildx_at)

    def test_pending_filters_a_durable_receipt(self) -> None:
        job = self._select("schedule", self.trigger, cron=self.cron)
        run: dict[str, object] = {
            "product_id": job.product_id,
            "cycle_iso": job.cycle_iso,
            "cycle_partition": job.cycle_partition,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending = drop_published(ROOT / "config", root.as_uri(), [run])
            self.assertEqual(pending, [run])
            prefix = published_cycle_prefix(self.bundle.product.model_id, job.cycle_partition)
            ready = root / prefix
            ready.mkdir(parents=True)
            (ready / "_ready.json").write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                drop_published(ROOT / "config", root.as_uri(), [run])

    def test_dispatch_envelope_uses_the_allowlist(self) -> None:
        job = self._select("schedule", self.trigger, cron=self.cron)
        target = next(item for item in self.targets if item.product_id == job.product_id)
        self.assertEqual(job.consumer_repository, target.consumer_repository)
        owner, name = repository_parts(target.consumer_repository)
        self.assertEqual(f"{owner}/{name}", target.consumer_repository)
        envelope = dispatch_envelope(
            target.consumer_environment, target.consumer_model, job.cycle_iso
        )
        payload = envelope["client_payload"]
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertEqual(envelope["event_type"], "upstream-cycle-ready")
        self.assertEqual(set(payload), {"environment", "model", "cycle"})
        self.assertEqual(payload["cycle"], job.cycle_iso)
        with self.assertRaises(ValueError):
            dispatch_envelope(
                target.consumer_environment, target.consumer_model, "2026-06-03T00:30:00Z"
            )
        with self.assertRaises(ValueError):
            repository_parts("https://example.invalid/owner/name")

    def test_repository_dispatch_posts_204_and_hides_the_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]
            return httpx.Response(204)

        envelope = dispatch_envelope("staging", "model", "2026-06-03T00:00:00Z")
        client = httpx.Client(transport=httpx.MockTransport(handler))
        status = post_repository_dispatch(
            "token-value", "example/consumer", envelope, client=client
        )
        self.assertEqual(status, 204)
        self.assertEqual(seen["url"], "https://api.github.com/repos/example/consumer/dispatches")
        self.assertEqual(seen["auth"], "Bearer token-value")

        def fail(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("token-value")

        failed = httpx.Client(transport=httpx.MockTransport(fail))
        with self.assertRaises(RuntimeError) as caught:
            post_repository_dispatch("token-value", "example/consumer", envelope, client=failed)
        self.assertNotIn("token-value", str(caught.exception))

    def test_missing_receipt_does_not_post(self) -> None:
        job = self._select("schedule", self.trigger, cron=self.cron)

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(str(request.url))

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            dispatch_published(
                ROOT / "config",
                _OPS,
                product_id=job.product_id,
                cycle=job.cycle_iso,
                attempt=job.attempt,
                store_uri=Path(tmp).as_uri(),
                token="token-value",
                client=client,
            )

    def _select(
        self,
        mode: str,
        now: datetime,
        *,
        cron: str = "",
        product_id: str = "",
        cycle: str = "",
    ) -> ForecastJob:
        jobs = select_runs(
            ROOT / "config",
            _OPS,
            mode=mode,
            now=now,
            run_id="run-1",
            cron=cron,
            product_id=product_id,
            cycle=cycle,
        )
        self.assertEqual(len(jobs), 1)
        return jobs[0]
