"""WPS field bounds for metgrid specific-humidity undershoot."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import netCDF4
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.codecs.wrf_nc_grid import verify_wps_fields  # noqa: E402
from av_wrf.core import MIN_WATER_VAPOR_KGKG  # noqa: E402


def _write_spechumd(
    path: Path,
    value: float,
    *,
    fill: float | None = None,
    dtype: str = "f4",
    soil: float = 0.2,
) -> None:
    with netCDF4.Dataset(str(path), "w") as ds:
        ds.createDimension("Time", 1)
        ds.setncattr("FLAG_SH", 1)
        ds.setncattr("FLAG_SOIL_LEVELS", 1)
        var = ds.createVariable("SPECHUMD", dtype, ("Time",), fill_value=fill)
        if fill is None or value != fill:
            var[:] = np.float32(value) if dtype == "f4" else value
        ds.createVariable("SOILM", "f4", ("Time",))[:] = np.float32(soil)


class TestWpsSpecificHumidity(unittest.TestCase):
    def test_float32_floor_stays_inside_the_post_check(self) -> None:
        specific = np.float32(MIN_WATER_VAPOR_KGKG)
        self.assertGreaterEqual(float(specific), MIN_WATER_VAPOR_KGKG)
        mixing = np.float32(specific / (np.float32(1.0) - specific))
        self.assertFalse(float(mixing) < MIN_WATER_VAPOR_KGKG)
        self.assertLess(float(np.nextafter(specific, np.float32(-1.0))), MIN_WATER_VAPOR_KGKG)

    def test_admits_the_observed_parent_cell_and_rejects_past_the_floor(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "met_em.nc"
            _write_spechumd(path, -4.042565342388116e-05)
            with netCDF4.Dataset(str(path)) as ds:
                verify_wps_fields(ds, path, ("SPECHUMD",))
            _write_spechumd(path, -2.0e-4)
            with netCDF4.Dataset(str(path)) as ds, self.assertRaisesRegex(ValueError, "outside"):
                verify_wps_fields(ds, path, ("SPECHUMD",))
            _write_spechumd(path, 1.0)
            with netCDF4.Dataset(str(path)) as ds, self.assertRaisesRegex(ValueError, "outside"):
                verify_wps_fields(ds, path, ("SPECHUMD",))
            _write_spechumd(path, -9999.0, fill=-9999.0)
            with netCDF4.Dataset(str(path)) as ds, self.assertRaisesRegex(ValueError, "masked"):
                verify_wps_fields(ds, path, ("SPECHUMD",))
            _write_spechumd(path, MIN_WATER_VAPOR_KGKG, dtype="f8")
            with netCDF4.Dataset(str(path)) as ds:
                verify_wps_fields(ds, path, ("SPECHUMD",))
            _write_spechumd(path, math.nextafter(MIN_WATER_VAPOR_KGKG, -1.0), dtype="f8")
            with netCDF4.Dataset(str(path)) as ds, self.assertRaisesRegex(ValueError, "outside"):
                verify_wps_fields(ds, path, ("SPECHUMD",))
            _write_spechumd(path, 0.005, soil=-4.05e-5)
            with (
                netCDF4.Dataset(str(path)) as ds,
                self.assertRaisesRegex(ValueError, "Invalid values in WPS field SOILM"),
            ):
                verify_wps_fields(ds, path, ("SOILM",))
