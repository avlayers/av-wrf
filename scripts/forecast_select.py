"""Select unattended forecast runs for a schedule trigger or a dispatch."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from av_wrf.cli.env import load_runtime_env
from av_wrf.contracts import format_iso_utc, parse_iso_utc
from av_wrf.core import parse_cycle_arg
from av_wrf.runner import LoadedBundle, load_bundle, load_ready_receipt, open_store
from scripts.forecast_job import (
    GEOG_CACHE_KEY,
    ForecastJob,
    ForecastTarget,
    load_targets,
    native_timeout_s,
    solver_label,
    timeout_minutes,
    verify_manifest,
    write_manifest,
)
from scripts.latest_cycle import _cron_offset_minutes, latest_cycle, validate_cycle

MAX_SCHEDULE_LATENESS = timedelta(hours=3)
_OPS = Path("config/operations/forecasts.yaml")


def select_runs(
    config_dir: Path,
    ops_path: Path,
    *,
    mode: str,
    now: datetime,
    run_id: str,
    cron: str = "",
    product_id: str = "",
    cycle: str = "",
    attempt: str = "att-01",
) -> tuple[ForecastJob, ...]:
    """Return the runs a schedule trigger or a dispatch should launch."""
    if not run_id.strip():
        raise ValueError("run_id is required")
    moment = _aware(now)
    chosen = _choose_targets(load_targets(ops_path), mode=mode, cron=cron, product_id=product_id)
    jobs = tuple(
        _job(
            config_dir,
            target,
            moment,
            run_id=run_id.strip(),
            cycle=cycle.strip(),
            attempt=_attempt(attempt),
            enforce_lateness=mode == "schedule",
        )
        for target in chosen
    )
    if len({job.image_name for job in jobs}) != 1:
        raise ValueError("Selected runs must share one solver image")
    return jobs


def cycle_published(config_dir: Path, product_id: str, cycle_iso: str, store_uri: str) -> bool:
    """Return whether the durable store already holds this cycle's ready receipt."""
    bundle = load_bundle(config_dir, product_id)
    _origin, partition = parse_cycle_arg(cycle_iso, bundle.product)
    store = open_store(store_uri, s3=load_runtime_env().s3)
    try:
        load_ready_receipt(store, bundle.product.model_id, partition)
    except FileNotFoundError:
        return False
    return True


def drop_published(
    config_dir: Path, store_uri: str, runs: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Drop cycles that already have a ready receipt in the durable store."""
    pending: list[dict[str, object]] = []
    for run in runs:
        product_id = _req(run, "product_id")
        cycle_iso = _req(run, "cycle_iso")
        if not cycle_published(config_dir, product_id, cycle_iso, store_uri):
            pending.append(run)
    return pending


def _job(
    config_dir: Path,
    target: ForecastTarget,
    now: datetime,
    *,
    run_id: str,
    cycle: str,
    attempt: str,
    enforce_lateness: bool,
) -> ForecastJob:
    origin, partition = _resolved_origin(
        config_dir,
        target.product_id,
        now,
        cycle=cycle,
        cron=target.schedule_cron_utc,
        enforce_lateness=enforce_lateness,
    )
    budget = timeout_minutes(target.estimate_minutes)
    token = partition.replace("/", "_")
    prefix = f"forecast-{target.product_id}-{token}-{attempt}"
    return ForecastJob(
        product_id=target.product_id,
        cycle_iso=origin,
        cycle_partition=partition,
        attempt=attempt,
        timeout_minutes=budget,
        native_timeout_s=native_timeout_s(budget),
        mpi_ranks=target.mpi_ranks,
        solver_label=solver_label(run_id, target),
        image_name=target.image_name,
        inputs_cache_key=f"{prefix}-inputs",
        wrfout_cache_key=f"{prefix}-wrfout",
        geog_cache_key=GEOG_CACHE_KEY,
        consumer_repository=target.consumer_repository,
        consumer_model=target.consumer_model,
        consumer_environment=target.consumer_environment,
        notify=target.notify,
    )


def _resolved_origin(
    config_dir: Path,
    product_id: str,
    now: datetime,
    *,
    cycle: str,
    cron: str,
    enforce_lateness: bool,
) -> tuple[str, str]:
    bundle = load_bundle(config_dir, product_id)
    if cron != bundle.product.schedule_cron_utc:
        raise ValueError(
            f"Operations schedule {cron!r} must equal the product schedule "
            f"{bundle.product.schedule_cron_utc!r}"
        )
    origin = (
        validate_cycle(config_dir, product_id, cycle, now)
        if cycle
        else latest_cycle(config_dir, product_id, now, cron=cron)
    )
    _reject_late(bundle, origin, now, cron=cron, enforce=enforce_lateness and not cycle)
    _iso, partition = parse_cycle_arg(origin, bundle.product)
    return origin, partition


def _reject_late(
    bundle: LoadedBundle, origin: str, now: datetime, *, cron: str, enforce: bool
) -> None:
    nominal = _nominal_cron(bundle, origin, cron)
    if enforce and now - nominal > MAX_SCHEDULE_LATENESS:
        raise ValueError(
            f"Schedule is more than 3h after {format_iso_utc(nominal)}; refusing before download"
        )


def _nominal_cron(bundle: LoadedBundle, origin_iso: str, cron: str) -> datetime:
    product = bundle.product
    gap = (product.publish_origin_hour_utc - product.forcing_cycle_hour_utc) % 24
    forcing = parse_iso_utc(origin_iso) - timedelta(hours=gap)
    minutes = _cron_offset_minutes(cron, product.forcing_cycle_hour_utc)
    if not isinstance(minutes, int):
        raise ValueError("Cron offset must be a whole number of minutes")
    return forcing + timedelta(minutes=minutes)


def _choose_targets(
    targets: tuple[ForecastTarget, ...],
    *,
    mode: str,
    cron: str,
    product_id: str,
) -> tuple[ForecastTarget, ...]:
    if mode == "schedule":
        return _matching_cron(targets, cron)
    if mode != "dispatch":
        raise ValueError(f"Unknown selection mode {mode!r}")
    wanted = product_id.strip()
    if not wanted:
        raise ValueError("Dispatch requires a product id from the operations file")
    matched = tuple(target for target in targets if target.product_id == wanted)
    if not matched:
        raise ValueError(f"Product {wanted!r} is not an enabled forecast run")
    return matched


def _matching_cron(targets: tuple[ForecastTarget, ...], cron: str) -> tuple[ForecastTarget, ...]:
    needle = cron.strip()
    if not needle:
        raise ValueError("A schedule event needs the triggering cron")
    chosen = tuple(
        target for target in targets if target.scheduled and target.schedule_cron_utc == needle
    )
    if not chosen:
        raise ValueError(f"No enabled run uses schedule {needle!r}")
    return chosen


def _attempt(raw: str) -> str:
    attempt = raw.strip() or "att-01"
    if "/" in attempt or attempt in {".", ".."} or any(char.isspace() for char in attempt):
        raise ValueError(f"Invalid attempt id {attempt!r}")
    return attempt


def _req(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {key}")
    return value.strip()


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def _load_run_list(raw: str) -> list[dict[str, object]]:
    parsed = cast(object, json.loads(raw))
    if not isinstance(parsed, list):
        raise ValueError("selection must be a JSON list")
    return [_as_map(cast(object, item)) for item in parsed]


def _as_map(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("run must be a mapping")
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("run keys must be strings")
        mapped[key] = item
    return mapped


def _add_select(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--ops", type=Path, default=_OPS)
    parser.add_argument("--mode", choices=("schedule", "dispatch"), required=True)
    parser.add_argument("--now", default="")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cron", default="")
    parser.add_argument("--product", default="")
    parser.add_argument("--cycle", default="")
    parser.add_argument("--attempt", default="att-01")


def _command_select(args: argparse.Namespace) -> int:
    moment = datetime.now(UTC) if not args.now else parse_iso_utc(str(args.now))
    jobs = select_runs(
        args.config_dir,
        args.ops,
        mode=str(args.mode),
        now=moment,
        run_id=str(args.run_id),
        cron=str(args.cron),
        product_id=str(args.product),
        cycle=str(args.cycle),
        attempt=str(args.attempt),
    )
    rows = [cast(dict[str, object], asdict(job)) for job in jobs]
    print(json.dumps(rows, separators=(",", ":")))
    return 0


def _command_pending(args: argparse.Namespace) -> int:
    store_uri = load_runtime_env().store_uri
    if not store_uri:
        raise ValueError("AV_WRF_STORE_URI is required")
    runs = drop_published(args.config_dir, store_uri, _load_run_list(str(args.runs)))
    print(json.dumps(runs, separators=(",", ":")))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    _add_select(sub.add_parser("select"))
    pending = sub.add_parser("pending")
    pending.add_argument("--config-dir", type=Path, default=Path("config"))
    pending.add_argument("--runs", required=True)
    for name in ("manifest", "verify-manifest"):
        command = sub.add_parser(name)
        command.add_argument("root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch selection, receipt filtering, and archive manifests."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "select":
            return _command_select(args)
        if args.command == "pending":
            return _command_pending(args)
        if args.command == "manifest":
            write_manifest(args.root)
            return 0
        verify_manifest(args.root)
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError) as exc:
        print(f"forecast_select: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
