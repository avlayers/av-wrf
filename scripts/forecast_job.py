"""Runner budget and archive manifest for one unattended forecast."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from av_wrf.runner import parse_declarative_yaml
from scripts.latest_cycle import _cron_offset_minutes

NATIVE_TIMEOUT_MARGIN_MIN = 15
GEOG_CACHE_KEY = "wps-geog-high-res-v1"
_MANIFEST = "MANIFEST.txt"
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Numeric size times 4 is the vCPU count (8xlarge is 32). Matching names are
# EBS-only arm64 types on Graviton3, Graviton4, and Graviton5. The solver
# image is armv8.2-a+fp16+dotprod, which those CPUs implement. Generation 6
# stays outside this pool because its throughput can miss the job cap.
_SOLVER_TYPE = re.compile(r"[cmr][789]g\.([0-9]+)xlarge")
_SOLVER_SIZES = frozenset({2, 4, 8, 12, 16, 24, 48})
_ENVIRONMENTS = frozenset({"staging", "production"})


@dataclass(frozen=True)
class ForecastTarget:
    product_id: str
    schedule_cron_utc: str
    estimate_minutes: int
    families: tuple[str, ...]
    vcpus: int
    mpi_ranks: int
    volume: str
    spot: bool
    image_name: str
    consumer_repository: str
    consumer_model: str
    consumer_environment: str


@dataclass(frozen=True)
class ForecastJob:
    product_id: str
    cycle_iso: str
    cycle_partition: str
    attempt: str
    timeout_minutes: int
    native_timeout_s: int
    mpi_ranks: int
    solver_label: str
    image_name: str
    inputs_cache_key: str
    wrfout_cache_key: str
    geog_cache_key: str
    consumer_repository: str
    consumer_model: str
    consumer_environment: str


def timeout_minutes(estimate_minutes: int) -> int:
    """Return the job cap at 1.5 times the planning estimate, rounded up."""
    if estimate_minutes < 1:
        raise ValueError("estimate_minutes must be positive")
    return (estimate_minutes * 3 + 1) // 2


def native_timeout_s(job_timeout_min: int) -> int:
    """Leave a short margin so the solver exits before the job is cancelled."""
    remain = job_timeout_min - NATIVE_TIMEOUT_MARGIN_MIN
    if remain < 1:
        raise ValueError("Job timeout must exceed the native-stage margin")
    return remain * 60


def load_targets(path: Path) -> tuple[ForecastTarget, ...]:
    """Load the unattended allowlist. An empty file is a configuration error."""
    doc = parse_declarative_yaml(path)
    raw_runs = doc.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError(f"{path} must list runs")
    return tuple(_target(_as_map(item, "run")) for item in raw_runs)


def write_manifest(root: Path) -> None:
    """Record restored-file sizes so a short cache extract fails closed."""
    lines = [f"{size} {rel}" for rel, size in _sizes(root).items()]
    (root / _MANIFEST).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def verify_manifest(root: Path) -> None:
    """Reject a cache restore whose files differ from the saved size manifest."""
    manifest = root / _MANIFEST
    if not manifest.is_file():
        raise ValueError(f"Missing archive manifest: {manifest}")
    if _sizes(root) != _parse_manifest(manifest):
        raise ValueError("Archive manifest does not match restored files")


def solver_label(run_id: str, target: ForecastTarget) -> str:
    """Bake the Graviton label. The caller supplies the workflow run id."""
    family = "+".join(target.families)
    spot = "true" if target.spot else "false"
    return (
        f"runs-on={run_id}/family={family}/cpu={target.vcpus}"
        f"/image=ubuntu24-full-arm64/volume={target.volume}"
        f"/spot={spot}/extras=s3-cache+ecr-cache"
    )


def _sizes(root: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for path in _manifest_files(root):
        sizes[path.relative_to(root).as_posix()] = path.stat().st_size
    return sizes


def _parse_manifest(manifest: Path) -> dict[str, int]:
    expected: dict[str, int] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        size_s, _, rel = line.partition(" ")
        if not size_s.isdecimal() or not rel:
            raise ValueError(f"Invalid manifest line: {line!r}")
        expected[rel] = int(size_s)
    return expected


def _manifest_files(root: Path) -> tuple[Path, ...]:
    files = [path for path in root.rglob("*") if path.is_file() and path.name != _MANIFEST]
    return tuple(sorted(files))


def repository_parts(repository: str) -> tuple[str, str]:
    """Return the owner and name of an `owner/name` repository slug."""
    owner, sep, name = repository.partition("/")
    if sep != "/" or not _SEGMENT.fullmatch(owner) or not _SEGMENT.fullmatch(name):
        raise ValueError(f"consumer_repository must be owner/name, got {repository!r}")
    return owner, name


def _target(item: dict[str, object]) -> ForecastTarget:
    repository, model, environment = _consumer(item)
    vcpus = _req_int(item, "vcpus")
    return ForecastTarget(
        product_id=_req_str(item, "product_id"),
        schedule_cron_utc=_daily_cron(_req_str(item, "schedule_cron_utc")),
        estimate_minutes=_req_int(item, "estimate_minutes"),
        families=_families(item, vcpus),
        vcpus=vcpus,
        mpi_ranks=_req_int(item, "mpi_ranks"),
        volume=_req_str(item, "volume"),
        spot=_req_bool(item, "spot"),
        image_name=_req_str(item, "image_name"),
        consumer_repository=repository,
        consumer_model=model,
        consumer_environment=environment,
    )


def _consumer(item: dict[str, object]) -> tuple[str, str, str]:
    repository = _req_str(item, "consumer_repository")
    model = _req_str(item, "consumer_model")
    environment = _req_str(item, "consumer_environment")
    repository_parts(repository)
    if environment not in _ENVIRONMENTS:
        raise ValueError("consumer_environment must be staging or production")
    if not _SEGMENT.fullmatch(model):
        raise ValueError("consumer_model must be a single path segment")
    return repository, model, environment


def _families(item: dict[str, object], vcpus: int) -> tuple[str, ...]:
    raw = item.get("families")
    if not isinstance(raw, list) or not raw:
        raise ValueError("families must be a non-empty list")
    names = tuple(entry.strip() for entry in raw if isinstance(entry, str) and entry.strip())
    if len(names) != len(raw) or any(_family_vcpus(name) != vcpus for name in names):
        raise ValueError("families must be Graviton3, Graviton4, or Graviton5 types of vcpus")
    return names


def _family_vcpus(name: str) -> int:
    """Return the vCPU count of one allowlisted Graviton type, or 0."""
    match = _SOLVER_TYPE.fullmatch(name)
    if match is None:
        return 0
    size = int(match.group(1))
    if size not in _SOLVER_SIZES:
        return 0
    return size * 4


def _daily_cron(cron: str) -> str:
    """Accept one daily UTC cron. The offset call rejects any other shape."""
    _cron_offset_minutes(cron, 0)
    return cron


def _req_str(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {key}")
    return value.strip()


def _req_bool(item: dict[str, object], key: str) -> bool:
    value = item.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _req_int(item: dict[str, object], key: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _as_map(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        mapped[key] = item
    return mapped
