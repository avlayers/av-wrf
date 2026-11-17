"""Solver families stay on the ARM image's Graviton generations."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.forecast_dispatch import dispatch_published  # noqa: E402
from scripts.forecast_job import _families, load_targets, solver_label  # noqa: E402
from scripts.forecast_select import select_runs  # noqa: E402

from av_wrf.contracts import parse_iso_utc  # noqa: E402
from av_wrf.runner import load_bundle  # noqa: E402

_OPS = ROOT / "config" / "operations" / "forecasts.yaml"
_SOLVE_FAMILIES = (
    "c8g.8xlarge",
    "c7g.8xlarge",
    "m7g.8xlarge",
    "c9g.8xlarge",
    "m8g.8xlarge",
    "m9g.8xlarge",
    "r7g.8xlarge",
    "r8g.8xlarge",
)


class TestSolverFamilies(unittest.TestCase):
    def test_allowlist_is_graviton3_or_newer_8xlarge(self) -> None:
        for target in load_targets(_OPS):
            self.assertEqual(target.families, _SOLVE_FAMILIES)
            self.assertEqual(target.vcpus, 32)
            self.assertTrue(target.spot)
            label = solver_label("1" * 12, target)
            self.assertLess(len(label), 256)
            self.assertIn("/cpu=32/image=ubuntu24-full-arm64/", label)
            self.assertIn("spot=true", label)

    def test_manual_product_is_dispatch_only(self) -> None:
        manual = [item for item in load_targets(_OPS) if not item.scheduled]
        self.assertEqual(len(manual), 1)
        target = manual[0]
        workflow = (ROOT / ".github/workflows/forecast.yml").read_text(encoding="utf-8")
        self.assertNotIn(target.schedule_cron_utc, workflow.split("\njobs:", 1)[0])
        minute_s, hour_s, *_rest = target.schedule_cron_utc.split()
        now = datetime(2026, 6, 2, int(hour_s), int(minute_s), tzinfo=UTC)
        jobs = select_runs(
            ROOT / "config",
            _OPS,
            mode="dispatch",
            now=now,
            run_id="run-1",
            product_id=target.product_id,
        )
        self.assertEqual(jobs[0].consumer_environment, "staging")
        self.assertFalse(target.notify)
        self.assertFalse(jobs[0].notify)
        self.assertIn("matrix.run.notify == true", workflow)
        self.assertTrue(all(item.notify for item in load_targets(_OPS) if item.scheduled))
        bundle = load_bundle(ROOT / "config", target.product_id)
        origin = parse_iso_utc(jobs[0].cycle_iso)
        self.assertEqual(origin.hour, bundle.product.publish_origin_hour_utc)
        with self.assertRaises(ValueError):
            select_runs(
                ROOT / "config",
                _OPS,
                mode="schedule",
                now=now,
                run_id="run-1",
                cron=target.schedule_cron_utc,
            )

    def test_manual_notify_refuses_before_a_receipt_write(self) -> None:
        target = next(item for item in load_targets(_OPS) if not item.notify)
        with self.assertRaisesRegex(ValueError, "does not send a consumer event"):
            dispatch_published(
                ROOT / "config",
                _OPS,
                product_id=target.product_id,
                cycle="2026-06-02T12:00:00Z",
                attempt="att-01",
                store_uri="s3://bucket/prefix",
                token="token-value",
            )

    def test_operations_cron_must_match_the_product(self) -> None:
        text = _OPS.read_text(encoding="utf-8").replace('"00 10 * * *"', '"30 10 * * *"', 1)
        self.assertNotEqual(text, _OPS.read_text(encoding="utf-8"))
        target = next(item for item in load_targets(_OPS) if not item.scheduled)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecasts.yaml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must equal the product schedule"):
                select_runs(
                    ROOT / "config",
                    path,
                    mode="dispatch",
                    now=datetime(2026, 6, 2, 10, 30, tzinfo=UTC),
                    run_id="run-1",
                    product_id=target.product_id,
                )

    def test_other_cpus_are_rejected(self) -> None:
        rejected = (
            ["c6g.8xlarge"],
            ["c8gd.8xlarge"],
            ["c7i.8xlarge"],
            ["c8g.16xlarge"],
            ["c8g.metal-24xl"],
        )
        for names in rejected:
            with self.assertRaises(ValueError):
                _families({"families": names}, 32)
