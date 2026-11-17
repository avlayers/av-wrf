"""Solver families stay on the ARM image's Graviton generations."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.forecast_job import _families, load_targets  # noqa: E402

_OPS = ROOT / "config" / "operations" / "forecasts.yaml"


class TestSolverFamilies(unittest.TestCase):
    def test_allowlist_is_graviton3_or_newer_8xlarge(self) -> None:
        target = load_targets(_OPS)[0]
        self.assertEqual(target.vcpus, 32)
        self.assertGreaterEqual(len(target.families), 4)
        for name in target.families:
            self.assertRegex(name, r"^[cmr][789]g\.8xlarge$")

    def test_other_cpus_are_rejected(self) -> None:
        rejected = (
            ["c6g.8xlarge"],
            ["c8gd.8xlarge"],
            ["c7i.8xlarge"],
            ["c8g.16xlarge"],
            ["c8g.metal-24xl"],
        )
        for names in rejected:
            with self.assertRaises(ValueError):
                _families({"families": names}, 32)
