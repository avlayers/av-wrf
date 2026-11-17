"""Cycle resolver boundaries, including schedules that wrap past midnight."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from scripts.latest_cycle import _cron_offset_minutes, latest_cycle, validate_cycle

from av_wrf.contracts import format_iso_utc, parse_iso_utc
from av_wrf.runner import load_bundle

CONFIG = Path(__file__).resolve().parents[2] / "config"


def _wrapped_config(tmp_path: Path) -> tuple[Path, str]:
    config = tmp_path / "config"
    shutil.copytree(CONFIG, config)
    product_path = next(
        path
        for path in sorted((config / "products").glob("*.yaml"))
        if (doc := yaml.safe_load(path.read_text(encoding="utf-8")))
        and doc["forcing_cycle_hour_utc"] == 18
        and doc["publish_origin_hour_utc"] == 0
        and not doc["enforce_local_deadline"]
    )
    doc = yaml.safe_load(product_path.read_text(encoding="utf-8"))
    doc["schedule_cron_utc"] = "15 6 * * *"
    product_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return config, str(doc["product_id"])


def test_configured_trigger_boundaries() -> None:
    product = next(
        path.stem
        for path in sorted((CONFIG / "products").glob("*.yaml"))
        if (doc := yaml.safe_load(path.read_text(encoding="utf-8")))
        and doc["forcing_cycle_hour_utc"] == 18
        and doc["publish_origin_hour_utc"] == 0
        and not doc["enforce_local_deadline"]
    )
    with pytest.raises(ValueError, match="No live cycle"):
        latest_cycle(CONFIG, product, parse_iso_utc("2026-11-16T23:14:59Z"))
    assert latest_cycle(CONFIG, product, parse_iso_utc("2026-11-16T23:15:00Z")) == (
        "2026-11-17T00:00:00Z"
    )
    assert latest_cycle(CONFIG, product, parse_iso_utc("2026-11-16T23:15:01Z")) == (
        "2026-11-17T00:00:00Z"
    )


def test_explicit_cycle_freshness_boundaries() -> None:
    bundles = [
        load_bundle(CONFIG, path.stem) for path in sorted((CONFIG / "products").glob("*.yaml"))
    ]
    short = next(b for b in bundles if b.product.enforce_local_deadline)
    origin = datetime(2026, 11, 17, short.product.publish_origin_hour_utc, tzinfo=UTC)
    product_id = short.product.product_id
    origin_iso = format_iso_utc(origin)
    assert validate_cycle(CONFIG, product_id, origin_iso, origin) == origin_iso
    part = f"20261117/{short.product.publish_origin_hour_utc:02d}"
    assert validate_cycle(CONFIG, product_id, part, origin) == origin_iso

    earliest = origin - timedelta(hours=short.product.spinup_hours, minutes=15)
    with pytest.raises(ValueError, match="not live"):
        validate_cycle(CONFIG, product_id, origin_iso, earliest - timedelta(seconds=1))

    hour, minute = (int(p) for p in short.product.local_deadline_hhmm.split(":"))
    deadline_local = origin.astimezone(ZoneInfo(short.product.region_tz)).replace(
        hour=hour, minute=minute, second=0
    )
    deadline = deadline_local.astimezone(UTC)
    if deadline <= origin:
        deadline += timedelta(days=1)
    with pytest.raises(ValueError, match="not live"):
        validate_cycle(CONFIG, product_id, origin_iso, deadline + timedelta(seconds=1))


def test_wrapped_trigger_selects_only_the_due_cycle(tmp_path: Path) -> None:
    config, product = _wrapped_config(tmp_path)
    assert latest_cycle(config, product, parse_iso_utc("2026-11-17T06:15:00Z")) == (
        "2026-11-17T00:00:00Z"
    )
    with pytest.raises(ValueError, match="No live cycle"):
        latest_cycle(config, product, parse_iso_utc("2026-11-17T03:00:00Z"))
    with pytest.raises(ValueError, match="No live cycle"):
        latest_cycle(config, product, parse_iso_utc("2026-11-17T06:14:59Z"))


def test_wrapped_trigger_rolls_over_year_end(tmp_path: Path) -> None:
    config, product = _wrapped_config(tmp_path)
    assert latest_cycle(config, product, parse_iso_utc("2027-01-01T06:15:00Z")) == (
        "2027-01-01T00:00:00Z"
    )


@pytest.mark.parametrize("cron", ["99 6 * * *", "15 25 * * *", "15 6 * * 1"])
def test_cron_bounds_and_daily_shape(cron: str) -> None:
    with pytest.raises(ValueError):
        _cron_offset_minutes(cron, 18)
