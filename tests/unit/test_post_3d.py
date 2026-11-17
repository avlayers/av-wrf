"""Published cloud cover and the true-thickness Courant proxy."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

import numpy as np
import numpy.typing as npt

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.codecs import build_synthetic_wrf_step  # noqa: E402
from av_wrf.core import RawWrfStepInput, build_accum_baseline, post_frame  # noqa: E402
from av_wrf.core.post_3d import (  # noqa: E402
    _snap_groomed_column,
    _step_below,
    crop2_f64,
    post_3d_slabs,
)
from av_wrf.runner import load_bundle  # noqa: E402


class TestPost3d(unittest.TestCase):
    def setUp(self) -> None:
        products = sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml"))
        self.bundle = load_bundle(ROOT / "config", next(p for p in products if "pilot" in p))
        self.domain = dataclasses.replace(
            self.bundle.domain,
            e_we=6,
            e_sn=5,
            e_vert=55,
            exported_levels_k=3,
            bdy_trim_cells=1,
            min_bl_levels_2km_agl=1,
        )
        self.raw: RawWrfStepInput = build_synthetic_wrf_step(
            self.domain, "2026-11-12T00:00:00Z", 1_000_000, 0
        )
        trim = self.domain.bdy_trim_cells
        self.shared = (
            crop2_f64(self.raw.terrain_m, trim, "HGT"),
            crop2_f64(self.raw.psfc_pa, trim, "PSFC"),
            crop2_f64(self.raw.cos_alpha, trim, "COSALPHA"),
            crop2_f64(self.raw.sin_alpha, trim, "SINALPHA"),
        )

    def test_selected_cloud_cover_preserves_clear_and_overcast_columns(self) -> None:
        for fraction in (0.25, 0.0, 1.0):
            with self.subTest(fraction=fraction):
                np.frombuffer(self.raw.cldfra.buf, np.float32).fill(fraction)
                _, fields, _ = post_3d_slabs(
                    self.raw, self.domain, self.bundle.science, self.shared
                )
                for name in ("cloud_total_pct", "cloud_low_pct"):
                    np.testing.assert_array_equal(
                        np.frombuffer(fields[name].buf, np.float32), fraction * 100.0
                    )

    def test_groomed_pressure_order_names_the_failing_column(self) -> None:
        pb = np.frombuffer(self.raw.pb_pa.buf, np.float32).reshape(
            self.raw.pb_pa.nz, self.raw.pb_pa.ny, self.raw.pb_pa.nx
        )
        trim = self.domain.bdy_trim_cells
        j = i = trim
        saved = float(pb[0, j, i]), float(pb[1, j, i])
        cases = (
            (
                0,
                np.float32(89_000.0),
                "lowest level >= surface",
                "89000.000 Pa vs 88000.000 Pa, gap 1000.000 Pa",
            ),
        )
        for level, value, comparison, pressures in cases:
            with self.subTest(comparison=comparison):
                pb[0, j, i], pb[1, j, i] = saved
                pb[level, j, i] = value
                with self.assertRaisesRegex(ValueError, comparison) as caught:
                    post_3d_slabs(self.raw, self.domain, self.bundle.science, self.shared, lead_h=3)
                message = str(caught.exception)
                self.assertIn(self.raw.valid_time_utc, message)
                self.assertIn("lead_h=3", message)
                self.assertIn(f"wrfout_y={j} wrfout_x={i}", message)
                self.assertIn(pressures, message)

    def _published_pressure(self) -> npt.NDArray[np.float32]:
        slabs, _, _ = post_3d_slabs(
            self.raw, self.domain, self.bundle.science, self.shared, lead_h=71
        )
        ny, nx = self.shared[1].shape
        return np.frombuffer(slabs["pressure_pa"].buf, np.float32).reshape(
            self.domain.exported_levels_k, ny, nx
        )

    def test_one_quantum_groomed_cross_steps_below_the_lower_value(self) -> None:
        low = _step_below(np.array([76084.5, 65536.0, 76084.6], dtype=np.float32))
        self.assertEqual(
            (float(low[0]), float(low[1]), float(low[2])), (76084.0, 65535.75, 76084.0)
        )
        trim = self.domain.bdy_trim_cells
        pb = np.frombuffer(self.raw.pb_pa.buf, np.float32).reshape(
            self.raw.pb_pa.nz, self.raw.pb_pa.ny, self.raw.pb_pa.nx
        )
        self.shared[1][0, 0] = np.float32(87_799.5)
        surface = self._published_pressure()
        self.assertEqual(float(surface[0, 0, 0]), 87_799.0)
        pb[1, trim, trim] = np.float32(87_799.8)
        self.shared[1][0, 0] = np.float32(88_000.0)
        levels = self._published_pressure()
        self.assertEqual(float(levels[1, 0, 0]), 87_799.5)
        self.assertTrue(bool(np.all(levels[1:] < levels[:-1])))
        pb[0, trim, trim] = np.float32(87_799.7)
        pb[1, trim, trim] = np.float32(87_799.4)
        self.shared[1][0, 0] = np.float32(87_799.0)
        cascaded = self._published_pressure()
        self.assertEqual((float(cascaded[0, 0, 0]), float(cascaded[1, 0, 0])), (87_798.5, 87_798.0))
        self.assertTrue(bool(np.all(cascaded[0] < self.shared[1])))
        self.assertTrue(bool(np.all(cascaded[1:] < cascaded[:-1])))

    def test_few_pascal_surface_gap_steps_below_psfc(self) -> None:
        column = np.array([[[76_161.0]], [[75_500.0]], [[70_000.0]]], dtype=np.float32)
        _snap_groomed_column(
            self.raw,
            column,
            np.array([[76_158.0]]),
            self.domain.bdy_trim_cells,
            72,
            self.domain.dzbot_m,
        )
        self.assertEqual(float(column[0, 0, 0]), 76_157.5)
        self.assertTrue(bool(np.all(column[1:] < column[:-1])))
        self.assertLess(float(column[0, 0, 0]), 76_158.0)

    def test_groomed_level_fold_names_the_column(self) -> None:
        folded = np.array([[[88_000.0]], [[89_000.0]]], dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "level 1 >= level 0") as caught:
            _snap_groomed_column(
                self.raw, folded, np.array([[90_000.0]]), 1, 3, self.domain.dzbot_m
            )
        self.assertIn("89000.000 Pa vs 88000.000 Pa, gap 1000.000 Pa", str(caught.exception))

    def test_pressure_validation_includes_levels_above_exported_column(self) -> None:
        pb = np.frombuffer(self.raw.pb_pa.buf, np.float32).reshape(
            self.raw.pb_pa.nz, self.raw.pb_pa.ny, self.raw.pb_pa.nx
        )
        for value, message in ((0.0, "Non-positive"), (88_000.0, "Non-monotonic")):
            with self.subTest(value=value):
                pb[-1].fill(value)
                with self.assertRaisesRegex(ValueError, message):
                    post_3d_slabs(self.raw, self.domain, self.bundle.science, self.shared)

    def test_sampled_timestep_and_column_diagnostics(self) -> None:
        _, f2d, (min_dz, max_dz, bl, cfl) = post_3d_slabs(
            self.raw, self.domain, self.bundle.science, self.shared
        )
        _, _, (_, _, _, half_cfl) = post_3d_slabs(
            dataclasses.replace(self.raw, dt_s=7.5),
            self.domain,
            self.bundle.science,
            self.shared,
        )
        self.assertAlmostEqual(half_cfl * 2, cfl, places=4)
        self.assertIn("mslp_pa", f2d)
        self.assertGreater(min_dz, 0.0)
        self.assertGreaterEqual(max_dz, min_dz)
        self.assertGreaterEqual(bl, 1)
        self.assertGreater(cfl, 0.0)

    def test_post_rejects_invalid_faces_even_with_increasing_midpoints_and_zero_w(self) -> None:
        raw, domain = self.raw, self.domain
        shape = (raw.phb_stag_m2s2.nz, raw.phb_stag_m2s2.ny, raw.phb_stag_m2s2.nx)
        phb = np.frombuffer(raw.phb_stag_m2s2.buf, np.float32).reshape(shape)
        terrain = np.frombuffer(raw.terrain_m.buf, np.float32).reshape(shape[1:])
        np.frombuffer(raw.ph_stag_m2s2.buf, np.float32).fill(0.0)
        np.frombuffer(raw.w_stag_mps.buf, np.float32).fill(0.0)
        baseline = build_accum_baseline(
            dataclasses.replace(raw, valid_time_unix_s=raw.valid_time_unix_s - 3600),
            domain.bdy_trim_cells,
        )
        for face2_m in (120.0, 50.0, 60.0, float("nan"), float("inf")):
            with self.subTest(face2_m=face2_m):
                faces = np.asarray((0.0, 60.0, face2_m, 160.0))
                phb[:4] = (terrain[None, :, :] + faces[:, None, None]) * 9.81
                if face2_m == 120.0:
                    post_frame(baseline, raw, domain, self.bundle.science)
                else:
                    # Both folded cases have valid increasing midpoints above 20 m AGL.
                    if np.isfinite(face2_m):
                        self.assertTrue(np.all(np.diff((faces[1:] + faces[:-1]) / 2) > 0))
                    with self.assertRaisesRegex(ValueError, "geopotential"):
                        post_frame(baseline, raw, domain, self.bundle.science)


if __name__ == "__main__":
    unittest.main()
