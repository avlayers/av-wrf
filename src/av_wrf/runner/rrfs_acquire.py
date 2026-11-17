"""Acquire paired RRFS CONUS GRIB2 files and record each half's digest."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from av_wrf.contracts import format_iso_utc
from av_wrf.runner.bundle import LoadedBundle, load_forcing_plan
from av_wrf.runner.forcing_rrfs import RRFS, admit_grib, rrfs_message_selected, rrfs_required_fields
from av_wrf.runner.mirrors import FORCING_MANIFEST_FILENAME, ForcingSubset
from av_wrf.runner.range_fetch import fetch_ranges
from av_wrf.runner.store import (
    LocalFileStore,
    forcing_cycle_lock,
    json_bytes,
    put_idempotent,
    sha256_file,
    sha256_hex,
)


def assembled_name(cycle: datetime, hour: int) -> str:
    """Return the WPS filename for one assembled RRFS hour."""
    return f"rrfs.t{cycle:%H}z.wps.f{hour:03d}.grib2"


def _provenance_name(name: str) -> str:
    return f"{name}.provenance.json"


def _hours(plan: ForcingSubset, through_hour: int | None) -> list[int]:
    hours = [
        hour for hour in plan["forecast_hours"] if through_hour is None or hour <= through_hour
    ]
    if not hours:
        raise ValueError("--through-hour precedes the first required boundary hour")
    return hours


def _keys(plan: ForcingSubset, hour: int) -> tuple[str, str]:
    raw = plan["file_keys"][str(hour)]
    if isinstance(raw, str) or len(raw) != 2 or not all(isinstance(key, str) for key in raw):
        raise ValueError("RRFS hour requires one prslev key and one 2dfld key")
    pressure, surface = str(raw[0]), str(raw[1])
    if ".prslev." not in pressure or ".2dfld." not in surface:
        raise ValueError("RRFS hour requires one prslev key and one 2dfld key")
    return pressure, surface


def _part_row(part: object) -> tuple[str, str, int] | None:
    if not isinstance(part, dict):
        return None
    key, digest, nbytes = part.get("key"), part.get("sha256"), part.get("nbytes")
    if isinstance(key, str) and isinstance(digest, str) and isinstance(nbytes, int) and nbytes > 0:
        return key, digest, nbytes
    return None


def _resume(
    path: Path, prov_path: Path, cycle: datetime, hour: int, keys: tuple[str, str]
) -> dict[str, str] | None:
    """Return component digests when the sidecar splits the assembled file exactly."""
    try:
        doc = json.loads(prov_path.read_bytes())
        blob = path.read_bytes()
    except (OSError, json.JSONDecodeError):
        return None
    parts = doc.get("parts") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or doc.get("hour") != hour
        or doc.get("forcing_cycle_utc") != format_iso_utc(cycle)
        or not isinstance(parts, list)
        or len(parts) != len(keys)
    ):
        return None
    found: dict[str, str] = {}
    offset = 0
    for key, part in zip(keys, parts, strict=True):
        row = _part_row(part)
        if row is None or row[0] != key or offset + row[2] > len(blob):
            return None
        chunk = blob[offset : offset + row[2]]
        if sha256_hex(chunk) != row[1]:
            return None
        found[key] = row[1]
        offset += row[2]
    return found if offset == len(blob) else None


def _admit_bytes(
    scratch: Path,
    name: str,
    payload: bytes,
    cycle: datetime,
    bundle: LoadedBundle,
    hour: int,
    role: str,
) -> str:
    path = scratch / name
    path.write_bytes(payload)
    return admit_grib(
        path, cycle, bundle.primary_source, bundle.domain, hour, rrfs_required_fields(role)
    )


def _assemble(
    client: httpx.Client,
    mirrors: tuple[str, ...],
    keys: tuple[str, str],
    scratch: Path,
    cycle: datetime,
    bundle: LoadedBundle,
    hour: int,
) -> tuple[bytes, list[dict[str, object]]]:
    """Fetch both halves, then admit each half and their concatenation."""
    payloads = tuple(
        fetch_ranges(client, mirrors, key, select=rrfs_message_selected, source_name="RRFS")
        for key in keys
    )
    digests = tuple(
        _admit_bytes(scratch, f"{role}.grib2", payload, cycle, bundle, hour, role)
        for role, payload in zip(("prslev", "2dfld"), payloads, strict=True)
    )
    if digests[0] != digests[1]:
        raise ValueError("RRFS pressure and surface grids differ")
    blob = payloads[0] + payloads[1]
    if _admit_bytes(scratch, "full.grib2", blob, cycle, bundle, hour, "full") != digests[0]:
        raise ValueError("RRFS assembled grid differs from its components")
    rows = [
        {"key": key, "sha256": sha256_hex(payload), "nbytes": len(payload)}
        for key, payload in zip(keys, payloads, strict=True)
    ]
    return blob, rows


def _json_object(path: Path) -> dict[str, object]:
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unreadable RRFS pin file {path.name}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"Unreadable RRFS pin file {path.name}")
    return doc


def _remember(pins: dict[str, str], name: object, digest: object) -> None:
    if not isinstance(name, str) or not isinstance(digest, str) or not name:
        raise ValueError("RRFS pin is not a filename and digest")
    prior = pins.get(name)
    if prior is not None and prior != digest:
        raise ValueError(f"RRFS pins disagree for {name}")
    pins[name] = digest


def _pins(directory: Path) -> dict[str, str]:
    """Return filename hashes already sealed by an inventory or forcing manifest."""
    pins: dict[str, str] = {}
    for path in sorted(directory.glob("rrfs.*.sources-*.json")):
        normalized = _json_object(path).get("normalized_sha256s")
        if not isinstance(normalized, dict):
            raise ValueError(f"RRFS inventory {path.name} has no normalized hashes")
        for name, digest in normalized.items():
            _remember(pins, name, digest)
    manifest = directory / FORCING_MANIFEST_FILENAME
    if not manifest.is_file():
        return pins
    pairs = _json_object(manifest).get("file_sha256s")
    if not isinstance(pairs, list):
        raise ValueError("forcing_manifest.json has no file hashes")
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("forcing_manifest.json has no file hashes")
        _remember(pins, pair[0], pair[1])
    return pins


def _refuse_foreign(pins: Mapping[str, str], name: str, digest: str) -> None:
    pinned = pins.get(name)
    if pinned is not None and pinned != digest:
        raise ValueError(f"Pinned RRFS file {name} does not match this revision")


def _place(directory: Path, name: str, payload: bytes, pins: Mapping[str, str]) -> None:
    path = directory / name
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Expected a regular RRFS file at {path}")
    digest = sha256_hex(payload)
    # A sealed inventory is immutable, so a conflicting revision must not unlink it.
    _refuse_foreign(pins, name, digest)
    if path.is_file():
        if sha256_file(path) == digest:
            return
        path.unlink()
    put_idempotent(LocalFileStore(directory), name, payload)


def _one_hour(
    client: httpx.Client,
    mirrors: tuple[str, ...],
    bundle: LoadedBundle,
    cycle: datetime,
    hour: int,
    keys: tuple[str, str],
    grib_dir: Path,
    scratch: Path,
    pins: Mapping[str, str],
) -> tuple[dict[str, str], str]:
    name = assembled_name(cycle, hour)
    target = grib_dir / name
    resumed = _resume(target, grib_dir / _provenance_name(name), cycle, hour, keys)
    if resumed is not None:
        try:
            admit_grib(
                target,
                cycle,
                bundle.primary_source,
                bundle.domain,
                hour,
                rrfs_required_fields("full"),
            )
        except ValueError:
            resumed = None
        else:
            digest = sha256_file(target)
            _refuse_foreign(pins, name, digest)
            return resumed, digest
    blob, rows = _assemble(client, mirrors, keys, scratch, cycle, bundle, hour)
    digest = sha256_hex(blob)
    _refuse_foreign(pins, name, digest)
    _place(grib_dir, name, blob, pins)
    _place(
        grib_dir,
        _provenance_name(name),
        json_bytes({"forcing_cycle_utc": format_iso_utc(cycle), "hour": hour, "parts": rows}),
        pins,
    )
    return {str(row["key"]): str(row["sha256"]) for row in rows}, digest


def _acquire_locked(
    client: httpx.Client,
    bundle: LoadedBundle,
    cycle: datetime,
    plan: ForcingSubset,
    grib_dir: Path,
    through_hour: int | None,
) -> dict[int, str]:
    hours = _hours(plan, through_hour)
    pins = _pins(grib_dir)
    files: dict[int, str] = {}
    source_sha256s: dict[str, str] = {}
    normalized: dict[str, str] = {}
    mirrors = tuple(bundle.primary_source.mirrors)
    with TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for hour in hours:
            keys = _keys(plan, hour)
            name = assembled_name(cycle, hour)
            parts, digest = _one_hour(
                client, mirrors, bundle, cycle, hour, keys, grib_dir, scratch, pins
            )
            source_sha256s.update(parts)
            files[hour] = name
            # digest is the admitted snapshot, not a later read of the shared path.
            normalized[name] = digest
    put_idempotent(
        LocalFileStore(grib_dir),
        f"rrfs.{cycle:%Y%m%d%H}.sources-f{hours[-1]:03d}.json",
        json_bytes(
            {
                "forcing_cycle_utc": format_iso_utc(cycle),
                "source_sha256s": source_sha256s,
                "normalized_sha256s": normalized,
            }
        ),
    )
    return files


def acquire_rrfs(
    client: httpx.Client,
    bundle: LoadedBundle,
    cycle: datetime,
    plan: ForcingSubset,
    grib_dir: Path,
    through_hour: int | None = None,
) -> dict[int, str]:
    """Install assembled hours. `through_hour` limits a smoke fetch and its inventory."""
    with forcing_cycle_lock(grib_dir):
        return _acquire_locked(client, bundle, cycle, plan, grib_dir, through_hour)


def main(argv: list[str] | None = None) -> int:
    """Print assembled hour names. This command does not write forcing_manifest.json."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="Path to forcing_subset.json")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--grib-dir", type=Path, required=True, help="Write validated GRIB2 here")
    parser.add_argument("--through-hour", type=int, help="Last boundary lead for a smoke run")
    args = parser.parse_args(argv)
    bundle, cycle, plan = load_forcing_plan(args.plan, args.config_dir, RRFS)
    with httpx.Client(timeout=120) as client:
        files = acquire_rrfs(client, bundle, cycle, plan, args.grib_dir, args.through_hour)
    print(json.dumps({str(hour): name for hour, name in files.items()}))
    return 0


__all__ = ["acquire_rrfs", "assembled_name", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
