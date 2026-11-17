"""Lambert coverage for the operational RRFS CONUS GRIB2 grid."""

from __future__ import annotations

import math
from collections.abc import Mapping

from av_wrf.core import latlon_to_lambert_xy_m
from av_wrf.runner.grib import grib_number

# Live 3 km CONUS grid, template 3.30. 0.027 deg is only the nest-gate equivalent.
_NI, _NJ, _DX_M = 1799, 1059, 3000.0
_LAT1, _LON1 = 21.138123, 237.280472
_LAD, _LOV, _LATIN = 38.5, 262.5, 38.5
# GRIB2 code table 3.2 value 6 is this sphere. The radius octets are unused.
_RADIUS_M = 6_371_229.0
_PROJ = {
    "origin_lat_deg": _LAD,
    "central_meridian_deg": _LOV,
    "std_parallel_1_deg": _LATIN,
    "std_parallel_2_deg": _LATIN,
    "radius_m": _RADIUS_M,
}


def _near_angle(got: float, expected: float, tol: float = 1e-4) -> bool:
    return abs((got - expected + 180.0) % 360.0 - 180.0) <= tol


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _identity(row: Mapping[str, object]) -> None:
    """Reject a grid that is not the operational CONUS Lambert template."""
    scan = ("iScansNegatively", "jPointsAreConsecutive", "alternativeRowScanning")
    _require(row.get("gridType") == "lambert", "RRFS grid must be Lambert conformal")
    _require(row.get("gridDefinitionTemplateNumber") == 30, "RRFS grid template must be 3.30")
    _require(row.get("shapeOfTheEarth") == 6, "RRFS earth shape must be code 6")
    _require(row.get("jScansPositively") == 1, "RRFS rows must scan north")
    _require(all(row.get(key) == 0 for key in scan), "RRFS rows must scan east without alternation")
    _require(
        int(grib_number(row, "Ni")) == _NI and int(grib_number(row, "Nj")) == _NJ,
        "RRFS grid dimensions disagree with the CONUS 3 km grid",
    )
    _require(
        math.isclose(grib_number(row, "DxInMetres"), _DX_M, abs_tol=1e-3)
        and math.isclose(grib_number(row, "DyInMetres"), _DX_M, abs_tol=1e-3),
        "RRFS grid spacing must be 3000 m",
    )
    _require(
        math.isclose(grib_number(row, "latitudeOfFirstGridPointInDegrees"), _LAT1, abs_tol=1e-5),
        "RRFS first latitude disagrees with the CONUS grid",
    )
    _require(
        _near_angle(grib_number(row, "longitudeOfFirstGridPointInDegrees"), _LON1),
        "RRFS first longitude disagrees with the CONUS grid",
    )
    _require(
        math.isclose(grib_number(row, "LaDInDegrees"), _LAD, abs_tol=1e-4)
        and _near_angle(grib_number(row, "LoVInDegrees"), _LOV)
        and math.isclose(grib_number(row, "Latin1InDegrees"), _LATIN, abs_tol=1e-4)
        and math.isclose(grib_number(row, "Latin2InDegrees"), _LATIN, abs_tol=1e-4),
        "RRFS Lambert cone disagrees with the CONUS grid",
    )


def rrfs_source_ij(
    row: Mapping[str, object], lat_deg: float, lon_deg: float
) -> tuple[float, float]:
    """Return source (i, j) of a geographic point on the admitted CONUS grid."""
    _identity(row)
    x0, y0 = latlon_to_lambert_xy_m(_LAT1, _LON1, **_PROJ)
    x_m, y_m = latlon_to_lambert_xy_m(lat_deg, lon_deg, **_PROJ)
    return (x_m - x0) / _DX_M, (y_m - y0) / _DX_M


def _samples(bounds: tuple[float, float, float, float]) -> tuple[tuple[float, float], ...]:
    west, south, east, north = bounds
    if not east > west or not north > south:
        raise ValueError("RRFS coverage bounds must have positive extent")
    lats = tuple(south + (north - south) * step / 4 for step in range(5))
    lons = tuple(west + (east - west) * step / 4 for step in range(5))
    return tuple((lat, lon) for lat in lats for lon in lons)


def require_rrfs_coverage(
    row: Mapping[str, object], bounds: tuple[float, float, float, float]
) -> None:
    """Require the footprint-plus-halo samples to land inside the CONUS Lambert grid."""
    for lat, lon in _samples(bounds):
        i_idx, j_idx = rrfs_source_ij(row, lat, lon)
        if not (-1e-3 <= i_idx <= _NI - 1 + 1e-3 and -1e-3 <= j_idx <= _NJ - 1 + 1e-3):
            raise ValueError("RRFS Lambert grid does not cover the forcing footprint")


__all__ = ["require_rrfs_coverage", "rrfs_source_ij"]
