"""Unit tests for av_wrf.core.namelist verifying WRF v4.8 Registry placement and types."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import BuildManifest, RunLock, ScienceProfile  # noqa: E402
from av_wrf.core import (  # noqa: E402
    compile_geogrid_namelist_sections,
    compile_wps_namelist_sections,
    compile_wrf_namelist_sections,
    compile_wrf_run_namelist,
    native_decomposition,
    render_fortran_namelist,
    required_forcing_forecast_hours,
)
from av_wrf.runner import LoadedBundle, load_bundle  # noqa: E402

PROFILE_TO_NAMELIST_KEY: dict[str, tuple[str, str]] = {
    "history_interval_min": ("time_control", "history_interval"),
    "time_step_s": ("domains", "time_step"),
    "mp_physics": ("physics", "mp_physics"),
    "cu_physics": ("physics", "cu_physics"),
    "ra_lw_physics": ("physics", "ra_lw_physics"),
    "ra_sw_physics": ("physics", "ra_sw_physics"),
    "radt_min": ("physics", "radt"),
    "slope_rad": ("physics", "slope_rad"),
    "topo_shading": ("physics", "topo_shading"),
    "shadlen_m": ("physics", "shadlen"),
    "sf_sfclay_physics": ("physics", "sf_sfclay_physics"),
    "sf_surface_physics": ("physics", "sf_surface_physics"),
    "bl_pbl_physics": ("physics", "bl_pbl_physics"),
    "bl_mynn_edmf": ("physics", "bl_mynn_edmf"),
    "topo_wind": ("physics", "topo_wind"),
    "icloud": ("physics", "icloud"),
    "icloud_bl": ("physics", "icloud_bl"),
    "sf_lake_physics": ("physics", "sf_lake_physics"),
    "use_lakedepth": ("physics", "use_lakedepth"),
    "lakedepth_default_m": ("physics", "lakedepth_default"),
    "lake_min_elev_m": ("physics", "lake_min_elev"),
    "ysu_topdown_pblmix": ("physics", "ysu_topdown_pblmix"),
    "shinhong_scu_mixing": ("physics", "shinhong_scu_mixing"),
    "shinhong_nonlocal_flux": ("physics", "shinhong_nonlocal_flux"),
    "bucket_mm": ("physics", "bucket_mm"),
    "opt_runsrf": ("noah_mp", "opt_runsrf"),
    "opt_runsub": ("noah_mp", "opt_runsub"),
    "use_theta_m": ("dynamics", "use_theta_m"),
    "base_temp_k": ("dynamics", "base_temp"),
    "hybrid_opt": ("dynamics", "hybrid_opt"),
    "etac": ("dynamics", "etac"),
    "diff_opt": ("dynamics", "diff_opt"),
    "km_opt": ("dynamics", "km_opt"),
    "diff_6th_opt": ("dynamics", "diff_6th_opt"),
    "diff_6th_factor": ("dynamics", "diff_6th_factor"),
    "diff_6th_slopeopt": ("dynamics", "diff_6th_slopeopt"),
    "diff_6th_thresh": ("dynamics", "diff_6th_thresh"),
    "epssm": ("dynamics", "epssm"),
    "w_damping": ("dynamics", "w_damping"),
    "damp_opt": ("dynamics", "damp_opt"),
    "zdamp_m": ("dynamics", "zdamp"),
    "dampcoef": ("dynamics", "dampcoef"),
    "gwd_opt": ("dynamics", "gwd_opt"),
    "gwd_if_nonhyd": ("dynamics", "gwd_if_nonhyd"),
    "use_adaptive_time_step": ("domains", "use_adaptive_time_step"),
    "target_cfl": ("domains", "target_cfl"),
    "target_hcfl": ("domains", "target_hcfl"),
    "min_time_step_s": ("domains", "min_time_step"),
    "max_time_step_s": ("domains", "max_time_step"),
    "afwa_diag_opt": ("afwa", "afwa_diag_opt"),
    "afwa_buoy_opt": ("afwa", "afwa_buoy_opt"),
    "spec_bdy_width": ("bdy_control", "spec_bdy_width"),
    "spec_zone": ("bdy_control", "spec_zone"),
    "relax_zone": ("bdy_control", "relax_zone"),
}


def _first_product() -> str:
    return sorted(path.stem for path in (ROOT / "config" / "products").glob("*.yaml"))[0]


def _run_lock(bundle: LoadedBundle, mpi_ranks: int) -> RunLock:
    return RunLock(
        product_id=bundle.product.product_id,
        model_id=bundle.product.model_id,
        cycle_partition="20261112/00",
        attempt_id="att-01",
        forcing_cycle_utc="2026-11-11T18:00:00Z",
        wrf_init_utc="2026-11-11T18:00:00Z",
        publish_origin_utc="2026-11-12T00:00:00Z",
        spinup_hours=6,
        lead_hours=(1, 2, 16),
        forcing_profile_id=bundle.primary_source.profile_id,
        domain_profile_id=bundle.domain.profile_id,
        science_profile_id=bundle.science.profile_id,
        output_profile_id=bundle.output.profile_id,
        config_sha256="abc",
        geog_bundle_sha256="def",
        build_manifest=BuildManifest("sha256:1", "w", "p", (), "gf", "mpi"),
        mpi_ranks=mpi_ranks,
    )


class TestNamelist(unittest.TestCase):
    def test_exhaustive_science_profile_real_exe_fields_and_wrf_48_guards(self) -> None:
        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        bundle = load_bundle(ROOT / "config", prod_id)
        dom, sci, src, cand = (
            bundle.domain,
            bundle.science,
            bundle.primary_source,
            bundle.candidate_source,
        )
        single_dom = dataclasses.replace(
            dom, parent_grid_ratio=1, parent_halo_cells=0, nest_trigger_ratio=20.0
        )

        all_fields = {f.name for f in dataclasses.fields(ScienceProfile)}
        self.assertEqual(
            all_fields - {"profile_id", "wrf_version", "wps_version"},
            set(PROFILE_TO_NAMELIST_KEY.keys()),
        )
        compiled = compile_wrf_namelist_sections(single_dom, sci, src)
        for attr, (sec, key) in PROFILE_TO_NAMELIST_KEY.items():
            self.assertEqual(compiled[sec][key], getattr(sci, attr))
        self.assertEqual(compiled["time_control"]["interval_seconds"], src.interval_seconds)
        cand_compiled = compile_wrf_namelist_sections(single_dom, sci, cand)
        self.assertEqual(cand_compiled["time_control"]["interval_seconds"], cand.interval_seconds)
        self.assertNotIn("prec_acc_dt", compiled["time_control"])
        self.assertIsInstance(compiled["physics"]["shinhong_scu_mixing"], bool)
        self.assertEqual(compiled["afwa"]["afwa_buoy_opt"], 1)

        override = compile_wrf_namelist_sections(
            single_dom, sci, src, metgrid_levels=38, metgrid_soil_levels=4
        )
        self.assertEqual(override["domains"]["num_metgrid_levels"], 38)
        with self.assertRaisesRegex(ValueError, "at least two"):
            compile_wrf_namelist_sections(single_dom, sci, src, metgrid_soil_levels=1)

        for bad in (
            dataclasses.replace(sci, opt_runsrf=5),
            dataclasses.replace(sci, gwd_opt=1, gwd_if_nonhyd=True),
            dataclasses.replace(sci, shinhong_scu_mixing=True, shinhong_nonlocal_flux=True),
            dataclasses.replace(sci, km_opt=5, bl_pbl_physics=0, sf_sfclay_physics=2),
            dataclasses.replace(sci, km_opt=5),
            dataclasses.replace(sci, bl_pbl_physics=10, sf_sfclay_physics=1),
            dataclasses.replace(sci, bl_pbl_physics=1, sf_sfclay_physics=10),
            dataclasses.replace(sci, time_step_s=24),
            dataclasses.replace(sci, afwa_buoy_opt=0),
            dataclasses.replace(sci, base_temp_k=100.0),
            dataclasses.replace(sci, hybrid_opt=0),
            dataclasses.replace(sci, history_interval_min=45),
            dataclasses.replace(sci, wrf_version="99"),
        ):
            with self.assertRaises(ValueError):
                compile_wrf_namelist_sections(single_dom, bad, src)

        with self.assertRaises(ValueError):
            compile_wrf_namelist_sections(dataclasses.replace(single_dom, e_we=100), sci, src)

        lock = RunLock(
            product_id="wrf-synth-3km",
            model_id="wrf-synth-3km",
            cycle_partition="20261112/00",
            attempt_id="att-01",
            forcing_cycle_utc="2026-11-11T18:00:00Z",
            wrf_init_utc="2026-11-11T18:00:00Z",
            publish_origin_utc="2026-11-12T00:00:00Z",
            spinup_hours=6,
            lead_hours=(1, 2, 16),
            forcing_profile_id=src.profile_id,
            domain_profile_id=dom.profile_id,
            science_profile_id=sci.profile_id,
            output_profile_id=bundle.output.profile_id,
            config_sha256="abc",
            geog_bundle_sha256="def",
            build_manifest=BuildManifest("sha256:1", "w", "p", (), "gf", "mpi"),
            mpi_ranks=4,
        )
        custom_dom = dataclasses.replace(dom, dem_source="cop_glo30", landuse_source="modis_15s")
        wps_3h = compile_wps_namelist_sections(custom_dom, sci, lock, cand)
        self.assertEqual(
            wps_3h["share"]["end_date"], ("2026-11-12_18:00:00", "2026-11-11_18:00:00")
        )
        self.assertEqual(wps_3h["share"]["interval_seconds"], 10_800)
        geog_res = wps_3h["geogrid"]["geog_data_res"]
        self.assertEqual(
            geog_res[0] if isinstance(geog_res, tuple) else geog_res,
            "modis_15s+cop_glo30+modis_lai+30s+default",
        )
        self.assertEqual(required_forcing_forecast_hours(lock, src), tuple(range(23)))
        self.assertEqual(required_forcing_forecast_hours(lock, cand), tuple(range(0, 25, 3)))
        real = compile_wrf_run_namelist(dom, sci, lock, cand, for_real=True)["time_control"]
        wrf = compile_wrf_run_namelist(dom, sci, lock, cand)["time_control"]
        self.assertEqual(real["end_hour"], (18, 18))
        self.assertEqual(wrf["end_hour"], (16, 16))
        self.assertEqual(wrf["run_hours"], 22)
        self.assertEqual(wrf["history_interval"], (60, 60))
        self.assertEqual(wrf["history_begin_h"], (23, 6))
        self.assertNotIn("iofields_filename", real)
        self.assertEqual(wrf["iofields_filename"], ("mynn-history.txt",) * 2)
        uncoupled = compile_wrf_run_namelist(
            dom, dataclasses.replace(sci, icloud_bl=0), lock, cand
        )["time_control"]
        self.assertNotIn("iofields_filename", uncoupled)

        wrf_txt = render_fortran_namelist(compile_wrf_run_namelist(dom, sci, lock, src))
        self.assertNotIn("nwp_diagnostics", wrf_txt)
        self.assertNotIn("afwa_severe_opt", wrf_txt)
        self.assertNotIn("bucket_J", wrf_txt)
        self.assertIn("shinhong_scu_mixing = .false.,", wrf_txt)
        self.assertIn("afwa_buoy_opt = 1, 1,", wrf_txt)
        self.assertIn("&namelist_quilt\n nio_tasks_per_group = 0,\n nio_groups = 1,", wrf_txt)
        self.assertNotIn("prec_acc_dt", wrf_txt)

    def test_custom_geography_resolution_precedes_default_for_each_field(self) -> None:
        config = ROOT / "config"
        dom = load_bundle(config, next((config / "products").glob("*.yaml")).stem).domain
        for landuse, dem in (("custom", "default"), ("default", "custom"), ("custom", "custom")):
            domain = dataclasses.replace(dom, landuse_source=landuse, dem_source=dem)
            sections = compile_geogrid_namelist_sections(domain)
            self.assertEqual(sections["geogrid"]["geog_data_res"], "custom+modis_lai+30s+default")
            self.assertNotIn("start_date", sections["share"])

    def test_wide_lambert_grid_limits_timestep_at_its_corners(self) -> None:
        product = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        bundle = load_bundle(ROOT / "config", product)
        domain = dataclasses.replace(
            bundle.domain,
            e_we=401,
            e_sn=101,
            dx_m=12_000,
            dy_m=12_000,
            center_lat_deg=55.0,
            std_parallel_1_deg=54.0,
            std_parallel_2_deg=56.0,
            parent_grid_ratio=1,
            parent_halo_cells=0,
        )
        with self.assertRaisesRegex(ValueError, "exceeds 6\\*dx_phys"):
            compile_wrf_namelist_sections(
                domain, dataclasses.replace(bundle.science, time_step_s=71), bundle.primary_source
            )

    def test_source_aware_compiler_derives_the_topology_it_emits(self) -> None:
        bundle = load_bundle(ROOT / "config", _first_product())
        nested = compile_wrf_namelist_sections(bundle.domain, bundle.science, bundle.primary_source)
        self.assertEqual(nested["domains"]["max_dom"], 2)
        self.assertIsInstance(nested["domains"]["e_we"], tuple)
        single_domain = dataclasses.replace(
            bundle.domain, parent_grid_ratio=1, parent_halo_cells=0, nest_trigger_ratio=20.0
        )
        single = compile_wrf_namelist_sections(single_domain, bundle.science, bundle.primary_source)
        self.assertEqual(single["domains"]["max_dom"], 1)
        self.assertIsInstance(single["domains"]["e_we"], int)

    def test_native_decomposition_matches_wrf_default_mesh(self) -> None:
        for ranks, mesh in {
            1: (1, 1),
            2: (1, 2),
            3: (1, 3),
            4: (2, 2),
            8: (2, 4),
            12: (3, 4),
            16: (4, 4),
        }.items():
            with self.subTest(ranks=ranks):
                self.assertEqual(native_decomposition(ranks), mesh)
        with self.assertRaisesRegex(ValueError, ">= 1"):
            native_decomposition(0)

    def test_every_product_compiles_for_real_and_integration_at_two_rank_counts(self) -> None:
        for path in sorted((ROOT / "config" / "products").glob("*.yaml")):
            bundle = load_bundle(ROOT / "config", path.stem)
            for ranks in (1, 8):
                lock = _run_lock(bundle, ranks)
                for for_real in (True, False):
                    with self.subTest(product=path.stem, ranks=ranks, for_real=for_real):
                        sections = compile_wrf_run_namelist(
                            bundle.domain,
                            bundle.science,
                            lock,
                            bundle.primary_source,
                            for_real=for_real,
                        )
                        self.assertIn("domains", sections)


if __name__ == "__main__":
    unittest.main()
