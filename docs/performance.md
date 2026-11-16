# Parallel Scaling and I/O

`av-wrf` launches `real.exe` and `wrf.exe` with `AV_WRF_MPI_RANKS` MPI ranks. Every
launched rank computes; history writes are synchronous. The default is four
ranks. WRF derives its own decomposition from the locked rank count, so no
runtime variable can change an already planned layout.

## Environment

| Variable | Default | Purpose |
| :--- | --- | --- |
| `AV_WRF_MPI_RANKS` | `4` | Total MPI ranks passed to the launcher and frozen in the RunLock. |
| `AV_WRF_MPI_LAUNCHER` | `mpirun -np` | Launcher and rank flag; the runner appends ranks and executable. |

For a Slurm allocation, override the launcher:

```sh
export AV_WRF_MPI_LAUNCHER='srun --mpi=pmix -n'
export AV_WRF_MPI_RANKS=4
```

The runner appends the rank count and the solver executable, so the command is
`... -n 4 /opt/av-wrf/bin/wrf.exe`.

## Decomposition

WRF's pinned default mesh is the largest divisor of the rank count no greater
than `sqrt(ranks)` in x and the remainder in y. `check` and `plan` use the same
helper to reject a rank count that would create a patch narrower than WRF's
ten-cell minimum on any resolved domain.

| Ranks | Mesh |
| ---: | ---: |
| 1 | 1x1 |
| 2 | 1x2 |
| 3 | 1x3 |
| 4 | 2x2 |
| 8 | 2x4 |
| 12 | 3x4 |
| 16 | 4x4 |

The RunLock records the rank count. Namelists omit `nproc_x`/`nproc_y` and set
`nio_tasks_per_group = 0`, so the pinned native implementation owns the layout.

## Qualification

Qualify two ranks with one OpenMP thread per rank on the target host first, then
compare four ranks. Admit higher counts only after measured memory and elapsed
time fit the operating policy. WPS stages currently run serially; measure any
future parallel WPS change separately.
