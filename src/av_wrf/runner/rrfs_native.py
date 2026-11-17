"""Offline soil and level contract for an RRFS met_em or wrfinput."""

from __future__ import annotations

from pathlib import Path

import netCDF4
import numpy as np
import numpy.typing as npt

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


def _finite(ds: netCDF4.Dataset, name: str) -> npt.NDArray[np.float64]:
    if name not in ds.variables:
        raise ValueError(f"Missing {name}")
    values = np.asarray(ds.variables[name][:], dtype=float)
    if values.size == 0 or not bool(np.isfinite(values).all()):
        raise ValueError(f"{name} must be finite")
    return values


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
    values = [float(item) for item in ds.variables["SOIL_LEVELS"][:].ravel()]
    same = len(values) == 9 and all(
        abs(got - exp) <= 1e-6 for got, exp in zip(values, _SOIL_CM, strict=True)
    )
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
            values = _finite(ds, name)
            if name == "PRES":
                _pressure(values)
            elif name == "SOILM":
                _fraction(values, name)
        for name in _SOILM:
            _fraction(_finite(ds, name), name)


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


def admit_wrfinput_soil(path: Path) -> None:
    """Require a finite Noah-MP soil column whose four depths vary."""
    with netCDF4.Dataset(str(path)) as ds:
        _noah_depths(np.asarray(ds.variables["ZS"][0], dtype=float).reshape(-1))
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
        _divided_fraction(moisture)


__all__ = ["admit_met_em", "admit_wrfinput_soil"]
