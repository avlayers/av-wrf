"""Offline soil and level contract for an RRFS met_em or wrfinput."""

from __future__ import annotations

import math
from pathlib import Path

import netCDF4
import numpy as np
import numpy.typing as npt

from av_wrf.codecs import NC_FLOAT_FILL_THRESHOLD, verify_wps_fields

# Vtable endpoints in centimetres. metgrid copies those values into SOIL_LEVELS.
_SOIL_CM = (0.0, 1.0, 4.0, 10.0, 30.0, 60.0, 100.0, 160.0, 300.0)
# Noah-MP calls init_soil_depth_2 and this namelist does not override dzs.
# https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_soil_pre.F#L1128
_NOAHMP_ZS_M = (0.05, 0.25, 0.70, 1.50)
_DEPTH_CM = (1.0, 4.0, 10.0, 30.0)
_FIELDS = ("PRES", "GHT", "TT", "SPECHUMD", "UU", "VV", "PMSL", "SOILT", "SOILM")
_SOILM = tuple(f"SOILM{int(cm):03d}" for cm in _SOIL_CM)


def _dim(ds: netCDF4.Dataset, name: str, expected: int) -> None:
    if name not in ds.dimensions or len(ds.dimensions[name]) != expected:
        raise ValueError(f"{name} must be {expected}")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _variable_values(var: object, name: str) -> npt.NDArray[np.float64]:
    """Return finite values. A mask or fill sentinel is invalid before it becomes a number."""
    getter = getattr(var, "__getitem__", None)
    if not callable(getter):
        raise ValueError(f"{name} is not a NetCDF variable")
    raw = getter(slice(None))
    if np.ma.isMaskedArray(raw) and bool(np.ma.is_masked(raw)):
        raise ValueError(f"{name} contains a masked cell")
    plain = np.ma.getdata(raw) if np.ma.isMaskedArray(raw) else raw
    values = np.asarray(plain, dtype=float)
    if values.size == 0 or not bool(np.isfinite(values).all()):
        raise ValueError(f"{name} must be finite")
    if bool(np.any(np.abs(values) >= NC_FLOAT_FILL_THRESHOLD)):
        raise ValueError(f"{name} contains a fill sentinel")
    for attr in ("_FillValue", "missing_value"):
        sentinel = _number(getattr(var, attr, None))
        if sentinel is not None and bool(np.any(values == sentinel)):
            raise ValueError(f"{name} contains a fill sentinel")
    return values


def _field(ds: netCDF4.Dataset, name: str) -> npt.NDArray[np.float64]:
    if name not in ds.variables:
        raise ValueError(f"Missing {name}")
    return _variable_values(ds.variables[name], name)


def _pressure(values: npt.NDArray[np.float64]) -> None:
    """Require the isobar column to reach the model top and the 1000 hPa level."""
    column = values
    if column.ndim >= 2 and column.shape[0] == 1 and column.shape[1] == 46:
        column = column[0]
    if column.shape[0] != 46:
        raise ValueError("PRES must cover 46 metgrid levels")
    typical = np.median(column.reshape(46, -1), axis=1)
    if float(np.min(typical)) > 5000.0:
        raise ValueError("PRES must reach the 5000 Pa model top")
    if not bool(np.any(np.abs(typical - 100_000.0) <= 500.0)):
        raise ValueError("PRES must include the 1000 hPa level")


def _fraction(values: npt.NDArray[np.float64], name: str) -> None:
    # Dry soil is 0. METGRID.TBL fill_missing for SOILM is 1.
    if not 0.0 <= float(np.min(values)) <= float(np.max(values)) <= 1.0:
        raise ValueError(f"{name} must stay inside the volumetric fraction range")


def _soil_levels(ds: netCDF4.Dataset) -> None:
    """Require the nine centimetre depths, in any order, on one constant slab.

    Time-dependent storage keeps the deeper level at the head, and metgrid writes
    that order across the tile. real.exe flips the index and sorts it with the soil.
    https://github.com/wrf-model/WPS/blob/v4.7.0/metgrid/src/storage_module.F#L530
    https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_optional_input.F#L1304
    """
    if "SOIL_LEVELS" not in ds.variables:
        raise ValueError("Missing SOIL_LEVELS")
    var = ds.variables["SOIL_LEVELS"]
    if "num_soilt_levels" not in var.dimensions:
        raise ValueError("SOIL_LEVELS must be the nine RRFS depths in centimetres")
    axis = var.dimensions.index("num_soilt_levels")
    values = _variable_values(var, "SOIL_LEVELS")
    if values.shape[axis] != 9:
        raise ValueError("SOIL_LEVELS must be the nine RRFS depths in centimetres")
    column = np.moveaxis(values, axis, 0).reshape(9, -1)
    if column.shape[1] < 1 or not bool(np.isfinite(column).all()):
        raise ValueError("SOIL_LEVELS must be the nine RRFS depths in centimetres")
    level = column[:, :1]
    if not bool(np.allclose(column, level, rtol=0.0, atol=1e-6)):
        raise ValueError("SOIL_LEVELS must be horizontally constant")
    got = tuple(sorted(float(item) for item in level[:, 0]))
    same = all(abs(item - exp) <= 1e-6 for item, exp in zip(got, _SOIL_CM, strict=True))
    if not same:
        raise ValueError("SOIL_LEVELS must be the nine RRFS depths in centimetres")


def admit_met_em(path: Path) -> None:
    """Require the 46-level, nine-depth met_em contract, including sea-level pressure."""
    with netCDF4.Dataset(str(path)) as ds:
        _dim(ds, "num_metgrid_levels", 46)
        _dim(ds, "num_soilt_levels", 9)
        _dim(ds, "num_soilm_levels", 9)
        if int(getattr(ds, "NUM_METGRID_SOIL_LEVELS", -1)) != 9:
            raise ValueError("NUM_METGRID_SOIL_LEVELS must be 9")
        for flag in ("FLAG_SOIL_LEVELS", "FLAG_SH", "FLAG_SLP"):
            if int(getattr(ds, flag, 0)) != 1:
                raise ValueError(f"{flag} must be 1")
        _soil_levels(ds)
        for name in _FIELDS:
            values = _field(ds, name)
            if name == "PRES":
                _pressure(values)
            elif name == "SOILM":
                _fraction(values, name)
        for name in _SOILM:
            _fraction(_field(ds, name), name)
        verify_wps_fields(ds, path, ("SOIL_LEVELS", *_FIELDS, *_SOILM))


def _layers(values: object, name: str) -> npt.NDArray[np.float64]:
    column = np.asarray(values, dtype=float)
    if column.ndim < 1 or column.shape[0] != 4 or not bool(np.isfinite(column).all()):
        raise ValueError(f"{name} must be finite across four soil layers")
    return column.reshape(4, -1)


def _noah_depths(depths: npt.NDArray[np.float64]) -> None:
    same = depths.shape == (4,) and all(
        abs(float(got) - exp) <= 1e-3 for got, exp in zip(depths, _NOAHMP_ZS_M, strict=True)
    )
    if not same:
        raise ValueError("ZS must be the Noah-MP centers 0.05, 0.25, 0.70 and 1.50 m")


def _divided_fraction(moisture: npt.NDArray[np.float64]) -> None:
    """Reject one fraction divided by the first four RRFS centimetre depths."""
    predicted = moisture[:1] / np.asarray(_DEPTH_CM, dtype=float).reshape(4, 1)
    if bool(np.allclose(moisture, predicted, rtol=0.0, atol=1e-6)):
        raise ValueError("SMOIS is a fraction divided by centimetre depth")


def _land_mask(ds: netCDF4.Dataset, columns: int) -> npt.NDArray[np.bool_]:
    values = _field(ds, "LANDMASK")
    if values.ndim >= 1 and values.shape[0] == 1 and values.size != columns:
        values = values[0]
    mask = values.reshape(-1) > 0.5
    if mask.size != columns:
        raise ValueError("LANDMASK must cover the soil columns")
    return mask


def _varies(column: npt.NDArray[np.float64]) -> bool:
    spread = np.max(column, axis=0) - np.min(column, axis=0)
    return bool(float(np.max(spread)) > 0.0)


def _soil_profile(
    temperature: npt.NDArray[np.float64],
    moisture: npt.NDArray[np.float64],
    land: npt.NDArray[np.bool_],
) -> None:
    if temperature.shape != moisture.shape:
        raise ValueError("TSLB and SMOIS must share four soil layers")
    warm = 220.0 <= float(temperature.min()) <= float(temperature.max()) <= 330.0
    if not warm:
        raise ValueError("TSLB or SMOIS magnitude is outside the Noah-MP soil range")
    if bool(land.any()):
        land_m = moisture[:, land]
        damp = 0.0 <= float(land_m.min()) <= float(land_m.max()) <= 0.8
        if not damp:
            raise ValueError("TSLB or SMOIS magnitude is outside the Noah-MP soil range")
        if not _varies(temperature[:, land]):
            raise ValueError("TSLB must vary with depth")
        if not _varies(land_m):
            raise ValueError("SMOIS must vary with depth")
        _divided_fraction(land_m)
    water = moisture[:, ~land]
    # init_soil_2_real sets SMOIS to 1 over water for both the SST and skin-temperature paths.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_soil_pre.F#L1736
    if water.size and not bool(np.allclose(water, 1.0, rtol=0.0, atol=1e-6)):
        raise ValueError("Water SMOIS must be the Noah-MP water value")


def admit_wrfinput_soil(path: Path) -> None:
    """Require a finite Noah-MP soil column whose land depths vary."""
    with netCDF4.Dataset(str(path)) as ds:
        depths = _field(ds, "ZS")
        if depths.ndim == 2 and depths.shape[0] == 1:
            depths = depths[0]
        _noah_depths(depths.reshape(-1))
        temperature = _layers(_field(ds, "TSLB")[0], "TSLB")
        moisture = _layers(_field(ds, "SMOIS")[0], "SMOIS")
        _soil_profile(temperature, moisture, _land_mask(ds, int(temperature.shape[1])))


__all__ = ["admit_met_em", "admit_wrfinput_soil"]
