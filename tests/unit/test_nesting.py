"""Concentric nest geometry: trigger, parent shape and footprint containment."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.core import (  # noqa: E402
    computational_latlon_bounds,
    forcing_domain,
    nesting_active,
    parent_domain_shape,
    validate_resolved_topology,
    validate_science_profile,
)
from av_wrf.runner import load_bundle  # noqa: E402


def _product(*, pilot: bool = False) -> str:
    names = sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml"))
    return next(n for n in names if ("pilot" in n) == pilot)


def _nested_product() -> str:
    for name in sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml")):
        bundle = load_bundle(ROOT / "config", name)
        if nesting_active(bundle.domain, bundle.primary_source.grid_spacing_deg):
            return name
    raise AssertionError("expected a product whose primary source nests")


class TestNesting(unittest.TestCase):
    def test_gfs_triggers_nest_and_parent_covers_child_plus_halos(self) -> None:
        b = load_bundle(ROOT / "config", _product(pilot=True))
        self.assertTrue(nesting_active(b.domain, 0.25))
        self.assertFalse(nesting_active(b.domain, 0.0625))
        parent = forcing_domain(b.domain, 0.25)
        parent_we, parent_sn = parent_domain_shape(b.domain)
        self.assertEqual((parent.e_we, parent.e_sn), (parent_we, parent_sn))
        self.assertEqual(parent.dx_m, b.domain.dx_m * b.domain.parent_grid_ratio)
        self.assertEqual(parent.center_lat_deg, b.domain.center_lat_deg)
        self.assertEqual(parent.center_lon_deg, b.domain.center_lon_deg)

    def test_shallow_parent_halo_is_rejected_before_acquisition(self) -> None:
        b = load_bundle(ROOT / "config", _product(pilot=True))
        domain = dataclasses.replace(b.domain, parent_halo_cells=1)
        with self.assertRaisesRegex(ValueError, "minimum mass cells|boundary zone"):
            validate_resolved_topology(domain, b.science, b.primary_source.grid_spacing_deg)

    def test_gfs_1km_jump_requires_an_unsupported_extra_nest(self) -> None:
        b = load_bundle(
            ROOT / "config",
            next(
                name
                for name in sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml"))
                if name.endswith("1km")
            ),
        )
        with self.assertRaisesRegex(ValueError, "unsupported additional nest"):
            validate_resolved_topology(b.domain, b.science, 0.25)

    def test_shipped_source_trees_pass_resolved_topology(self) -> None:
        for path in sorted((ROOT / "config" / "products").glob("*.yaml")):
            b = load_bundle(ROOT / "config", path.stem)
            with self.subTest(product=path.stem):
                validate_resolved_topology(b.domain, b.science, b.primary_source.grid_spacing_deg)

    def test_unpublished_parent_skips_the_child_publication_crop(self) -> None:
        b = load_bundle(ROOT / "config", _product())
        domain = dataclasses.replace(b.domain, bdy_trim_cells=70)
        # The comparison source is coarse enough to resolve the unpublished parent.
        validate_resolved_topology(domain, b.science, b.candidate_source.grid_spacing_deg)

    def test_invalid_child_publication_crop_still_fails(self) -> None:
        b = load_bundle(ROOT / "config", _product())
        domain = dataclasses.replace(b.domain, bdy_trim_cells=120)
        with self.assertRaisesRegex(ValueError, "Published grid dimensions"):
            validate_resolved_topology(domain, b.science, b.primary_source.grid_spacing_deg)

    def test_unpublished_parent_still_checks_its_scaled_clock(self) -> None:
        b = load_bundle(ROOT / "config", _nested_product())
        parent = forcing_domain(b.domain, b.primary_source.grid_spacing_deg)
        scaled = dataclasses.replace(b.science, time_step_s=1000)
        with self.assertRaisesRegex(ValueError, "exceeds 6\\*dx_phys"):
            validate_science_profile(scaled, parent, published=False)

    def test_parent_contains_the_child_footprint(self) -> None:
        b = load_bundle(ROOT / "config", _product())
        parent = forcing_domain(b.domain, 0.25)
        child_w, child_s, child_e, child_n = computational_latlon_bounds(b.domain, halo_deg=0.0)
        par_w, par_s, par_e, par_n = computational_latlon_bounds(parent, halo_deg=0.0)
        self.assertLessEqual(par_w, child_w)
        self.assertLessEqual(par_s, child_s)
        self.assertGreaterEqual(par_e, child_e)
        self.assertGreaterEqual(par_n, child_n)


if __name__ == "__main__":
    unittest.main()
