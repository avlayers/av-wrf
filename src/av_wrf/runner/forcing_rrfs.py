"""Admit NOAA RRFS CONUS pressure-level and surface GRIB2 for WPS."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from av_wrf.contracts import DomainProfile, ForcingProfile
from av_wrf.core import computational_latlon_bounds, forcing_domain
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.grib import (
    GRIB_FIELD_KEYS,
    GRIB_KEYS,
    checked_grib_grid,
    grib_number,
    inspect_grib,
)
from av_wrf.runner.rrfs_lambert import require_rrfs_coverage

# ungrib's default pmin is 100 Pa, so 2 hPa is retained. metgrid adds the surface.
# https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/read_namelist.F
_PRESSURE_HPA = frozenset((2, 5, 7, 10, 20, 30, 50, 70, *range(100, 1001, 25)))
_AIR = frozenset(("HGT", "TMP", "UGRD", "VGRD", "SPFH"))
_SURFACE = frozenset(("PRES", "HGT", "TMP", "LAND", "ICEC", "WEASD", "SNOD"))
_SOIL_LEVELS = (
    "0-0 m below ground",
    "0.01-0.01 m below ground",
    "0.04-0.04 m below ground",
    "0.1-0.1 m below ground",
    "0.3-0.3 m below ground",
    "0.6-0.6 m below ground",
    "1-1 m below ground",
    "1.6-1.6 m below ground",
    "3-3 m below ground",
)
_AIR_CODES = ((0, 3, 5), (0, 0, 0), (0, 2, 2), (0, 2, 3), (0, 1, 0))
_SURFACE_IDS = (
    (0, 3, 0, 1, 0, 0),
    (0, 3, 5, 1, 0, 0),
    (0, 0, 0, 1, 0, 0),
    (2, 0, 0, 1, 0, 0),
    (10, 2, 0, 1, 0, 0),
    (0, 1, 13, 1, 0, 0),
    (0, 1, 11, 1, 0, 0),
    (0, 0, 0, 103, 2, 2),
    (0, 1, 0, 103, 2, 2),
    (0, 2, 2, 103, 10, 10),
    (0, 2, 3, 103, 10, 10),
    (0, 3, 192, 101, 0, 0),
)
_SOIL_CM = (0, 1, 4, 10, 30, 60, 100, 160, 300)
_SOIL_CODES = ((2, 0, 2), (2, 0, 192))
_WIND = frozenset(((0, 2, 2), (0, 2, 3)))
_LONG_HOURS = frozenset((0, 6, 12, 18))
_SHORT_HOURS = frozenset((3, 9, 15, 21))
_PACKING = ("grid_complex_spatial_differencing",)
_KEYS = (
    GRIB_KEYS + ",numberOfMissing:i,uvRelativeToGrid:i,typeOfSecondFixedSurface:i,"
    "DxInMetres:d,DyInMetres:d,LaDInDegrees:d,LoVInDegrees:d,"
    "Latin1InDegrees:d,Latin2InDegrees:d,shapeOfTheEarth:i"
)
_METGRID = (
    "PRES",
    "GHT",
    "TT",
    "SPECHUMD",
    "UU",
    "VV",
    "PSFC",
    "PMSL",
    "SKINTEMP",
    "SOILHGT",
    "LANDSEA",
    "SEAICE",
    "SNOW",
    "SNOWH",
    "SOILT",
    "SOILM",
    "SOIL_LEVELS",
    *(f"SOILT{cm:03d}" for cm in _SOIL_CM),
    *(f"SOILM{cm:03d}" for cm in _SOIL_CM),
)


def rrfs_message_selected(variable: str, level: str) -> bool:
    """Return whether one inventory line belongs in the WPS RRFS subset."""
    pressure = re.fullmatch(r"([0-9]+) mb", level)
    if pressure:
        return variable in _AIR and int(pressure.group(1)) in _PRESSURE_HPA
    if level == "surface":
        return variable in _SURFACE
    if level == "mean sea level":
        return variable == "MSLET"
    if level in _SOIL_LEVELS:
        return variable in ("TSOIL", "SOILW")
    if level == "2 m above ground":
        return variable in ("TMP", "SPFH")
    return level == "10 m above ground" and variable in ("UGRD", "VGRD")


def rrfs_required_fields(role: str) -> frozenset[tuple[int, ...]]:
    """Return the GRIB identity tuples one component or the assembly must contain."""
    air = frozenset((*code, 100, hpa, hpa) for code in _AIR_CODES for hpa in _PRESSURE_HPA)
    soil = frozenset((*code, 106, cm, cm) for code in _SOIL_CODES for cm in _SOIL_CM)
    surface = frozenset(_SURFACE_IDS)
    if role == "prslev":
        return air
    if role == "2dfld":
        return soil | surface
    if role == "full":
        return air | soil | surface
    raise ValueError(f"Unknown RRFS component role {role}")


def _row(level_type: int, top: object, bottom: object, name: str, units: str, desc: str) -> str:
    disc = {"HGT": (0, 3, 5), "TT": (0, 0, 0), "UU": (0, 2, 2), "VV": (0, 2, 3)}
    codes = {
        **disc,
        "SPECHUMD": (0, 1, 0),
        "PSFC": (0, 3, 0),
        "SOILHGT": (0, 3, 5),
        "SKINTEMP": (0, 0, 0),
        "LANDSEA": (2, 0, 0),
        "SEAICE": (10, 2, 0),
        "SNOW": (0, 1, 13),
        "SNOWH": (0, 1, 11),
        "PMSL": (0, 3, 192),
    }
    if name.startswith("SOILT"):
        code, g2lev = (2, 0, 2), 106
    elif name.startswith("SOILM"):
        code, g2lev = (2, 0, 192), 106
    else:
        code = codes[name]
        g2lev = {100: 100, 1: 1, 105: 103, 102: 101}[level_type]
    second = f" {bottom} " if bottom != "" else " "
    return (
        f" | {level_type} | {top} |{second}| {name} | {units} | {desc} | "
        f"{code[0]} | {code[1]} | {code[2]} | {g2lev} |"
    )


def _vtable() -> str:
    """Render Vtable.RRFS. metgrid renames intermediate HGT to GHT."""
    # https://github.com/wrf-model/WPS/blob/v4.7.0/metgrid/src/read_met_module.F#L93
    air = (
        ("HGT", "m", "HGT"),
        ("TT", "K", "TMP"),
        ("UU", "m s-1", "UGRD"),
        ("VV", "m s-1", "VGRD"),
        ("SPECHUMD", "kg kg-1", "SPFH"),
    )
    surface = (
        (1, 0, "PSFC", "Pa", "PRES"),
        (1, 0, "SOILHGT", "m", "HGT"),
        (1, 0, "SKINTEMP", "K", "TMP"),
        (1, 0, "LANDSEA", "0/1", "LAND"),
        (1, 0, "SEAICE", "fraction", "ICEC"),
        (1, 0, "SNOW", "kg m-2", "WEASD"),
        (1, 0, "SNOWH", "m", "SNOD"),
        (105, 2, "TT", "K", "TMP"),
        (105, 2, "SPECHUMD", "kg kg-1", "SPFH"),
        (105, 10, "UU", "m s-1", "UGRD"),
        (105, 10, "VV", "m s-1", "VGRD"),
        (102, 0, "PMSL", "Pa", "MSLET"),
    )
    rows = [_row(100, "*", "", name, units, desc) for name, units, desc in air]
    rows.extend(
        _row(kind, level, "", name, units, desc) for kind, level, name, units, desc in surface
    )
    # Equal endpoints are centimetres. SOILW is already a fraction, so thickness must not scale it.
    for cm in _SOIL_CM:
        rows.append(_row(112, cm, cm, f"SOILT{cm:03d}", "K", "TSOIL"))
        rows.append(_row(112, cm, cm, f"SOILM{cm:03d}", "fraction", "SOILW"))
    return "\n".join(("---", *rows, "---", ""))


def _check_window(
    source: ForcingProfile, domain: DomainProfile, cycle: datetime, hours: tuple[int, ...]
) -> None:
    if not source.use_sh_qv or (source.metgrid_levels, source.metgrid_soil_levels) != (46, 9):
        raise ValueError("RRFS requires specific humidity, 46 metgrid levels, and 9 soil depths")
    if min(_PRESSURE_HPA) * 100 > domain.p_top_pa:
        raise ValueError("RRFS lowest pressure is above the domain model top")
    if not hours or hours[0] < 0:
        raise ValueError("RRFS window requires forecast hours starting at zero or later")
    if cycle.hour in _LONG_HOURS:
        limit = 84
    elif cycle.hour in _SHORT_HOURS:
        limit = 18
    else:
        raise ValueError(f"No hourly 3D RRFS cycle at {cycle:%H}Z for {source.profile_id}")
    if hours[-1] > limit:
        raise ValueError(
            f"{source.profile_id} {cycle:%H}Z hourly files end at f{limit:03d}; "
            f"cycle requires f{hours[-1]:03d}"
        )


def _file_keys(cycle: datetime, hours: tuple[int, ...]) -> Mapping[str, tuple[str, str]]:
    stem = f"rrfs.{cycle:%Y%m%d}/{cycle:%H}/rrfs.t{cycle:%H}z"
    return {
        str(hour): (
            f"{stem}.prslev.3km.f{hour:03d}.conus.grib2",
            f"{stem}.2dfld.3km.f{hour:03d}.conus.grib2",
        )
        for hour in hours
    }


def _flag(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Missing or invalid GRIB2 key {key}")
    return value


def _field_id(row: Mapping[str, object]) -> tuple[int, ...]:
    disc, cat, num, surface = (int(grib_number(row, key)) for key in GRIB_FIELD_KEYS[:4])
    if surface == 100:
        unit = row.get("typeOfLevel")
        if unit not in ("isobaricInhPa", "isobaricInPa"):
            raise ValueError("Unknown GRIB isobaric coordinate units")
        level = grib_number(row, "level")
        hpa = round(level if unit == "isobaricInhPa" else level / 100.0)
        return (disc, cat, num, 100, hpa, hpa)
    top, bottom = grib_number(row, "level"), grib_number(row, "bottomLevel")
    if surface == 106:
        # A live TSOIL message stores scale 2 and scaled value 1: ecCodes level is metres.
        if _flag(row, "typeOfSecondFixedSurface") != 106 or abs(top - bottom) > 1e-4:
            raise ValueError("RRFS soil depth must be an equal-endpoint layer in metres")
        return (disc, cat, num, 106, round(top * 100), round(bottom * 100))
    return (disc, cat, num, surface, round(top), round(bottom))


def _accept(
    row: Mapping[str, object],
    path: Path,
    fields: set[tuple[int, ...]],
    required: frozenset[tuple[int, ...]],
) -> None:
    field = _field_id(row)
    if field not in required or field in fields:
        raise ValueError(f"Unexpected or duplicate RRFS field {field} in {path}")
    missing = _flag(row, "numberOfMissing")
    # Soil is masked over water. Other required fields are complete.
    if missing < 0 or (field[3] != 106 and missing != 0):
        raise ValueError(f"Missing values in required RRFS field {field} in {path}")
    # WPS rotates grid winds to earth, then to the target grid.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rd_grib2.F#L452
    if field[:3] in _WIND and _flag(row, "uvRelativeToGrid") != 1:
        raise ValueError(f"RRFS winds must be grid-relative in {path}")
    fields.add(field)


def _expected_time(cycle: datetime, fhr: int) -> tuple[int, ...]:
    valid = cycle + timedelta(hours=fhr)
    return (
        2,
        7,
        int(cycle.strftime("%Y%m%d")),
        int(cycle.strftime("%H%M")),
        int(valid.strftime("%Y%m%d")),
        int(valid.strftime("%H%M")),
    )


def admit_grib(
    path: Path,
    cycle: datetime,
    source: ForcingProfile,
    domain: DomainProfile,
    fhr: int,
    required: frozenset[tuple[int, ...]],
) -> str:
    """Return the shared section-3 digest after the file matches `required`."""
    expected = _expected_time(cycle, fhr)
    messages = inspect_grib(path, _KEYS)
    spacing = source.grid_spacing_deg
    bounds = computational_latlon_bounds(forcing_domain(domain, spacing), snap_deg=spacing)
    fields: set[tuple[int, ...]] = set()
    for row in checked_grib_grid(path, messages, bounds, spacing, _PACKING, require_rrfs_coverage):
        identity = tuple(
            row.get(key)
            for key in ("edition", "centre", "dataDate", "dataTime", "validityDate", "validityTime")
        )
        # Process 134 is present on f000 as well as later hours.
        if identity != expected or row.get("generatingProcessIdentifier") != 134:
            raise ValueError(f"Wrong RRFS source, cycle or valid time in {path} for f{fhr:03d}")
        _accept(row, path, fields, required)
    if missing := required - fields:
        raise ValueError(f"Missing {len(missing)} RRFS messages in {path}")
    grid = messages[0].get("md5Section3")
    if not isinstance(grid, str) or not grid:
        raise ValueError(f"Missing GRIB2 section-3 digest in {path}")
    return grid


def _validate_file(
    path: Path, cycle: datetime, source: ForcingProfile, domain: DomainProfile, fhr: int
) -> None:
    admit_grib(path, cycle, source, domain, fhr, rrfs_required_fields("full"))


RRFS = ForcingRecipe(
    identity=("NOAA", "Vtable.RRFS", 3600, 0.027),
    check_window=_check_window,
    file_keys=_file_keys,
    validate_file=_validate_file,
    metgrid_fields=_METGRID,
    metgrid_soil_dimensions=("num_soilt_levels", "num_soilm_levels"),
    vtable_text=_vtable(),
)

__all__ = ["RRFS", "admit_grib", "rrfs_message_selected", "rrfs_required_fields"]
