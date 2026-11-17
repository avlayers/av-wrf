"""Check one met_em and one wrfinput against the RRFS soil contract."""

from __future__ import annotations

import argparse
from pathlib import Path

from av_wrf.runner.rrfs_native import admit_met_em, admit_wrfinput_soil


def main(argv: list[str] | None = None) -> int:
    """Admit isolated WPS or WRF files. This command does not seal a forcing manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--met-em", type=Path)
    parser.add_argument("--wrfinput", type=Path)
    args = parser.parse_args(argv)
    if args.met_em is None and args.wrfinput is None:
        parser.error("Pass --met-em or --wrfinput")
    if args.met_em is not None:
        admit_met_em(args.met_em)
    if args.wrfinput is not None:
        admit_wrfinput_soil(args.wrfinput)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
