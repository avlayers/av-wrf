"""Lock RRFS index selection to the captured CONUS inventory."""

from __future__ import annotations

import re
from pathlib import Path

from av_wrf.runner.forcing_rrfs import rrfs_message_selected, rrfs_required_fields
from av_wrf.runner.range_fetch import _ranges

_ROOT = Path(__file__).resolve().parents[2]
_PRS = _ROOT / "tests/fixtures/rrfs/prslev-f000.idx"
_SFC = _ROOT / "tests/fixtures/rrfs/2dfld-f000.idx"
_SOIL = (
    "0-0 m below ground",
    "0.01-0.01 m below ground",
    "0.04-0.04 m below ground",
    "0.1-0.1 m below ground",
    "0.3-0.3 m below ground",
    "0.6-0.6 m below ground",
    "1-1 m below ground",
    "1.6-1.6 m below ground",
    "3-3 m below ground",
)
_SURFACE = (
    ("PRES", "surface"),
    ("HGT", "surface"),
    ("TMP", "surface"),
    ("LAND", "surface"),
    ("ICEC", "surface"),
    ("WEASD", "surface"),
    ("SNOD", "surface"),
    ("MSLET", "mean sea level"),
    ("TMP", "2 m above ground"),
    ("SPFH", "2 m above ground"),
    ("UGRD", "10 m above ground"),
    ("VGRD", "10 m above ground"),
    *((variable, level) for variable in ("TSOIL", "SOILW") for level in _SOIL),
)


def _lines(path: Path) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(":")
        if len(parts) >= 5:
            rows.append((parts[3], parts[4]))
    return rows


def _isobaric(rows: list[tuple[str, str]], variable: str) -> list[int]:
    found = []
    for name, level in rows:
        match = re.fullmatch(r"([0-9]+) mb", level)
        if name == variable and match:
            found.append(int(match.group(1)))
    return found


def test_isobaric_selection_follows_the_captured_height_levels() -> None:
    rows = _lines(_PRS)
    levels = {level for _name, level in rows}
    heights = _isobaric(rows, "HGT")
    assert "70 mb" in levels and "75 mb" not in levels
    assert heights == [
        level for level in range(1, 1100) if rrfs_message_selected("HGT", f"{level} mb")
    ]
    assert not rrfs_message_selected("HGT", "75 mb")
    assert not rrfs_message_selected("RH", "500 mb")
    assert not rrfs_message_selected("SPFH", "30-0 mb above ground")
    assert any(level == "30-0 mb above ground" and name == "SPFH" for name, level in _lines(_PRS))


def test_surface_selection_matches_the_captured_pairs() -> None:
    rows = _lines(_PRS) + _lines(_SFC)
    assert ("SPFH", "surface") in rows
    assert not rrfs_message_selected("SPFH", "surface")
    for pair in _SURFACE:
        assert pair in rows
        assert rrfs_message_selected(*pair)
    selected = [pair for pair in rows if rrfs_message_selected(*pair) and "mb" not in pair[1]]
    assert set(selected) == set(_SURFACE)
    assert sum(rrfs_message_selected(*pair) for pair in rows) == 255
    assert len(rrfs_required_fields("full")) == 255
    assert len(rrfs_required_fields("prslev")) == 225
    assert len(rrfs_required_fields("2dfld")) == 30


def test_byte_ranges_drop_an_unselected_neighbor() -> None:
    index = "1:0:d=x:HGT:70 mb:anl:\n2:10:d=x:HGT:75 mb:anl:\n"
    assert _ranges(index, 20, rrfs_message_selected, "RRFS") == [(0, 9)]
