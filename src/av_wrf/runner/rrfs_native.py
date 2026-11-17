"""Offline soil and level contract for an RRFS met_em or wrfinput."""

from __future__ import annotations

from pathlib import Path

import netCDF4
import numpy as np
import numpy.typing as npt

# Vtable endpoints in centimetres. metgrid copies those values into SOIL_LEVELS.
_SOIL_CM = (0.0, 1.0, 4.0, 10.0, 30.0, 60.0, 100.0, 160.0, 300.0)


def _dim(ds: netCDF4.Dataset, name: str, expected: int) -> None:
    if name not in ds.dimensions or len(ds.dimensions[name]) != expected:
        raise ValueError(f"{name} must be {expected}")


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
        values = [float(item) for item in ds.variables["SOIL_LEVELS"][:].ravel()]
        same = len(values) == 9 and all(
            abs(got - exp) <= 1e-6 for got, exp in zip(values, _SOIL_CM, strict=True)
        )
        if not same:
            raise ValueError("SOIL_LEVELS must be the nine RRFS depths in centimetres")


def _layers(values: object, name: str) -> npt.NDArray[np.float64]:
    column = np.asarray(values, dtype=float)
    if column.ndim < 1 or column.shape[0] != 4 or not bool(np.isfinite(column).all()):
        raise ValueError(f"{name} must be finite across four soil layers")
    return column.reshape(4, -1)


def admit_wrfinput_soil(path: Path) -> None:
    """Require a finite Noah-MP soil column whose four depths vary."""
    with netCDF4.Dataset(str(path)) as ds:
        depths = np.asarray(ds.variables["ZS"][0], dtype=float).reshape(-1)
        increasing = depths.shape == (4,) and bool(np.all(np.diff(depths) > 0))
        if not increasing or not (float(depths[0]) > 0.0 and float(depths[-1]) <= 3.0):
            raise ValueError("ZS must be four increasing Noah-MP depths within 3 m")
        temperature = _layers(ds.variables["TSLB"][0], "TSLB")
        moisture = _layers(ds.variables["SMOIS"][0], "SMOIS")
        if temperature.shape != moisture.shape:
            raise ValueError("TSLB and SMOIS must share four soil layers")
        warm = float(temperature.min()) >= 220.0 and float(temperature.max()) <= 330.0
        damp = float(moisture.min()) >= 0.0 and float(moisture.max()) <= 0.8
        if not warm or not damp:
            raise ValueError("TSLB or SMOIS magnitude is outside the Noah-MP soil range")
        if float(np.max(np.max(temperature, axis=0) - np.min(temperature, axis=0))) <= 0.0:
            raise ValueError("TSLB must vary with depth")
        if float(np.max(np.max(moisture, axis=0) - np.min(moisture, axis=0))) <= 0.0:
            raise ValueError("SMOIS must vary with depth")


__all__ = ["admit_met_em", "admit_wrfinput_soil"]
