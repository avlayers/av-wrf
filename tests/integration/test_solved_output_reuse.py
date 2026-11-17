"""Reuse one attempt's wrfout when a later image keeps the solver build."""

from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest

from av_wrf.contracts import BuildManifest, RunLock
from av_wrf.runner import LocalFileStore, plan_product_cycle
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.stages import verify_producer_sha256
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"
_WRF = "a" * 40
_WPS = "b" * 40
_PATCH = "c" * 64
_GEOG = "d" * 64


def _build(nibble: str) -> BuildManifest:
    return BuildManifest(
        "sha256:" + nibble * 64, _WRF, _WPS, (("surface.patch", _PATCH),), "gcc", "mpi"
    )


def _plan(tmp_path: Path) -> tuple[partial[tuple[str, RunLock]], RunLock]:
    bundle = bundle_for_recipe(CONFIG, GFS)
    product = bundle.product
    store = LocalFileStore(tmp_path)
    plan = partial(
        plan_product_cycle,
        CONFIG,
        store,
        product_id=product.product_id,
        publish_origin_utc=f"2026-11-12T{product.publish_origin_hour_utc:02d}:00:00Z",
        attempt_id="att-01",
        mpi_ranks=4,
        geog_bundle_sha256=_GEOG,
        bundle=bundle,
    )
    _key, lock = plan(build_manifest=_build("1"))
    return plan, lock


def test_plan_reuses_the_stored_lock_when_only_the_image_changes(tmp_path: Path) -> None:
    plan, lock = _plan(tmp_path)
    bundle = bundle_for_recipe(CONFIG, GFS)
    with patch("av_wrf.runner.stages._producer_sha256", return_value="f" * 64):
        replay_key, replay_lock = plan(build_manifest=_build("2"))
        with pytest.raises(FileExistsError, match="solved-output identity differs"):
            plan(build_manifest=replace(_build("2"), wrf_git_sha="e" * 40))
    assert replay_lock == lock
    assert replay_key.endswith("att-01/run.lock.json")
    assert lock.build_manifest.oci_digest == "sha256:" + "1" * 64
    with pytest.raises(FileExistsError, match="solved-output identity differs"):
        plan(build_manifest=_build("2"), geog_bundle_sha256="e" * 64)
    with pytest.raises(FileExistsError, match="solved-output identity differs"):
        plan(build_manifest=_build("2"), mpi_ranks=1)
    with pytest.raises(ValueError, match="Config bundle SHA-256 drifted"):
        plan(build_manifest=_build("2"), bundle=replace(bundle, config_sha256="a" * 64))


def test_post_replays_wrfout_when_the_installed_solver_matches(tmp_path: Path) -> None:
    _plan_fn, lock = _plan(tmp_path)
    manifest = tmp_path / "build-manifest.json"
    manifest.write_text(
        "{"
        f'"wrf_sha":"{_WRF}","wps_sha":"{_WPS}",'
        f'"patch_sha256s":[["surface.patch","{_PATCH}"]],'
        '"compiler":"gcc","mpi":"mpi"}',
        encoding="utf-8",
    )
    drifted = "0" * 64
    with (
        patch("av_wrf.runner.stages._producer_sha256", return_value=drifted),
        patch("av_wrf.runner.stages._SOLVER_MANIFEST", manifest),
    ):
        verify_producer_sha256(lock, allow_solved_replay=True)
        with pytest.raises(ValueError, match="Producer code SHA-256 drifted"):
            verify_producer_sha256(lock)
    other = tmp_path / "other.json"
    other.write_text(manifest.read_text(encoding="utf-8").replace(_WRF, "e" * 40), encoding="utf-8")
    with (
        patch("av_wrf.runner.stages._producer_sha256", return_value=drifted),
        pytest.raises(ValueError, match="Producer code SHA-256 drifted"),
    ):
        verify_producer_sha256(lock, allow_solved_replay=True, replay_manifest=other)
