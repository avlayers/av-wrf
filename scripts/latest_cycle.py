"""Print the latest due publish origin for a configured product."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from av_wrf.contracts import format_iso_utc, parse_iso_utc
from av_wrf.core import is_cycle_live_fresh, parse_cycle_arg
from av_wrf.runner import load_bundle


def _cron_offset_minutes(cron: str, forcing_hour: int) -> int:
    """Return minutes from the forcing cycle to the daily schedule trigger."""
    parts = cron.split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        raise ValueError(f"Expected a daily UTC cron expression, got {cron!r}")
    minute, hour = int(parts[0]), int(parts[1])
    if not (0 <= minute < 60 and 0 <= hour < 24):
        raise ValueError(f"Cron minute/hour out of range in {cron!r}")
    return (hour * 60 + minute - forcing_hour * 60) % (24 * 60)


def latest_cycle(config_dir: Path, product_id: str, now: datetime, *, cron: str = "") -> str:
    """Return the newest publish origin whose trigger has passed and is live."""
    bundle = load_bundle(config_dir, product_id)
    product = bundle.product
    trigger_cron = cron or product.schedule_cron_utc
    trigger = timedelta(minutes=_cron_offset_minutes(trigger_cron, product.forcing_cycle_hour_utc))
    forcing = now.replace(hour=product.forcing_cycle_hour_utc, minute=0, second=0, microsecond=0)
    while forcing + trigger > now:
        forcing -= timedelta(days=1)
    origin_gap = (product.publish_origin_hour_utc - product.forcing_cycle_hour_utc) % 24
    origin = forcing + timedelta(hours=origin_gap)
    origin_iso = format_iso_utc(origin)
    if not is_cycle_live_fresh(product, bundle.output, origin_iso, format_iso_utc(now)):
        raise ValueError(
            f"No live cycle for {product_id} at {format_iso_utc(now)}; "
            f"latest due origin is {origin_iso}"
        )
    return origin_iso


def validate_cycle(config_dir: Path, product_id: str, cycle: str, now: datetime) -> str:
    """Return the canonical origin for a live explicit cycle, or raise."""
    bundle = load_bundle(config_dir, product_id)
    origin_iso, _ = parse_cycle_arg(cycle, bundle.product)
    if not is_cycle_live_fresh(bundle.product, bundle.output, origin_iso, format_iso_utc(now)):
        raise ValueError(f"Cycle {origin_iso} is not live at {format_iso_utc(now)}")
    return origin_iso


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True, help="Configured product id")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--now", help="ISO UTC override for testing")
    parser.add_argument("--cycle", help="Validate this explicit cycle instead of resolving")
    args = parser.parse_args(argv)
    now = datetime.now(UTC) if args.now is None else parse_iso_utc(args.now)
    try:
        origin = (
            validate_cycle(args.config_dir, args.product, args.cycle, now)
            if args.cycle is not None
            else latest_cycle(args.config_dir, args.product, now)
        )
        print(origin)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
