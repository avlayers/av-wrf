"""Argument parser for the `av-wrf` CLI."""

from __future__ import annotations

import argparse
from pathlib import Path

CLI_COMMANDS: tuple[tuple[str, str], ...] = (
    ("check", "Validate configured product(s), vertical stretch, namelist, and Zarr v3."),
    ("plan", "Resolve cycle timestamps and persist an immutable run.lock.json."),
    ("geog", "Build and validate static geography for the active domains."),
    ("prep", "Write Fortran namelist.wps + namelist.input and forcing_subset.json recipe."),
    ("acquire", "Acquire and seal the locked forcing inventory."),
    ("forcing-manifest", "Verify local GRIB2 inputs and record the forcing inventory."),
    ("solve", "Execute WPS ungrib/metgrid + MPI real/wrf and bind solver_receipt.json."),
    ("post", "Stage wrfout frames into attempt-scoped sharded wrf-zarr-v3 arrays."),
    ("publish", "Verify full RunLock horizon, promote Zarr store, and CAS latest.json."),
    ("recover", "Validate a committed cycle and advance latest without recomputing it."),
    ("reconcile", "Dispatch pending _ready.json webhook idempotently (fail closed)."),
)

_CYCLE_CMDS = {name for name, _ in CLI_COMMANDS if name not in {"check", "geog"}}


def _add_product_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument(
        "--product",
        required=(cmd_name != "check"),
        help="Configured product ID under <config-dir>/products/ (omit on check for all)",
    )
    cmd.add_argument("--config-dir", default="config", help="Declarative config directory")


def _add_cycle_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name not in _CYCLE_CMDS:
        return
    cmd.add_argument("--cycle", required=True, help="Publish origin ISO UTC or YYYYMMDD/HH")
    attempt = None if cmd_name == "reconcile" else "att-01"
    cmd.add_argument("--attempt", default=attempt, help="Attempt identifier")


def _add_plan_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name != "plan":
        return
    cmd.add_argument(
        "--allow-unpinned-dev",
        action="store_true",
        help="Permit local host dry-run when solver build-manifest.json is absent",
    )
    cmd.add_argument("--geog-file", type=Path, help="Bind the geography file or bundle SHA-256")


def _add_work_dir_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name in {"geog", "prep"}:
        default_dir = "work/geog" if cmd_name == "geog" else "work/prep"
        dir_help = "New geogrid directory" if cmd_name == "geog" else "Target directory"
        cmd.add_argument("--work-dir", default=default_dir, help=dir_help)
    if cmd_name == "solve":
        cmd.add_argument("--work-dir", default="work/solve", help="Attempt scratch root")
        cmd.add_argument("--geog-file", type=Path, required=True, help="Locked geography path")


def _add_forcing_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name in {"acquire", "forcing-manifest", "solve"}:
        cmd.add_argument("--grib-dir", type=Path, required=True, help="Staged forcing directory")
    if cmd_name != "forcing-manifest":
        return
    cmd.add_argument("--acquisition-inventory", type=Path, help="Source hash inventory path")
    cmd.add_argument(
        "--file",
        action="append",
        required=True,
        metavar="HOUR=FILENAME",
        help="Forcing forecast hour and filename within --grib-dir (repeatable)",
    )


def _add_post_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name != "post":
        return
    source = cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--wrfout-dir", type=Path, help="Active-domain WRF output directory")
    source.add_argument("--synthetic", action="store_true", help="Stage full synthetic horizon")


def _add_publish_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name != "publish":
        return
    cmd.add_argument(
        "--allow-synthetic-fixture",
        action="store_true",
        help="Publish synthetic run into isolated fixtures/synthetic/ namespace",
    )
    cmd.add_argument(
        "--prune-stage",
        action="store_true",
        help="Delete staged attempt prefix after promotion succeeds",
    )
    cmd.add_argument(
        "--store-uri",
        action="append",
        help="Target destination store URI(s) (repeatable; defaults to AV_WRF_STORE_URI)",
    )


def _add_store_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name == "reconcile":
        cmd.add_argument("--store-uri", default=None, help="Destination store URI")
        cmd.add_argument("--webhook-url", default="", help="Consumer webhook URL override")
    if cmd_name == "recover":
        cmd.add_argument(
            "--probe",
            action="store_true",
            help="Return absent instead of failing when no committed receipt exists",
        )
        cmd.add_argument("--store-uri", default=None, help="Destination store URI")


def _add_acquire_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    if cmd_name != "acquire":
        return
    cmd.add_argument("--plan", type=Path, help="Verify a prep forcing_subset.json")
    cmd.add_argument("--cache", type=Path, help="ICON source object cache")
    cmd.add_argument("--workers", type=int, default=8, help="Concurrent downloads")
    cmd.add_argument("--gfs-regional-filter", action="store_true", help="Use NOAA cutout")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="av-wrf")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd_name, help_text in CLI_COMMANDS:
        cmd = sub.add_parser(cmd_name, help=help_text)
        _add_product_flags(cmd_name, cmd)
        _add_cycle_flags(cmd_name, cmd)
        _add_plan_flags(cmd_name, cmd)
        _add_work_dir_flags(cmd_name, cmd)
        _add_forcing_flags(cmd_name, cmd)
        _add_post_flags(cmd_name, cmd)
        _add_publish_flags(cmd_name, cmd)
        _add_store_flags(cmd_name, cmd)
        _add_acquire_flags(cmd_name, cmd)
    return parser


__all__ = ["CLI_COMMANDS", "build_parser"]
