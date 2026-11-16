"""Validate `met_em` level and field inventory against the locked forcing window."""

from __future__ import annotations

from datetime import timedelta
from numbers import Integral
from pathlib import Path

from av_wrf.codecs import verify_grid_against_domain, verify_wps_fields
from av_wrf.contracts import RunLock
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.geog import geog_domain_profiles
from av_wrf.runner.mirrors import resolve_forcing_window


def inspect_metgrid_dims(attempt_dir: Path, b: LoadedBundle, lock: RunLock) -> tuple[int, int]:
    """Return the observed air and soil level counts shared by every met_em file."""
    import netCDF4

    recipe, cycle, hours = resolve_forcing_window(lock, b.primary_source, domain=b.domain)
    dates = tuple((cycle + timedelta(hours=h)).strftime("%Y-%m-%d_%H:%M:%S") for h in hours)
    files = {
        attempt_dir / f"met_em.{tag}.{date}.nc": (profile, date)
        for index, (tag, profile) in enumerate(geog_domain_profiles(b))
        for date in (dates[:1] if index else dates)
    }
    if {p for p in attempt_dir.glob("met_em.d*.*.nc")} != set(files):
        raise RuntimeError("metgrid timestamps differ from the RunLock forcing window")
    dimensions = ("num_metgrid_levels", *recipe.metgrid_soil_dimensions)
    fields = recipe.metgrid_fields
    if b.science.sf_lake_physics == 1:
        fields = (*fields, "LAKE_DEPTH")
    dims_seen: set[tuple[tuple[int, ...], int]] = set()
    for path, (profile, date) in files.items():
        with netCDF4.Dataset(str(path), mode="r") as ds:
            if bytes(ds["Times"][0].tobytes()).decode("ascii") != date:
                raise ValueError(f"met_em Times does not match filename: {path}")
            try:
                counts = tuple(len(ds.dimensions[name]) for name in dimensions)
            except KeyError as exc:
                raise ValueError(f"Missing met_em dimension {exc} in {path}") from exc
            # WPS reports the soil temperature count used by real.exe in this attribute.
            # https://github.com/wrf-model/WRF/blob/v4.8.0/share/input_wrf.F#L612-L640
            soil_levels: object = getattr(ds, "NUM_METGRID_SOIL_LEVELS", None)
            if not isinstance(soil_levels, Integral) or int(soil_levels) < 2:
                raise ValueError(f"Invalid met_em NUM_METGRID_SOIL_LEVELS in {path}")
            if counts[0] < 2 or min(counts) < 1:
                raise ValueError(f"Invalid met_em level dimensions in {path}: {counts}")
            dims_seen.add((counts, int(soil_levels)))
            verify_wps_fields(ds, path, fields)
            verify_grid_against_domain(ds, profile, path, wps=True)
    if len(dims_seen) != 1:
        raise ValueError(f"Inconsistent met_em level dimensions across window: {dims_seen}")
    counts, wrf_soil_levels = dims_seen.pop()
    return counts[0], wrf_soil_levels
