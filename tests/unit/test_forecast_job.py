"""Solver families stay on the ARM image's Graviton generations."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.forecast_job import _families, load_targets, solver_label  # noqa: E402

_OPS = ROOT / "config" / "operations" / "forecasts.yaml"
_SOLVE_FAMILIES = (
    "c8g.8xlarge",
    "c7g.8xlarge",
    "m7g.8xlarge",
    "c9g.8xlarge",
    "m8g.8xlarge",
    "m9g.8xlarge",
    "r7g.8xlarge",
    "r8g.8xlarge",
)


class TestSolverFamilies(unittest.TestCase):
    def test_allowlist_is_graviton3_or_newer_8xlarge(self) -> None:
        target = load_targets(_OPS)[0]
        self.assertEqual(target.families, _SOLVE_FAMILIES)
        self.assertEqual(target.vcpus, 32)
        self.assertTrue(target.spot)
        label = solver_label("1" * 12, target)
        self.assertLess(len(label), 256)
        self.assertIn("/cpu=32/image=ubuntu24-full-arm64/", label)
        self.assertIn("spot=true", label)

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
