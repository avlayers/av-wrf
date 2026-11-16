"""Validate a committed cycle and advance latest without recomputation."""

from __future__ import annotations

from av_wrf.contracts import ReadyReceipt
from av_wrf.core import resolve_cycle_window
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.publish import (
    _advance_latest_pointer_with_retry,
    _LatestOutcome,
    load_ready_receipt,
)
from av_wrf.runner.publish_verify import verify_published_objects
from av_wrf.runner.store import ObjectStore, published_cycle_prefix


def recover_committed_cycle(
    store: ObjectStore,
    bundle: LoadedBundle,
    cycle_partition: str,
    *,
    now_utc_iso: str | None = None,
) -> tuple[ReadyReceipt, _LatestOutcome]:
    """Validate a committed receipt and advance latest without recomputing it."""
    receipt = load_ready_receipt(store, bundle.product.model_id, cycle_partition)
    if receipt.config_sha256 != bundle.config_sha256:
        raise ValueError("Committed receipt config differs from the current configuration")
    if receipt.forcing_profile_id != bundle.primary_source.profile_id:
        raise ValueError("Committed receipt forcing profile differs from the current configuration")
    if not receipt.oci_digest.startswith("sha256:"):
        raise ValueError("Committed receipt lacks a pinned OCI digest")
    window = resolve_cycle_window(bundle.product, bundle.output, receipt.publish_origin_utc)
    root = published_cycle_prefix(receipt.model_id, receipt.cycle_partition)
    verify_published_objects(store, root, receipt, bundle, window)
    latest = _advance_latest_pointer_with_retry(store, receipt, bundle, now_utc_iso)
    return receipt, latest


__all__ = ["recover_committed_cycle"]
