"""Committed-cycle recovery validation without native recomputation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from av_wrf.runner import LoadedBundle, LocalFileStore, recover_committed_cycle
from av_wrf.runner.forcing_gfs import GFS
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"


def _recovery_receipt(bundle: LoadedBundle) -> SimpleNamespace:
    hour = bundle.product.publish_origin_hour_utc
    return SimpleNamespace(
        model_id=bundle.product.model_id,
        cycle_partition=f"20261112/{hour:02d}",
        publish_origin_utc=f"2026-11-12T{hour:02d}:00:00Z",
        attempt_id="gha-1-1",
        config_sha256=bundle.config_sha256,
        forcing_profile_id=bundle.primary_source.profile_id,
        oci_digest="sha256:" + "a" * 64,
    )


def test_recover_validates_inventory_and_advances_latest(tmp_path: Path) -> None:
    bundle = bundle_for_recipe(CONFIG, GFS)
    receipt = _recovery_receipt(bundle)
    with (
        patch("av_wrf.runner.recovery.load_ready_receipt", return_value=receipt),
        patch("av_wrf.runner.recovery.verify_published_objects") as verify,
        patch(
            "av_wrf.runner.recovery._advance_latest_pointer_with_retry",
            return_value="advanced",
        ) as advance,
    ):
        recovered, latest = recover_committed_cycle(
            LocalFileStore(tmp_path), bundle, receipt.cycle_partition
        )
    assert recovered.attempt_id == receipt.attempt_id
    assert latest == "advanced"
    verify.assert_called_once()
    advance.assert_called_once()


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("config_sha256", "wrong", "config differs"),
        ("forcing_profile_id", "wrong", "forcing profile differs"),
        ("oci_digest", "local", "pinned OCI digest"),
    ),
)
def test_recover_rejects_invalid_provenance(
    tmp_path: Path, field: str, value: str, match: str
) -> None:
    bundle = bundle_for_recipe(CONFIG, GFS)
    receipt = _recovery_receipt(bundle)
    setattr(receipt, field, value)
    with (
        patch("av_wrf.runner.recovery.load_ready_receipt", return_value=receipt),
        pytest.raises(ValueError, match=match),
    ):
        recover_committed_cycle(LocalFileStore(tmp_path), bundle, receipt.cycle_partition)
