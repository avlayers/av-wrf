"""Compile WRF v4.8 Fortran namelists from validated domain, science, and forcing profiles."""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from math import isqrt

from av_wrf.contracts import (
    CycleWindow,
    DomainProfile,
    ForcingProfile,
    RunLock,
    ScienceProfile,
    parse_iso_utc,
)
from av_wrf.core.nesting import nesting_active, parent_domain_shape
from av_wrf.core.physics_namelist import (
    NamelistSections,
    NamelistValue,
    adaptive_domains,
    per_domain,
    physics_and_dynamics_sections,
    validate_science_profile,
)

MYNN_CLOUD_FIELD = "CLDFRA_BL"
MYNN_HISTORY_FILE = "mynn-history.txt"
MYNN_HISTORY_TEXT = f"+:h:0:{MYNN_CLOUD_FIELD}\n"

# WRF's RSL_LITE communicator requires at least ten cells per patch in each direction.
# https://github.com/wrf-model/WRF/blob/v4.8.0/external/RSL_LITE/module_dm.F
MIN_PATCH_CELLS = 10


def mpi_decomposition(e_we: int, e_sn: int, ranks: int) -> tuple[int, int]:
    """Return a near-square WRF `(nproc_x, nproc_y)` mesh that multiplies to `ranks`."""
    if isinstance(ranks, bool) or ranks < 1:
        raise ValueError("MPI decomposition ranks must be >= 1")
    if e_we < MIN_PATCH_CELLS or e_sn < MIN_PATCH_CELLS:
        raise ValueError(f"domain is narrower than WRF's {MIN_PATCH_CELLS}-cell minimum patch")
    if ranks == 1:
        return 1, 1
    best: tuple[tuple[float, int, int, int], tuple[int, int]] | None = None
    for factor in range(1, isqrt(ranks) + 1):
        if ranks % factor:
            continue
        pairs = ((factor, ranks // factor), (ranks // factor, factor))
        for nproc_x, nproc_y in pairs:
            if e_we // nproc_x < MIN_PATCH_CELLS or e_sn // nproc_y < MIN_PATCH_CELLS:
                continue
            # Near-square patches first, even counts next, then WRF's smaller x factor.
            score = (abs(e_we / nproc_x - e_sn / nproc_y), nproc_x % 2, nproc_y % 2, nproc_x)
            if best is None or score < best[0]:
                best = (score, (nproc_x, nproc_y))
    if best is None:
        raise ValueError(
            f"{ranks} MPI ranks create a WRF patch narrower than {MIN_PATCH_CELLS} cells"
        )
    return best[1]


def _decomposition(
    e_we: int, e_sn: int, mpi_ranks: int, nproc: tuple[int, int] | None
) -> tuple[int, int]:
    if nproc is None:
        return mpi_decomposition(e_we, e_sn, mpi_ranks)
    nproc_x, nproc_y = nproc
    if nproc_x < 1 or nproc_y < 1 or nproc_x * nproc_y != mpi_ranks:
        raise ValueError(
            f"AV_WRF_NPROC_X * AV_WRF_NPROC_Y ({nproc_x}x{nproc_y}) must equal {mpi_ranks}"
        )
    if e_we // nproc_x < MIN_PATCH_CELLS or e_sn // nproc_y < MIN_PATCH_CELLS:
        raise ValueError("AV_WRF_NPROC_X/Y create a WRF patch narrower than 10 cells")
    return nproc_x, nproc_y


def _add_decomposition(
    domains: dict[str, NamelistValue],
    shape_we: int,
    shape_sn: int,
    mpi_ranks: int,
    nproc: tuple[int, int] | None,
) -> None:
    if mpi_ranks <= 1:
        return
    nproc_x, nproc_y = _decomposition(shape_we, shape_sn, mpi_ranks, nproc)
    domains.update(nproc_x=nproc_x, nproc_y=nproc_y)


def _wrf_domains(
    domain: DomainProfile,
    science: ScienceProfile,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None,
    metgrid_soil_levels: int | None,
    nested: bool,
    mpi_ranks: int,
    nproc: tuple[int, int] | None,
) -> dict[str, NamelistValue]:
    max_dom, ratio = (2, domain.parent_grid_ratio) if nested else (1, 1)
    mg_levels = forcing.metgrid_levels if metgrid_levels is None else metgrid_levels
    soil_levels = (
        forcing.metgrid_soil_levels if metgrid_soil_levels is None else metgrid_soil_levels
    )
    if min(mg_levels, soil_levels) < 2 or forcing.interval_seconds < 1:
        raise ValueError("WRF requires at least two air and soil levels and a positive interval")
    e_we, e_sn = domain.e_we, domain.e_sn
    parent_we, parent_sn = parent_domain_shape(domain) if nested else (e_we, e_sn)
    dx, dy = domain.dx_m, domain.dy_m
    start = (1, domain.parent_halo_cells + 1) if nested else 1
    # The child clock is `time_step_s`; `parent_time_step_ratio` scales the d01 clock.
    time_step = science.time_step_s * ratio
    if nested and time_step // ratio != science.time_step_s:
        raise ValueError("parent_time_step_ratio must scale the child time step exactly")
    domains: dict[str, NamelistValue] = {
        "time_step": time_step,
        "max_dom": max_dom,
        "e_we": (parent_we, e_we) if nested else e_we,
        "e_sn": (parent_sn, e_sn) if nested else e_sn,
        "e_vert": per_domain(domain.e_vert, max_dom),
        "num_metgrid_levels": mg_levels,
        "num_metgrid_soil_levels": soil_levels,
        # WRF real interpolates specific humidity via its mixing-ratio conversion.
        **({"use_sh_qv": True} if forcing.use_sh_qv else {}),
        "dx": (dx * ratio, dx) if nested else dx,
        "dy": (dy * ratio, dy) if nested else dy,
        "grid_id": (1, 2) if nested else 1,
        "parent_id": (1, 1) if nested else 0,
        "i_parent_start": start,
        "j_parent_start": start,
        "parent_grid_ratio": (1, ratio) if nested else 1,
        "parent_time_step_ratio": (1, ratio) if nested else 1,
        # One-way nesting: child states never feed back into the parent.
        "feedback": 0,
        "smooth_option": 0,
        "p_top_requested": domain.p_top_pa,
        "auto_levels_opt": domain.auto_levels_opt,
        "dzbot": domain.dzbot_m,
        "dzstretch_s": domain.dzstretch_s,
        "dzstretch_u": domain.dzstretch_u,
        "max_dz": domain.max_dz_m,
    }
    # One mesh serves every domain, so the smaller of child and parent is binding.
    _add_decomposition(domains, min(e_we, parent_we), min(e_sn, parent_sn), mpi_ranks, nproc)
    if science.use_adaptive_time_step:
        domains.update(adaptive_domains(science, ratio))
    return domains


def compile_wrf_namelist_sections(
    domain: DomainProfile,
    science: ScienceProfile,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None = None,
    metgrid_soil_levels: int | None = None,
    nested: bool = False,
    mpi_ranks: int = 1,
    nproc: tuple[int, int] | None = None,
) -> NamelistSections:
    """Compile validated profiles into WRF namelist sections (1 or 2 concentric domains)."""
    validate_science_profile(science, domain)
    if forcing.metgrid_levels < 20 and domain.e_vert > 40:
        warnings.warn(
            f"Forcing {forcing.profile_id} supplies {forcing.metgrid_levels} metgrid levels "
            f"(< 20) for a {domain.e_vert}-level hybrid column; initialization is coarse",
            UserWarning,
            stacklevel=2,
        )
    max_dom = 2 if nested else 1
    return {
        "time_control": {
            "interval_seconds": forcing.interval_seconds,
            "history_interval": per_domain(science.history_interval_min, max_dom),
            "frames_per_outfile": per_domain(1, max_dom),
            "input_from_file": per_domain(True, max_dom),
        },
        "domains": _wrf_domains(
            domain,
            science,
            forcing,
            metgrid_levels=metgrid_levels,
            metgrid_soil_levels=metgrid_soil_levels,
            nested=nested,
            mpi_ranks=mpi_ranks,
            nproc=nproc,
        ),
        **physics_and_dynamics_sections(science, max_dom),
        "bdy_control": {
            # Parent d01 is driven by met_em; only the nested child is relaxed from d01.
            "specified": (True, False) if nested else True,
            "nested": (False, True) if nested else False,
            "spec_bdy_width": science.spec_bdy_width,
            "spec_zone": science.spec_zone,
            "relax_zone": science.relax_zone,
        },
        # One I/O task per group overlaps history writes with the compute ranks. The
        # launcher must supply nproc_x*nproc_y compute ranks plus these I/O tasks.
        # https://github.com/wrf-model/WRF/blob/v4.8.0/frame/module_io_quilt_old.F#L2474-L2480
        "namelist_quilt": {
            "nio_tasks_per_group": 1 if mpi_ranks > 1 else 0,
            "nio_groups": 1,
        },
    }


def forcing_bracket_duration_s(window: RunLock | CycleWindow, interval_s: int) -> int:
    """Return elapsed seconds from WRF initialization through the final source bracket."""
    if interval_s <= 0 or interval_s % 3600:
        raise ValueError("Forcing interval must be a positive whole-hour multiple")
    origin = parse_iso_utc(window.publish_origin_utc)
    init = parse_iso_utc(window.wrf_init_utc)
    total_s = int((origin - init).total_seconds()) + max(window.lead_hours) * 3600
    return ((total_s + interval_s - 1) // interval_s) * interval_s


def _run_time_control(
    start_dt: datetime,
    end_dt: datetime,
    total_hours: int,
    begin: NamelistValue,
    max_dom: int,
    base: dict[str, NamelistValue],
) -> dict[str, NamelistValue]:
    return {
        "run_days": 0,
        "run_hours": total_hours,
        "run_minutes": 0,
        "run_seconds": 0,
        "start_year": per_domain(start_dt.year, max_dom),
        "start_month": per_domain(start_dt.month, max_dom),
        "start_day": per_domain(start_dt.day, max_dom),
        "start_hour": per_domain(start_dt.hour, max_dom),
        "end_year": per_domain(end_dt.year, max_dom),
        "end_month": per_domain(end_dt.month, max_dom),
        "end_day": per_domain(end_dt.day, max_dom),
        "end_hour": per_domain(end_dt.hour, max_dom),
        "history_begin_h": begin,
        "io_form_history": 2,
        "io_form_restart": 2,
        "io_form_input": 2,
        "io_form_boundary": 2,
        **base,
    }


def compile_wrf_run_namelist(
    domain: DomainProfile,
    science: ScienceProfile,
    lock: RunLock,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None = None,
    metgrid_soil_levels: int | None = None,
    for_real: bool = False,
    nproc: tuple[int, int] | None = None,
) -> NamelistSections:
    """Attach RunLock UTC calendar and history_begin_h onto WRF namelist.input sections."""
    nested = nesting_active(domain, forcing.grid_spacing_deg)
    # Integration reserves one rank for I/O quilting; real.exe disables quilting and
    # therefore decomposes the full launched rank count.
    compute_ranks = lock.mpi_ranks if for_real or lock.mpi_ranks <= 1 else lock.mpi_ranks - 1
    base = compile_wrf_namelist_sections(
        domain,
        science,
        forcing,
        metgrid_levels=metgrid_levels,
        metgrid_soil_levels=metgrid_soil_levels,
        nested=nested,
        mpi_ranks=compute_ranks,
        nproc=None if for_real else nproc,
    )
    start_dt = parse_iso_utc(lock.wrf_init_utc)
    total_hours = lock.spinup_hours + max(lock.lead_hours)
    # real.exe reads end_* rather than run_hours to determine how many met_em files to open.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/run/README.namelist
    end_s = (
        forcing_bracket_duration_s(lock, forcing.interval_seconds)
        if for_real
        else (total_hours * 3600)
    )
    end_dt = start_dt + timedelta(seconds=end_s)
    # WRF adapts the parent clock modulo its history cadence. Delay writes independently.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/adapt_timestep_em.F#L321-L355
    if nested:
        parent_begin = (end_s // 3600 + 1, lock.spinup_hours)
        if parent_begin[0] <= total_hours:
            raise ValueError("Nested parent history must begin after the integration ends")
        begin: NamelistValue = parent_begin
    else:
        begin = lock.spinup_hours
    max_dom = 2 if nested else 1
    base["time_control"] = _run_time_control(
        start_dt, end_dt, total_hours, begin, max_dom, base["time_control"]
    )
    if not for_real and science.bl_pbl_physics == 5 and science.icloud_bl > 0:
        # The native Registry includes these MYNN fields only on the restart stream.
        base["time_control"]["iofields_filename"] = per_domain(MYNN_HISTORY_FILE, max_dom)
        base["time_control"]["ignore_iofields_warning"] = False
    return base


def _format_fortran_literal(val: NamelistValue) -> str:
    if isinstance(val, tuple):
        return ", ".join(_format_fortran_literal(item) for item in val)
    if isinstance(val, bool):
        return ".true." if val else ".false."
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    return str(val)


def render_fortran_namelist(sections: NamelistSections) -> str:
    """Render namelist dictionary sections into canonical Fortran `&section ... /` text."""
    lines: list[str] = []
    for sec_name, mapping in sections.items():
        lines.append(f"&{sec_name}")
        lines.extend(f" {key} = {_format_fortran_literal(val)}," for key, val in mapping.items())
        lines.extend((" /", ""))
    return "\n".join(lines)


__all__ = [
    "MIN_PATCH_CELLS",
    "MYNN_CLOUD_FIELD",
    "MYNN_HISTORY_FILE",
    "MYNN_HISTORY_TEXT",
    "NamelistSections",
    "NamelistValue",
    "compile_wrf_namelist_sections",
    "compile_wrf_run_namelist",
    "forcing_bracket_duration_s",
    "mpi_decomposition",
    "render_fortran_namelist",
    "validate_science_profile",
]
