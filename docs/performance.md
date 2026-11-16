# Parallel Scaling and I/O

`av-wrf` launches `real.exe` and `wrf.exe` with `AV_WRF_MPI_RANKS` MPI ranks and
decomposes each active domain into `nproc_x` by `nproc_y` patches. The default is
sixteen ranks. Every patch stays at or above WRF's ten-cell minimum.

## Environment

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `AV_WRF_MPI_RANKS` | `16` | Total MPI ranks passed to the launcher. |
| `AV_WRF_NPROC_X` | unset | Manual integration mesh in x. Set with `AV_WRF_NPROC_Y`. |
| `AV_WRF_NPROC_Y` | unset | Manual integration mesh in y. |
| `AV_WRF_MPI_LAUNCHER` | `mpirun -np` | Launcher and rank flag; the runner appends ranks and executable. |

When both `nproc` variables are unset, the compiler chooses a near-square
factor pair that keeps every patch at least ten cells wide, prefers even counts,
and places the smaller factor in x. The manual mesh applies to the integration
communicator: with quilting enabled, `nproc_x * nproc_y` must equal
`AV_WRF_MPI_RANKS - 1`. `real.exe` disables quilting and decomposes all launched
ranks.

For a Slurm allocation, override the launcher:

```sh
export AV_WRF_MPI_LAUNCHER='srun --mpi=pmix -n'
export AV_WRF_MPI_RANKS=16
```

The runner appends the rank count and the solver executable, so the command is
`... -n 16 /opt/av-wrf/bin/wrf.exe`.

## I/O Quilting

Integration reserves one rank per group for asynchronous history writes:
`nio_tasks_per_group = 1` and `nio_groups = 1`. Compute ranks proceed while the
I/O rank writes. Single-rank runs disable quilting. Full asynchronous quilting
with multiple groups is future work.

## Rank Accounting

`real.exe` uses every launched rank for compute. `wrf.exe` uses
`AV_WRF_MPI_RANKS - 1` compute ranks plus one I/O rank. Run planning validates
both decompositions against the child and any nested parent, so an invalid mesh
fails before the solver starts. A single-rank Docker check overrides
`AV_WRF_MPI_RANKS=1`.
