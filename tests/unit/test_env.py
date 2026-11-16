"""Runtime environment defaults and MPI mesh validation."""

from __future__ import annotations

import unittest

from av_wrf.cli import load_runtime_env


class TestRuntimeEnv(unittest.TestCase):
    def test_default_ranks_and_optional_compute_mesh(self) -> None:
        env = load_runtime_env({})
        self.assertEqual(env.mpi_ranks, 16)
        self.assertIsNone(env.nproc)
        override = load_runtime_env({"AV_WRF_NPROC_X": "5", "AV_WRF_NPROC_Y": "3"})
        self.assertEqual(override.nproc, (5, 3))

    def test_partial_or_invalid_compute_mesh_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "both"):
            load_runtime_env({"AV_WRF_NPROC_X": "4"})
        with self.assertRaisesRegex(ValueError, ">= 1"):
            load_runtime_env({"AV_WRF_NPROC_X": "0", "AV_WRF_NPROC_Y": "1"})


if __name__ == "__main__":
    unittest.main()
