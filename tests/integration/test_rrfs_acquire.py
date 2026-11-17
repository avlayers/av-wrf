"""Assemble RRFS pressure and surface halves without mixing their digests."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx
import pytest

from av_wrf.contracts import BuildManifest
from av_wrf.runner import LocalFileStore, plan_product_cycle
from av_wrf.runner.acquire import acquire_forcing
from av_wrf.runner.mirrors import ForcingSubset
from av_wrf.runner.rrfs_acquire import acquire_rrfs
from av_wrf.runner.store import sha256_hex
from tests.integration import bundle_for_spacing

_ROOT = Path(__file__).resolve().parents[2]
_CYCLE = datetime(2026, 11, 17, 6, tzinfo=UTC)


def _plan(*hours: int) -> dict[str, object]:
    def keys(hour: int) -> tuple[str, str]:
        stem = "rrfs.20261117/06/rrfs.t06z"
        return (
            f"{stem}.prslev.3km.f{hour:03d}.conus.grib2",
            f"{stem}.2dfld.3km.f{hour:03d}.conus.grib2",
        )

    return {
        "forecast_hours": list(hours),
        "file_keys": {str(hour): keys(hour) for hour in hours},
    }


def _admit(path: Path, *_args: object, **_kwargs: object) -> str:
    blob = path.read_bytes()
    if blob.startswith(b"BAD"):
        raise ValueError("corrupt RRFS component")
    if blob.startswith(b"TIME"):
        raise ValueError("Wrong RRFS source, cycle or valid time")
    if blob.startswith(b"SFCX"):
        return "other-grid"
    return "grid"


def _fetch(payloads: dict[str, bytes], calls: list[str]) -> Callable[[object, object, str], bytes]:
    def fetch(_client: object, _mirrors: object, key: str, **_kwargs: object) -> bytes:
        calls.append(key)
        if "2dfld" in key and "sfc" not in payloads:
            raise ValueError("missing RRFS surface object")
        return payloads["prs" if "prslev" in key else "sfc"]

    return fetch


def _acquire(
    directory: Path,
    payloads: dict[str, bytes],
    hours: tuple[int, ...] = (0, 1),
    through: int | None = None,
) -> tuple[dict[int, str], list[str]]:
    calls: list[str] = []
    bundle = bundle_for_spacing(_ROOT / "config", 0.027)
    with (
        patch("av_wrf.runner.rrfs_acquire.fetch_ranges", _fetch(payloads, calls)),
        patch("av_wrf.runner.rrfs_acquire.admit_grib", _admit),
        httpx.Client() as client,
    ):
        plan = cast(ForcingSubset, _plan(*hours))
        files = acquire_rrfs(client, bundle, _CYCLE, plan, directory, through)
    return files, calls


def test_components_keep_separate_hashes_and_install_together(tmp_path: Path) -> None:
    prs, sfc = b"PRS-pressure", b"SFC-surface"
    files, calls = _acquire(tmp_path, {"prs": prs, "sfc": sfc}, (0,))
    name = files[0]
    assembled = (tmp_path / name).read_bytes()
    provenance = json.loads((tmp_path / f"{name}.provenance.json").read_bytes())
    inventory = json.loads(next(tmp_path.glob("rrfs.*.sources-*.json")).read_bytes())
    assert assembled == prs + sfc
    offset = 0
    for part in provenance["parts"]:
        chunk = assembled[offset : offset + part["nbytes"]]
        assert sha256_hex(chunk) == part["sha256"] == inventory["source_sha256s"][part["key"]]
        offset += part["nbytes"]
    assert offset == len(assembled)
    assert provenance["parts"][0]["nbytes"] == len(prs)
    assert sha256_hex(prs) != sha256_hex(assembled)
    assert inventory["source_sha256s"][provenance["parts"][0]["key"]] == sha256_hex(prs)
    assert inventory["normalized_sha256s"][name] == sha256_hex(assembled)
    assert inventory["source_sha256s"][provenance["parts"][1]["key"]] != sha256_hex(assembled)
    assert len(calls) == 2


def test_resume_reuses_component_provenance(tmp_path: Path) -> None:
    payloads = {"prs": b"PRS-pressure", "sfc": b"SFC-surface"}
    _acquire(tmp_path, payloads, (0,))
    _files, calls = _acquire(tmp_path, payloads, (0,))
    assert calls == []
    provenance = next(tmp_path.glob("*.provenance.json"))
    doc = json.loads(provenance.read_bytes())
    doc["parts"][0]["sha256"] = "0" * 64
    provenance.write_bytes(json.dumps(doc).encode())
    _files, calls = _acquire(tmp_path, payloads, (0,))
    assert len(calls) == 2
    restored = json.loads(provenance.read_bytes())
    assert restored["parts"][0]["sha256"] == sha256_hex(payloads["prs"])


def test_a_bad_half_leaves_no_assembled_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _acquire(tmp_path, {"prs": b"BAD-pressure", "sfc": b"SFC-surface"}, (0,))
    assert list(tmp_path.glob("*.grib2")) == []
    with pytest.raises(ValueError):
        _acquire(tmp_path, {"prs": b"PRS-pressure"}, (0,))
    assert list(tmp_path.glob("*.grib2")) == []
    with pytest.raises(ValueError, match="grids differ"):
        _acquire(tmp_path, {"prs": b"PRS-pressure", "sfc": b"SFCX-surface"}, (0,))
    assert list(tmp_path.glob("*.grib2")) == []
    with pytest.raises(ValueError, match="Wrong RRFS"):
        _acquire(tmp_path, {"prs": b"PRS-pressure", "sfc": b"TIME-surface"}, (0,))
    assert list(tmp_path.glob("*.grib2")) == []


def test_smoke_hour_does_not_record_the_later_boundary(tmp_path: Path) -> None:
    files, calls = _acquire(tmp_path, {"prs": b"PRS-pressure", "sfc": b"SFC-surface"}, (0, 1), 0)
    assert list(files) == [0]
    assert len(calls) == 2
    inventory = json.loads(next(tmp_path.glob("*.sources-*.json")).read_bytes())
    assert all(key.endswith("f000.conus.grib2") for key in inventory["source_sha256s"])
    assert not (tmp_path / "rrfs.t06z.wps.f001.grib2").exists()


def test_a_pinned_hour_survives_a_revised_retry(tmp_path: Path) -> None:
    original = {"prs": b"PRS-revision-A", "sfc": b"SFC-revision-A"}
    _acquire(tmp_path, original, (0,))
    grib = next(tmp_path.glob("*.grib2"))
    installed = grib.read_bytes()
    provenance = tmp_path / f"{grib.name}.provenance.json"
    provenance.write_bytes(b"{")
    with pytest.raises(ValueError, match="Pinned RRFS"):
        _acquire(tmp_path, {"prs": b"PRS-revision-B", "sfc": b"SFC-revision-B"}, (0,))
    assert grib.read_bytes() == installed
    assert provenance.read_bytes() == b"{"
    inventory = json.loads(next(tmp_path.glob("*.sources-*.json")).read_bytes())
    assert inventory["normalized_sha256s"][grib.name] == sha256_hex(installed)
    assert set(inventory["source_sha256s"].values()) == {
        sha256_hex(original["prs"]),
        sha256_hex(original["sfc"]),
    }


def _race(directory: Path, revisions: dict[str, tuple[bytes, bytes]]) -> dict[str, object]:
    barrier = threading.Barrier(len(revisions))
    outcomes: dict[str, object] = {}
    bundle = bundle_for_spacing(_ROOT / "config", 0.027)
    plan = cast(ForcingSubset, _plan(0))

    def fetch(_client: object, _mirrors: object, key: str, **_kwargs: object) -> bytes:
        prs, sfc = revisions[threading.current_thread().name]
        return prs if "prslev" in key else sfc

    def acquire(label: str, client: httpx.Client) -> None:
        barrier.wait()
        try:
            outcomes[label] = acquire_rrfs(client, bundle, _CYCLE, plan, directory)
        except (OSError, ValueError) as exc:
            outcomes[label] = exc

    with (
        patch("av_wrf.runner.rrfs_acquire.fetch_ranges", fetch),
        patch("av_wrf.runner.rrfs_acquire.admit_grib", _admit),
        httpx.Client() as client,
    ):
        threads = []
        for label in revisions:
            threads.append(threading.Thread(target=acquire, name=label, args=(label, client)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    return outcomes


def test_concurrent_revisions_keep_one_lineage(tmp_path: Path) -> None:
    revisions = {
        "A": (b"PRS-revision-A", b"SFC-revision-A"),
        "B": (b"PRS-revision-B", b"SFC-revision-B"),
    }
    outcomes = _race(tmp_path, revisions)
    assert len(outcomes) == 2
    assert any(not isinstance(item, Exception) for item in outcomes.values())
    grib = next(tmp_path.glob("*.grib2"))
    installed = grib.read_bytes()
    matched = [label for label, parts in revisions.items() if parts[0] + parts[1] == installed]
    assert len(matched) == 1
    prs, sfc = revisions[matched[0]]
    inventory = json.loads(next(tmp_path.glob("*.sources-*.json")).read_bytes())
    provenance = json.loads((tmp_path / f"{grib.name}.provenance.json").read_bytes())
    assert inventory["normalized_sha256s"][grib.name] == sha256_hex(installed)
    assert set(inventory["source_sha256s"].values()) == {sha256_hex(prs), sha256_hex(sfc)}
    offset = 0
    for part in provenance["parts"]:
        chunk = installed[offset : offset + part["nbytes"]]
        assert sha256_hex(chunk) == part["sha256"] == inventory["source_sha256s"][part["key"]]
        offset += part["nbytes"]
    assert offset == len(installed)


def test_production_acquire_keeps_the_full_lock(tmp_path: Path) -> None:
    bundle = bundle_for_spacing(_ROOT / "config", 0.027)
    _key, lock = plan_product_cycle(
        _ROOT / "config",
        LocalFileStore(tmp_path / "store"),
        product_id=bundle.product.product_id,
        publish_origin_utc="2026-11-17T12:00:00Z",
        attempt_id="rrfs-lock",
        mpi_ranks=1,
        build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m"),
        bundle=bundle,
    )
    with (
        patch("av_wrf.runner.acquire.acquire_rrfs", return_value={0: "hour.grib2"}) as fetched,
        patch("av_wrf.runner.acquire.write_forcing_manifest", return_value="sealed") as sealed,
    ):
        acquire_forcing(bundle, lock, tmp_path / "grib")
    assert fetched.call_args is not None
    assert len(fetched.call_args.args) == 5
    assert fetched.call_args.kwargs.get("through_hour") is None
    sealed.assert_called_once()
