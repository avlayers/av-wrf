"""Committed-cycle recovery validation without native recomputation."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from av_wrf.cli import load_runtime_env, main
from av_wrf.codecs import build_synthetic_wrf_baseline, build_synthetic_wrf_step
from av_wrf.contracts import (
    BuildManifest,
    CompMass2D,
    RawWrfStepInput,
    ReadyReceipt,
    Slab2DF32,
    SolverReceipt,
    parse_iso_utc,
)
from av_wrf.core import resolve_cycle_window
from av_wrf.runner import (
    LoadedBundle,
    LocalFileStore,
    json_bytes,
    load_bundle,
    plan_product_cycle,
    publish_cycle_receipt,
    published_cycle_prefix,
    put_idempotent,
    recover_committed_cycle,
    solver_receipt_key,
)
from av_wrf.runner.post import _stage_cycle
from tests.integration import stage_sparse_config

CONFIG = Path(__file__).resolve().parents[2] / "config"


def _published_fixture(
    tmp_path: Path,
) -> tuple[Path, LoadedBundle, LocalFileStore, str, ReadyReceipt]:
    cfg = tmp_path / "cfg"
    product_id = stage_sparse_config(CONFIG, cfg)
    bundle = load_bundle(cfg, product_id)
    store = LocalFileStore(tmp_path / "store")
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    manifest = BuildManifest("sha256:" + "b" * 64, "wrf-sha", "wps-sha", (), "gcc", "openmpi")
    window = resolve_cycle_window(bundle.product, bundle.output, origin)
    _, lock = plan_product_cycle(
        cfg,
        store,
        product_id=product_id,
        publish_origin_utc=origin,
        attempt_id="att-recover",
        mpi_ranks=4,
        build_manifest=manifest,
        geog_bundle_sha256="a" * 64,
        bundle=bundle,
    )
    solver = SolverReceipt(
        product_id,
        window.cycle_partition,
        "att-recover",
        bundle.config_sha256,
        manifest.oci_digest,
        "a" * 64,
        (("gfs.f000", "f0"),),
        (("wrfout_d01_00", "w0"),),
    )
    put_idempotent(
        store,
        solver_receipt_key(product_id, window.cycle_partition, "att-recover"),
        json_bytes(solver.to_doc()),
    )
    base_unix = int(parse_iso_utc(origin).timestamp())
    ny, nx = bundle.domain.e_sn - 1, bundle.domain.e_we - 1

    def stream_leads() -> Iterator[RawWrfStepInput]:
        for hour, valid in zip(window.lead_hours, window.valid_times_utc, strict=True):
            step = build_synthetic_wrf_step(bundle.domain, valid, base_unix + hour * 3600, hour)
            yield dataclasses.replace(
                step,
                t2_k=CompMass2D(Slab2DF32.from_values(ny, nx, [285.0] * (ny * nx))),
            )

    _stage_cycle(
        store,
        bundle,
        lock,
        window,
        build_synthetic_wrf_baseline(bundle.domain, origin, base_unix),
        stream_leads(),
        "solver",
    )
    receipt = publish_cycle_receipt(
        cfg,
        store,
        product_id=product_id,
        cycle_partition=window.cycle_partition,
        attempt_id="att-recover",
        bundle=bundle,
        now_utc_iso="2026-11-12T00:30:00Z",
    )
    return cfg, bundle, store, window.cycle_partition, receipt


def _ready_key(receipt: ReadyReceipt) -> str:
    return f"{published_cycle_prefix(receipt.model_id, receipt.cycle_partition)}/_ready.json"


def _latest_key(receipt: ReadyReceipt) -> str:
    return f"upstream/{receipt.model_id}/latest.json"


def test_recover_absent_returns_none(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    product_id = stage_sparse_config(CONFIG, cfg)
    bundle = load_bundle(cfg, product_id)
    store = LocalFileStore(tmp_path / "empty")
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    window = resolve_cycle_window(bundle.product, bundle.output, origin)
    assert recover_committed_cycle(store, bundle, window.cycle_partition) is None


def test_recover_missing_diagnostics_is_not_absent(tmp_path: Path) -> None:
    _, bundle, store, part, receipt = _published_fixture(tmp_path)
    diag_key = f"{published_cycle_prefix(receipt.model_id, part)}/_diagnostics.json"
    raw = store.get_bytes(diag_key)
    assert raw is not None
    store.delete_prefix(diag_key)
    before = store.get_bytes(_latest_key(receipt))
    with pytest.raises((FileNotFoundError, ValueError)):
        recover_committed_cycle(store, bundle, part, publish_origin_utc=receipt.publish_origin_utc)
    assert store.get_bytes(_latest_key(receipt)) == before


def test_recover_missing_shard_is_not_absent(tmp_path: Path) -> None:
    _, bundle, store, part, receipt = _published_fixture(tmp_path)
    shard_key = f"{published_cycle_prefix(receipt.model_id, part)}/{receipt.zarr_relpath}"
    shard_key = f"{shard_key}/{receipt.shard_sha256s[0][0]}"
    raw = store.get_bytes(shard_key)
    assert raw is not None
    store.delete_prefix(shard_key)
    before = store.get_bytes(_latest_key(receipt))
    with pytest.raises((FileNotFoundError, ValueError)):
        recover_committed_cycle(store, bundle, part, publish_origin_utc=receipt.publish_origin_utc)
    assert store.get_bytes(_latest_key(receipt)) == before


def test_recover_corrupt_ready_is_not_absent(tmp_path: Path) -> None:
    _, bundle, store, part, receipt = _published_fixture(tmp_path)
    key = _ready_key(receipt)
    _, token = store.get_with_token(key)
    assert token is not None
    store.compare_and_swap(key, b"{}", token)
    before = store.get_bytes(_latest_key(receipt))
    with pytest.raises((FileNotFoundError, ValueError)):
        recover_committed_cycle(store, bundle, part, publish_origin_utc=receipt.publish_origin_utc)
    assert store.get_bytes(_latest_key(receipt)) == before


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("config_sha256", "wrong"),
        ("forcing_profile_id", "wrong"),
        ("model_id", "wrong"),
        ("cycle_partition", "20261112/01"),
        ("publish_origin_utc", "2026-11-12T01:00:00Z"),
        ("grid", {"dx_m": 1}),
        ("exported_levels_k", 1),
        ("lead_hours", (999,)),
        ("valid_times_utc", ("2026-11-12T01:00:00Z",)),
        ("window_starts_utc", ("2026-11-12T01:00:00Z",)),
        ("window_ends_utc", ("2026-11-12T01:00:00Z",)),
        ("field_names", ("wrong-field",)),
        ("oci_digest", "sha256:not-a-digest"),
        ("zarr_relpath", "wrong.zarr"),
        ("diagnostics_sha256", "0" * 64),
        ("shard_sha256s", (("wrong", "0" * 64),)),
    ),
)
def test_recover_rejects_metadata_mutations(tmp_path: Path, field: str, value: object) -> None:
    _, bundle, store, part, receipt = _published_fixture(tmp_path)
    key = _ready_key(receipt)
    original, token = store.get_with_token(key)
    assert original is not None and token is not None
    mutated = ReadyReceipt.from_doc({**receipt.to_doc(), field: value})
    mutated_token = store.compare_and_swap(key, json_bytes(mutated.to_doc()), token)
    before = store.get_bytes(_latest_key(receipt))
    with pytest.raises((FileNotFoundError, ValueError)):
        recover_committed_cycle(store, bundle, part, publish_origin_utc=receipt.publish_origin_utc)
    assert store.get_bytes(_latest_key(receipt)) == before
    store.compare_and_swap(key, original, mutated_token)


def test_recover_valid_fixture_returns_current(tmp_path: Path) -> None:
    _, bundle, store, part, receipt = _published_fixture(tmp_path)
    before = store.get_bytes(_latest_key(receipt))
    result = recover_committed_cycle(
        store,
        bundle,
        part,
        publish_origin_utc=receipt.publish_origin_utc,
        now_utc_iso="2026-11-12T00:30:00Z",
    )
    assert result is not None
    recovered, latest = result
    assert recovered.attempt_id == receipt.attempt_id
    assert latest == "current"
    assert store.get_bytes(_latest_key(receipt)) == before


def test_notify_reconcile_ignores_current_config_changes(tmp_path: Path) -> None:
    cfg, bundle, store, part, receipt = _published_fixture(tmp_path)
    product_path = cfg / "products" / f"{bundle.product.product_id}.yaml"
    product_path.write_text(
        product_path.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8"
    )
    ready_before = store.get_bytes(_ready_key(receipt))
    latest_before = store.get_bytes(_latest_key(receipt))
    args = [
        "reconcile",
        "--config-dir",
        str(cfg),
        "--product",
        bundle.product.product_id,
        "--cycle",
        part,
        "--store-uri",
        (tmp_path / "store").as_uri(),
        "--webhook-url",
        "https://example.invalid",
    ]
    env = load_runtime_env({})
    with (
        patch("av_wrf.cli.main.load_runtime_env", return_value=env),
        patch("av_wrf.cli.main.post_webhook_json", return_value=204),
        redirect_stdout(StringIO()) as stdout,
    ):
        assert main(args) == 0
    assert json.loads(stdout.getvalue())["status"] == "reconciled"
    assert store.get_bytes(_ready_key(receipt)) == ready_before
    assert store.get_bytes(_latest_key(receipt)) == latest_before
    with (
        patch("av_wrf.cli.main.load_runtime_env", return_value=env),
        patch("av_wrf.cli.main.post_webhook_json", return_value=204),
        redirect_stdout(StringIO()) as stdout,
    ):
        assert main(args) == 0
    assert json.loads(stdout.getvalue())["dispatched_now"] is False
