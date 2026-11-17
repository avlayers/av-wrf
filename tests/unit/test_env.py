"""Runtime environment defaults and rank parsing."""

from __future__ import annotations

import unittest

from av_wrf.cli import load_runtime_env


class TestRuntimeEnv(unittest.TestCase):
    def test_default_rank_count_is_four(self) -> None:
        self.assertEqual(load_runtime_env({}).mpi_ranks, 4)
        self.assertEqual(load_runtime_env({"AV_WRF_MPI_RANKS": "12"}).mpi_ranks, 12)

    def test_invalid_rank_count_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, ">= 1"):
            load_runtime_env({"AV_WRF_MPI_RANKS": "0"})


if __name__ == "__main__":
    unittest.main()
