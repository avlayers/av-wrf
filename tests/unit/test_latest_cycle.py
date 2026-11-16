"""Cycle resolver boundaries, including schedules that wrap past midnight."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from scripts.latest_cycle import _cron_offset_minutes, latest_cycle

from av_wrf.contracts import parse_iso_utc

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
