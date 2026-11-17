"""Check the RRFS met_em and wrfinput soil contract without running WRF."""

from __future__ import annotations

from pathlib import Path

import netCDF4
import numpy as np
import pytest

from av_wrf.runner.rrfs_native import admit_met_em, admit_wrfinput_soil

_SOIL_CM = (0.0, 1.0, 4.0, 10.0, 30.0, 60.0, 100.0, 160.0, 300.0)


def _met_em(
    path: Path,
    *,
    levels: int = 46,
    slp: int = 1,
    soil: tuple[float, ...] = _SOIL_CM,
    top_pa: float = 200.0,
    low_pa: float = 100_000.0,
    soilm: float = 0.2,
    omit: str | None = None,
) -> None:
    with netCDF4.Dataset(str(path), "w") as ds:
        ds.createDimension("num_metgrid_levels", levels)
        ds.createDimension("num_soilt_levels", len(soil))
        ds.createDimension("num_soilm_levels", len(soil))
        ds.setncattr("NUM_METGRID_SOIL_LEVELS", len(soil))
        ds.setncattr("FLAG_SOIL_LEVELS", 1)
        ds.setncattr("FLAG_SH", 1)
        ds.setncattr("FLAG_SLP", slp)
        ds.createVariable("SOIL_LEVELS", "f4", ("num_soilt_levels",))[:] = np.asarray(
            soil, dtype=np.float32
        )
        pressure = np.full(levels, 50_000.0, dtype=np.float32)
        if levels >= 2:
            pressure[0], pressure[-1] = top_pa, low_pa
        names = (
            "GHT",
            "TT",
            "SPECHUMD",
            "UU",
            "VV",
            "PMSL",
            "SOILT",
            *(f"SOILM{int(cm):03d}" for cm in _SOIL_CM),
        )
        if omit != "PRES":
            ds.createVariable("PRES", "f4", ("num_metgrid_levels",))[:] = pressure
        if omit != "SOILM":
            ds.createVariable("SOILM", "f4", ("num_soilm_levels",))[:] = soilm
        for name in names:
            if name == omit:
                continue
            if name.startswith("SOILM"):
                value = soilm
            elif name == "SPECHUMD":
                value = 0.01
            else:
                value = 280.0
            ds.createVariable(name, "f4", ())[()] = value


def _wrfinput(
    path: Path,
    *,
    temperature: tuple[float, ...] = (280, 282, 285, 288),
    moisture: tuple[float, ...] = (0.2, 0.25, 0.3, 0.22),
    depths: tuple[float, ...] = (0.05, 0.25, 0.7, 1.5),
    water: bool = False,
) -> None:
    with netCDF4.Dataset(str(path), "w") as ds:
        ds.createDimension("Time", 1)
        ds.createDimension("soil_layers_stag", 4)
        ds.createDimension("south_north", 1)
        width = 2 if water else 1
        ds.createDimension("west_east", width)
        dims = ("Time", "soil_layers_stag", "south_north", "west_east")
        ds.createVariable("ZS", "f4", ("Time", "soil_layers_stag"))[0, :] = depths
        soil_t = np.zeros((4, 1, width), dtype=np.float32)
        soil_m = np.zeros((4, 1, width), dtype=np.float32)
        soil_t[:, 0, 0] = temperature
        soil_m[:, 0, 0] = moisture
        land = np.ones((1, width), dtype=np.float32)
        if water:
            # Noah-MP writes a constant SST and SMOIS of 1 on water cells.
            soil_t[:, 0, 1] = 285.0
            soil_m[:, 0, 1] = 1.0
            land[0, 1] = 0.0
        ds.createVariable("TSLB", "f4", dims)[0, :, :, :] = soil_t
        ds.createVariable("SMOIS", "f4", dims)[0, :, :, :] = soil_m
        ds.createVariable("LANDMASK", "f4", ("Time", "south_north", "west_east"))[0, :, :] = land


def test_met_em_accepts_the_nine_centimetre_depths(tmp_path: Path) -> None:
    path = tmp_path / "met_em.nc"
    _met_em(path)
    admit_met_em(path)
    _met_em(path, levels=45)
    with pytest.raises(ValueError, match="num_metgrid_levels"):
        admit_met_em(path)
    _met_em(path, slp=0)
    with pytest.raises(ValueError, match="FLAG_SLP"):
        admit_met_em(path)
    _met_em(path, soil=tuple(value / 100 for value in _SOIL_CM))
    with pytest.raises(ValueError, match="centimetres"):
        admit_met_em(path)
    _met_em(path, omit="PMSL")
    with pytest.raises(ValueError, match="Missing PMSL"):
        admit_met_em(path)
    _met_em(path, top_pa=8_000.0)
    with pytest.raises(ValueError, match="5000 Pa"):
        admit_met_em(path)
    _met_em(path, low_pa=90_000.0)
    with pytest.raises(ValueError, match="1000 hPa"):
        admit_met_em(path)
    _met_em(path, soilm=1.2)
    with pytest.raises(ValueError, match="SOILM"):
        admit_met_em(path)


def test_met_em_accepts_the_deepest_first_soil_slab(tmp_path: Path) -> None:
    path = tmp_path / "slab.nc"
    _met_em(path)
    with netCDF4.Dataset(str(path), "a") as ds:
        ds.renameVariable("SOIL_LEVELS", "flat")
        ds.createDimension("Time", 1)
        ds.createDimension("south_north", 2)
        ds.createDimension("west_east", 3)
        # metgrid writes the deeper level at the first vertical index.
        levels = np.asarray(_SOIL_CM[::-1], dtype=np.float32).reshape(1, 9, 1, 1)
        slab = np.broadcast_to(levels, (1, 9, 2, 3)).copy()
        dims = ("Time", "num_soilt_levels", "south_north", "west_east")
        ds.createVariable("SOIL_LEVELS", "f4", dims)[:] = slab
    admit_met_em(path)
    with netCDF4.Dataset(str(path), "a") as ds:
        ds.variables["SOIL_LEVELS"][0, 0, 1, 2] = 1.0
    with pytest.raises(ValueError, match="horizontally constant"):
        admit_met_em(path)


def test_wrfinput_requires_varying_finite_soil(tmp_path: Path) -> None:
    path = tmp_path / "wrfinput.nc"
    _wrfinput(path)
    admit_wrfinput_soil(path)
    _wrfinput(path, temperature=(280, 280, 280, 280))
    with pytest.raises(ValueError, match="TSLB must vary"):
        admit_wrfinput_soil(path)
    _wrfinput(path, moisture=(0.2, 0.2, 0.2, 0.2))
    with pytest.raises(ValueError, match="SMOIS must vary"):
        admit_wrfinput_soil(path)
    _wrfinput(path, moisture=(0.9, 0.95, 1.1, 1.2))
    with pytest.raises(ValueError, match="magnitude"):
        admit_wrfinput_soil(path)
    _wrfinput(path, depths=(0.1, 0.4, 1.0, 3.0))
    with pytest.raises(ValueError, match="Noah-MP centers"):
        admit_wrfinput_soil(path)
    _wrfinput(path, moisture=(0.30, 0.075, 0.03, 0.01))
    with pytest.raises(ValueError, match="centimetre depth"):
        admit_wrfinput_soil(path)


def test_wrfinput_checks_land_columns_and_keeps_water(tmp_path: Path) -> None:
    path = tmp_path / "wrfinput.nc"
    _wrfinput(path, water=True)
    admit_wrfinput_soil(path)
    _wrfinput(path, water=True, moisture=(0.9, 0.95, 1.1, 1.2))
    with pytest.raises(ValueError, match="magnitude"):
        admit_wrfinput_soil(path)
    _wrfinput(path, water=True)
    with netCDF4.Dataset(str(path), "a") as ds:
        ds.variables["SMOIS"][0, :, 0, 1] = np.float32(0.2)
    with pytest.raises(ValueError, match="Noah-MP water value"):
        admit_wrfinput_soil(path)
    _wrfinput(path, water=True)
    with netCDF4.Dataset(str(path), "a") as ds:
        ds.variables["SMOIS"][0, 0, 0, 0] = np.float32("nan")
    with pytest.raises(ValueError, match="finite"):
        admit_wrfinput_soil(path)
    _wrfinput(path, water=True)
    _mask_cell(path, "TSLB")
    with pytest.raises(ValueError, match="masked cell"):
        admit_wrfinput_soil(path)


def _rewrite(path: Path, name: str, *, fill: float, masked: bool) -> None:
    with netCDF4.Dataset(str(path), "a") as ds:
        dims = ds.variables[name].dimensions
        ds.renameVariable(name, f"_{name}_raw")
        var = ds.createVariable(name, "f4", dims, fill_value=np.float32(fill))
        var[:] = np.ma.masked if masked else np.float32(fill)


def _mask_cell(path: Path, name: str) -> None:
    _rewrite(path, name, fill=9.969209968386869e36, masked=True)


def test_met_em_rejects_masked_cells_and_fill_sentinels(tmp_path: Path) -> None:
    path = tmp_path / "masked.nc"
    _met_em(path)
    admit_met_em(path)
    _mask_cell(path, "TT")
    with pytest.raises(ValueError, match="masked cell"):
        admit_met_em(path)
    _met_em(path)
    with netCDF4.Dataset(str(path), "a") as ds:
        ds.variables["TT"][()] = np.float32(1.0e36)
    with pytest.raises(ValueError, match="fill sentinel"):
        admit_met_em(path)
    _met_em(path)
    _rewrite(path, "TT", fill=-9999.0, masked=False)
    with pytest.raises(ValueError, match="masked cell"):
        admit_met_em(path)
    _met_em(path)
    with netCDF4.Dataset(str(path), "a") as ds:
        ds.variables["TT"][()] = np.float32("nan")
    with pytest.raises(ValueError, match="finite"):
        admit_met_em(path)
    _met_em(path)
    _mask_cell(path, "SOILT")
    with pytest.raises(ValueError, match="masked cell"):
        admit_met_em(path)
