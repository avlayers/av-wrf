"""Rank admission: check and plan agree on WRF-native decomposition limits."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import BuildManifest  # noqa: E402
from av_wrf.core import nesting_active  # noqa: E402
from av_wrf.runner import (  # noqa: E402
    LocalFileStore,
    check_product_config,
    load_bundle,
    plan_product_cycle,
    run_geogrid,
)

CONFIG = ROOT / "config"


class TestRankAdmission(unittest.TestCase):
    def test_all_products_plan_at_supported_rank_counts(self) -> None:
        manifest = BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m")
        for path in sorted((CONFIG / "products").glob("*.yaml")):
            bundle = load_bundle(CONFIG, path.stem)
            origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
            with TemporaryDirectory() as tmp:
                store = LocalFileStore(Path(tmp))
                for ranks in (1, 2, 4, 8, 12, 16):
                    with self.subTest(product=path.stem, ranks=ranks):
                        _, lock = plan_product_cycle(
                            CONFIG,
                            store,
                            product_id=bundle.product.product_id,
                            publish_origin_utc=origin,
                            attempt_id=f"ranks-{ranks}",
                            mpi_ranks=ranks,
                            build_manifest=manifest,
                            bundle=bundle,
                        )
                        self.assertEqual(lock.mpi_ranks, ranks)

    def test_geogrid_admits_topology_before_native_launch(self) -> None:
        bundle = next(
            load_bundle(CONFIG, path.stem)
            for path in sorted((CONFIG / "products").glob("*.yaml"))
            if nesting_active(
                load_bundle(CONFIG, path.stem).domain,
                load_bundle(CONFIG, path.stem).primary_source.grid_spacing_deg,
            )
        )
        invalid = dataclasses.replace(
            bundle, domain=dataclasses.replace(bundle.domain, parent_halo_cells=1)
        )
        with TemporaryDirectory() as tmp, patch("av_wrf.runner.solver_exec._run_native") as native:
            with self.assertRaises(ValueError):
                run_geogrid(
                    CONFIG,
                    product_id=bundle.product.product_id,
                    work_dir=Path(tmp) / "geog",
                    bundle=invalid,
                )
            native.assert_not_called()

    def test_check_rejects_oversized_rank_count(self) -> None:
        for path in sorted((CONFIG / "products").glob("*.yaml")):
            bundle = load_bundle(CONFIG, path.stem)
            with (
                self.subTest(product=path.stem),
                self.assertRaisesRegex(ValueError, "exceeds WRF's 10-cell"),
            ):
                check_product_config(CONFIG, bundle.product.product_id, 5000)


if __name__ == "__main__":
    unittest.main()
