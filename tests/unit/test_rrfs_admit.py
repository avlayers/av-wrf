"""Admit synthetic RRFS messages and the configured single-domain product."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from av_wrf.core import nesting_active, resolve_cycle_window
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.forcing_rrfs import RRFS, admit_grib, rrfs_required_fields
from av_wrf.runner.grib import GRIB_FIELD_KEYS
from av_wrf.runner.mirrors import find_forcing_recipe, resolve_forcing_window
from av_wrf.runner.rrfs_lambert import require_rrfs_coverage, rrfs_source_ij
from tests.integration import bundle_for_spacing

_ROOT = Path(__file__).resolve().parents[2]
_CYCLE = datetime(2026, 11, 17, 6, tzinfo=UTC)
_MSLET = (0, 3, 192, 101, 0, 0)


def _bundle() -> LoadedBundle:
    return bundle_for_spacing(_ROOT / "config", 0.027)


def _grid(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "gridType": "lambert",
        "gridDefinitionTemplateNumber": 30,
        "shapeOfTheEarth": 6,
        "Ni": 1799,
        "Nj": 1059,
        "numberOfDataPoints": 1799 * 1059,
        "DxInMetres": 3000.0,
        "DyInMetres": 3000.0,
        "latitudeOfFirstGridPointInDegrees": 21.138123,
        "longitudeOfFirstGridPointInDegrees": 237.280472,
        "LaDInDegrees": 38.5,
        "LoVInDegrees": 262.5,
        "Latin1InDegrees": 38.5,
        "Latin2InDegrees": 38.5,
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "md5Section3": "rrfs-grid",
        "packingType": "grid_complex_spatial_differencing",
        "min": 1.0,
        "max": 2.0,
        "average": 1.5,
    }
    row.update(overrides)
    return row


def _messages(
    fields: tuple[tuple[int, ...], ...], *, fhr: int = 0, process: int = 134
) -> list[dict[str, object]]:
    valid = _CYCLE + timedelta(hours=fhr)
    shared = _grid(
        edition=2,
        centre=7,
        generatingProcessIdentifier=process,
        dataDate=20261117,
        dataTime=600,
        validityDate=int(valid.strftime("%Y%m%d")),
        validityTime=int(valid.strftime("%H%M")),
        productDefinitionTemplateNumber=0,
    )
    rows = []
    for index, field in enumerate(fields):
        disc, cat, num, surface, top, bottom = field
        level = top / 100.0 if surface == 106 else float(top)
        depth = bottom / 100.0 if surface == 106 else float(bottom)
        # Soil and the two snow fields are bitmapped in a live CONUS file.
        masked = surface == 106 or (disc, cat, num) in ((0, 1, 11), (0, 1, 13))
        rows.append(
            {
                **shared,
                **dict(zip(GRIB_FIELD_KEYS, (disc, cat, num, surface, level, depth), strict=True)),
                "typeOfLevel": "isobaricInhPa" if surface == 100 else "surface",
                "typeOfSecondFixedSurface": 106 if surface == 106 else 255,
                "numberOfMissing": 4 if masked else 0,
                "uvRelativeToGrid": 1 if (disc, cat) == (0, 2) else 0,
                "offset": index * 20,
                "totalLength": 20,
            }
        )
    return rows


def _admit(path: Path, messages: list[dict[str, object]], fhr: int = 0) -> str:
    bundle = _bundle()
    path.write_bytes(b" " * (20 * len(messages)))
    with patch("av_wrf.runner.forcing_rrfs.inspect_grib", return_value=messages):
        return admit_grib(
            path,
            _CYCLE,
            bundle.primary_source,
            bundle.domain,
            fhr,
            rrfs_required_fields("full"),
        )


def test_product_is_one_domain_and_requests_thirty_one_hours() -> None:
    bundle = _bundle()
    window = resolve_cycle_window(bundle.product, bundle.output, "2026-11-17T12:00:00Z")
    _, _, hours = resolve_forcing_window(window, bundle.primary_source, domain=bundle.domain)
    assert find_forcing_recipe(bundle.primary_source) is RRFS
    assert bundle.primary_source.profile_id == bundle.product.candidate_source_profile
    assert bundle.product.enforce_local_deadline is False
    assert hours == tuple(range(31))
    assert not nesting_active(bundle.domain, bundle.primary_source.grid_spacing_deg)
    allowlist = (_ROOT / "config/operations/forecasts.yaml").read_text(encoding="utf-8")
    assert bundle.product.product_id not in allowlist
    RRFS.check_window(bundle.primary_source, bundle.domain, _CYCLE, hours)
    text = RRFS.vtable_text or ""
    assert " | 102 | 0 | | PMSL | Pa | MSLET | 0 | 3 | 192 | 101 |" in text
    assert " | 112 | 0 | 0 | SOILT000 | K | TSOIL | 2 | 0 | 2 | 106 |" in text
    assert " | 112 | 300 | 300 | SOILM300 | fraction | SOILW | 2 | 0 | 192 | 106 |" in text
    assert " | 100 | * | | HGT | m | HGT |" in text
    assert "GHT" not in text and " RH " not in text


def test_window_follows_the_published_cycle_lengths() -> None:
    bundle = _bundle()
    source, domain = bundle.primary_source, bundle.domain
    RRFS.check_window(source, domain, _CYCLE.replace(hour=3), tuple(range(19)))
    RRFS.check_window(source, domain, _CYCLE.replace(hour=0), tuple(range(85)))
    with pytest.raises(ValueError, match="No hourly 3D"):
        RRFS.check_window(source, domain, _CYCLE.replace(hour=1), (0,))
    with pytest.raises(ValueError, match="f018"):
        RRFS.check_window(source, domain, _CYCLE.replace(hour=3), tuple(range(20)))
    with pytest.raises(ValueError, match="f084"):
        RRFS.check_window(source, domain, _CYCLE.replace(hour=12), tuple(range(86)))


def test_complete_inventory_covers_the_configured_footprint(tmp_path: Path) -> None:
    bundle = _bundle()
    row = _grid()
    i_idx, j_idx = rrfs_source_ij(row, 21.138123, 237.280472)
    assert abs(i_idx) < 1e-6 and abs(j_idx) < 1e-6
    assert rrfs_source_ij(row, 22.138123, 237.280472)[1] > 0
    assert rrfs_source_ij(row, 21.138123, 238.280472)[0] > 0
    from av_wrf.core import computational_latlon_bounds, forcing_domain

    bounds = computational_latlon_bounds(forcing_domain(bundle.domain, 0.027), snap_deg=0.027)
    require_rrfs_coverage(row, bounds)
    with pytest.raises(ValueError, match="does not cover"):
        require_rrfs_coverage(row, (-140.0, 10.0, -130.0, 20.0))
    messages = _messages(tuple(sorted(rrfs_required_fields("full"))))
    assert _admit(tmp_path / "full.grib2", messages) == "rrfs-grid"


def test_missing_mslet_bad_wind_and_extra_level_are_rejected(tmp_path: Path) -> None:
    fields = tuple(sorted(field for field in rrfs_required_fields("full") if field != _MSLET))
    with pytest.raises(ValueError, match="Missing 1 RRFS"):
        _admit(tmp_path / "mslet.grib2", _messages(fields))
    wind = next(field for field in rrfs_required_fields("full") if field[:3] == (0, 2, 2))
    messages = _messages(tuple(sorted(rrfs_required_fields("full"))))
    messages[tuple(sorted(rrfs_required_fields("full"))).index(wind)]["uvRelativeToGrid"] = 0
    with pytest.raises(ValueError, match="grid-relative"):
        _admit(tmp_path / "wind.grib2", messages)
    extra = (*tuple(sorted(rrfs_required_fields("full"))), (0, 3, 5, 100, 75, 75))
    with pytest.raises(ValueError, match="Unexpected"):
        _admit(tmp_path / "extra.grib2", _messages(extra))
    bare = _messages(tuple(sorted(rrfs_required_fields("full"))), process=81)
    with pytest.raises(ValueError, match="Wrong RRFS"):
        _admit(tmp_path / "process.grib2", bare)


def test_soil_and_snow_masks_are_allowed_and_a_masked_pressure_field_is_not(
    tmp_path: Path,
) -> None:
    messages = _messages(tuple(sorted(rrfs_required_fields("full"))))
    assert any(row["numberOfMissing"] == 4 for row in messages)
    _admit(tmp_path / "soil.grib2", messages)
    pressure = next(row for row in messages if row["typeOfFirstFixedSurface"] == 100)
    pressure["numberOfMissing"] = 1
    with pytest.raises(ValueError, match="Missing values"):
        _admit(tmp_path / "masked.grib2", messages)
    bad = _grid(shapeOfTheEarth=1)
    with pytest.raises(ValueError, match="earth shape"):
        require_rrfs_coverage(bad, (30.0, 35.0, 32.0, 37.0))
