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
from av_wrf.core.thermo import MIN_WATER_VAPOR_KGKG  # noqa: E402


def _write_spechumd(path: Path, value: float, *, fill: float | None = None) -> None:
    with netCDF4.Dataset(str(path), "w") as ds:
        ds.createDimension("Time", 1)
        ds.setncattr("FLAG_SH", 1)
        var = ds.createVariable("SPECHUMD", "f4", ("Time",), fill_value=fill)
        if fill is None or value != fill:
            var[:] = np.float32(value)


class TestWpsSpecificHumidity(unittest.TestCase):
    def test_floor_stays_admissible_after_real_mixing_ratio_conversion(self) -> None:
        specific = MIN_WATER_VAPOR_KGKG
        mixing = specific / (1.0 - specific)
        self.assertGreaterEqual(mixing, MIN_WATER_VAPOR_KGKG)
        self.assertLess(specific, 0.0)
        self.assertLess(math.nextafter(1.0, 0.0), 1.0)

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
