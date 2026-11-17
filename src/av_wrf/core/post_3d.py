"""3D thermodynamics, cloud and momentum slab construction for the Published Grid."""

from __future__ import annotations

from array import array

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import (
    CompMass2D,
    CompMass3D,
    DomainProfile,
    PubMass2D,
    PubMass3D,
    RawWrfStepInput,
    ScienceProfile,
    Slab2DF32,
    Slab3DF32,
)
from av_wrf.core.grid import (
    _crop_sponge_2d_view,
    crop_destagger_x_3d,
    crop_destagger_y_3d,
    crop_destagger_z_3d,
    lambert_map_scale,
)
from av_wrf.core.thermo import (
    G0_MPS2,
    MIN_WATER_VAPOR_KGKG,
    afwa_mslp_vec,
    geometric_w_to_omega_vec,
    groom_f32_buf,
    max_random_cloud_cover_pct,
    mixing_ratio_to_spfh_vec,
    potential_to_temp_vec,
    rotate_grid_to_earth_vec,
    validate_thermodynamic_field,
)
from av_wrf.core.vertical import model_column_spacing, staggered_face_phi

type ArrF64 = npt.NDArray[np.float64]
type SharedSurface = tuple[ArrF64, ArrF64, ArrF64, ArrF64]


def as_f64(buf: array[float], shape: tuple[int, ...], label: str | None = None) -> ArrF64:
    arr = np.frombuffer(buf, dtype=np.float32).reshape(shape)
    if label is not None and not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"Non-finite value encountered in {label}")
    return arr.astype(np.float64)


def _mass_view(slab: CompMass3D, trim: int, shape: tuple[int, int, int]) -> npt.NDArray[np.float32]:
    nz, ny, nx = slab.nz, slab.ny, slab.nx
    sub = np.frombuffer(slab.buf, dtype=np.float32).reshape(nz, ny, nx)
    sub = sub[: shape[0], trim:-trim, trim:-trim]
    if sub.shape != shape:
        raise ValueError(f"Cropped mass volume shape {sub.shape} != {shape}")
    return sub


def crop2_f64(slab: CompMass2D, trim: int, label: str, *, nonnegative: bool = False) -> ArrF64:
    view = _crop_sponge_2d_view(slab, trim)
    if not bool(np.all(np.isfinite(view))):
        raise ValueError(f"Non-finite value encountered in {label}")
    if nonnegative and np.any(view < 0.0):
        raise ValueError(f"Negative value encountered in {label}")
    return view.astype(np.float64)


def groom_2d(values: ArrF64, keepbits: int) -> PubMass2D:
    ny, nx = values.shape
    return PubMass2D(Slab2DF32(ny, nx, groom_f32_buf(values, keepbits)))


def _groomed_order_message(
    comparison: str,
    valid_time_utc: str,
    lead_h: int | None,
    k: int,
    y: int,
    x: int,
    trim: int,
    left_pa: float,
    right_pa: float,
) -> str:
    # Published y/x omit bdy_trim_cells on each edge, so wrfout mass indices are y+trim, x+trim.
    lead = "" if lead_h is None else f" lead_h={lead_h}"
    gap_pa = left_pa - right_pa
    return (
        "Bit-groomed 3D column pressure: "
        f"{comparison} at {valid_time_utc}{lead} "
        f"k={k} y={y} x={x} wrfout_y={y + trim} wrfout_x={x + trim}: "
        f"{left_pa:.3f} Pa vs {right_pa:.3f} Pa, gap {gap_pa:.3f} Pa"
    )


def _quantum17(values: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """Return the 17-bit quantum 2^(exponent-17) for each positive float32 value."""
    grid = np.ascontiguousarray(values, dtype=np.float32)
    # Float32 stores a bias-127 exponent. Keeping 17 mantissa bits leaves 2^(e-17).
    exp = ((grid.view(np.uint32) >> np.uint32(23)) & np.uint32(0xFF)).astype(np.int32)
    return np.ldexp(np.float32(1.0), exp - 144).astype(np.float32)


def _step_below(groomed: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """Return the next lower 17-bit value, using the bin below an exact power of two."""
    grid = np.ascontiguousarray(groomed, dtype=np.float32)
    # Quantize first so an off-grid neighbor cannot publish a value between grid points.
    buf = groom_f32_buf(grid.astype(np.float64), 17)
    quantized = np.frombuffer(buf, np.float32).reshape(grid.shape).copy()
    at_pow2 = (quantized.view(np.uint32) & np.uint32(0x7FFFFF)) == np.uint32(0)
    quantum = _quantum17(quantized)
    step = np.where(at_pow2, quantum * np.float32(0.5), quantum)
    return quantized - step.astype(np.float32)


def _raise_hard_groomed_gap(
    raw: RawWrfStepInput,
    upper: npt.NDArray[np.float32],
    lower: npt.NDArray[np.float32],
    k: int,
    trim: int,
    lead_h: int | None,
    comparison: str,
) -> None:
    """Reject a groomed cross larger than one 17-bit quantum of the greater value."""
    excess = np.subtract(upper, lower, dtype=np.float64)
    hard = (upper >= lower) & (excess > _quantum17(np.maximum(upper, lower)))
    if not bool(np.any(hard)):
        return
    y, x = (int(v) for v in np.argwhere(hard)[0])
    raise ValueError(
        _groomed_order_message(
            comparison,
            raw.valid_time_utc,
            lead_h,
            k,
            y,
            x,
            trim,
            float(upper[y, x]),
            float(lower[y, x]),
        )
    )


def _snap_below(upper: npt.NDArray[np.float32], lower: npt.NDArray[np.float32]) -> None:
    crossed = upper >= lower
    if bool(np.any(crossed)):
        np.copyto(upper, _step_below(lower), where=crossed)


def _snap_groomed_column(
    raw: RawWrfStepInput,
    p_gr: npt.NDArray[np.float32],
    psfc: ArrF64,
    trim: int,
    lead_h: int | None,
) -> None:
    """Publish a strictly decreasing 17-bit column below PSFC.

    Hard gaps use the original groomed values. The snap then walks upward, so a
    legal surface step cannot turn the next one-quantum tie into a false fold.
    """
    surface = np.asarray(psfc, dtype=np.float32)
    _raise_hard_groomed_gap(raw, p_gr[0], surface, 0, trim, lead_h, "lowest level >= surface")
    for level in range(1, p_gr.shape[0]):
        _raise_hard_groomed_gap(
            raw,
            p_gr[level],
            p_gr[level - 1],
            level,
            trim,
            lead_h,
            f"level {level} >= level {level - 1}",
        )
    _snap_below(p_gr[0], surface)
    for level in range(1, p_gr.shape[0]):
        _snap_below(p_gr[level], p_gr[level - 1])


def _validate_and_groom_3d_pressure(
    raw: RawWrfStepInput,
    psfc_2d: ArrF64,
    trim: int,
    k_exp: int,
    lead_h: int | None,
) -> tuple[ArrF64, array[float]]:
    ny, nx = psfc_2d.shape
    shape = (raw.cldfra.nz, ny, nx)
    p_pa = np.add(
        _mass_view(raw.p_pert_pa, trim, shape),
        _mass_view(raw.pb_pa, trim, shape),
        dtype=np.float64,
    )
    if not np.all(p_pa[-1] > 0.0):
        raise ValueError("Non-positive 3D pressure in post_frame")
    if not np.all(p_pa[1:] < p_pa[:-1]):
        raise ValueError("Non-monotonic 3D column pressure (pressure must decrease with height)")
    p_buf = groom_f32_buf(p_pa[:k_exp], 17)
    p_gr = np.frombuffer(p_buf, dtype=np.float32).reshape(k_exp, ny, nx)
    _snap_groomed_column(raw, p_gr, psfc_2d, trim, lead_h)
    return p_pa, p_buf


def _post_mslp(
    raw: RawWrfStepInput, trim: int, first_height: ArrF64, first_temp: ArrF64, first_qv: ArrF64
) -> PubMass2D:
    mslp = (
        np.zeros_like(first_height)
        if raw.afwa_mslp_pa is None
        else crop2_f64(raw.afwa_mslp_pa, trim, "AFWA_MSLP")
    )
    missing = mslp <= 0.0
    if np.any(missing):
        mslp[missing] = afwa_mslp_vec(
            _crop_sponge_2d_view(raw.psfc_pa, trim)[missing].astype(np.float64),
            _crop_sponge_2d_view(raw.terrain_m, trim)[missing].astype(np.float64),
            first_height[missing],
            first_temp[missing],
            first_qv[missing],
        )
    return groom_2d(mslp, 17)


def _cloud_and_thermo(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    shared_2d: SharedSurface,
    lead_h: int | None,
) -> tuple[
    ArrF64,
    array[float],
    array[float],
    array[float],
    ArrF64,
    ArrF64,
    dict[str, PubMass2D],
]:
    _, psfc, _, _ = shared_2d
    trim, k_exp = domain.bdy_trim_cells, domain.exported_levels_k
    ny, nx = psfc.shape
    shp3 = (k_exp, ny, nx)
    p_all, p_buf = _validate_and_groom_3d_pressure(raw, psfc, trim, k_exp, lead_h)
    cloud_shape = (raw.cldfra.nz, ny, nx)
    cloud = _mass_view(raw.cldfra, trim, cloud_shape)
    # Cloud fraction is bounded by definition; tolerate only float32 round-off.
    if bool(np.any(cloud < 0.0)) or bool(np.any(cloud > 1.0 + 1e-6)):
        raise ValueError("CLDFRA outside [0, 1] in post_frame")
    cld_tot, cld_low = max_random_cloud_cover_pct(
        cloud, p_all, _crop_sponge_2d_view(raw.psfc_pa, trim)
    )
    p_pa = p_all[:k_exp]
    ph = np.frombuffer(crop_destagger_z_3d(raw.ph_stag_m2s2, trim, k_exp).buf, np.float32)
    phb = np.frombuffer(crop_destagger_z_3d(raw.phb_stag_m2s2, trim, k_exp).buf, np.float32)
    height_m = np.add(ph, phb, dtype=np.float64).reshape(shp3) / G0_MPS2
    z_buf = groom_f32_buf(height_m, 17)
    th = _mass_view(raw.theta_pert_k, trim, shp3).astype(np.float64)
    tk = potential_to_temp_vec(th, p_pa)
    t_buf = groom_f32_buf(tk, 15)
    validate_thermodynamic_field("temp_k", np.frombuffer(t_buf, np.float32))
    qv_in = _mass_view(raw.qvapor_kgkg, trim, shp3).astype(np.float64)
    if bool(np.any(qv_in < MIN_WATER_VAPOR_KGKG)):
        raise ValueError("Large negative QVAPOR encountered in post_frame")
    spfh = mixing_ratio_to_spfh_vec(qv_in)
    if bool(np.any(spfh >= 1.0)):
        raise ValueError("Invalid specific humidity in 3D column")
    columns = {
        "cloud_total_pct": groom_2d(cld_tot, 15),
        "cloud_low_pct": groom_2d(cld_low, 15),
        "mslp_pa": _post_mslp(raw, trim, height_m[0], tk[0], qv_in[0]),
    }
    return p_pa, p_buf, z_buf, t_buf, tk, spfh, columns


def _wind_stats(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
    shared_2d: SharedSurface,
    thermo: tuple[ArrF64, array[float], ArrF64, ArrF64, ArrF64],
) -> tuple[ArrF64, ArrF64, ArrF64, tuple[float, float, int, float]]:
    terr, _, ca, sa = shared_2d
    p_pa, z_buf, tk, spfh, phi = thermo
    trim, k_exp = domain.bdy_trim_cells, domain.exported_levels_k
    ny, nx = ca.shape
    shp3 = (k_exp, ny, nx)
    w_m = as_f64(crop_destagger_z_3d(raw.w_stag_mps, trim, k_exp).buf, shp3)
    omega = geometric_w_to_omega_vec(w_m, p_pa, tk, spfh)
    u_g = as_f64(crop_destagger_x_3d(raw.u_stag_mps, trim, k_exp).buf, shp3)
    v_g = as_f64(crop_destagger_y_3d(raw.v_stag_mps, trim, k_exp).buf, shp3)
    u_e, v_e = rotate_grid_to_earth_vec(u_g, v_g, ca, sa)
    agl = np.subtract(np.frombuffer(z_buf, np.float32).reshape(shp3), terr)
    dz, bl_n = model_column_spacing(agl)
    # True Eulerian thickness differs from the consumer midpoint gap used for admission.
    # Face validity is checked before destaggering; this denominator floor cannot hide folds.
    dz_cell = np.maximum(1.0, (phi[1:] - phi[:-1]) / G0_MPS2)
    # dx_phys = dx / m, so the Courant proxy needs a Lambert scale (center-column proxy).
    m_scale = lambert_map_scale(
        domain.center_lat_deg,
        std_parallel_1_deg=domain.std_parallel_1_deg,
        std_parallel_2_deg=domain.std_parallel_2_deg,
        origin_lat_deg=domain.center_lat_deg,
        radius_m=domain.earth_radius_m,
    )
    dt_s = float(science.time_step_s if raw.dt_s is None else raw.dt_s)
    cfl = ((np.abs(u_g) + np.abs(v_g)) * m_scale / domain.dx_m + np.abs(w_m) / dz_cell) * dt_s
    stats = (float(np.min(dz)), float(np.max(dz)), bl_n, round(float(np.max(cfl)), 5))
    return u_e, v_e, omega, stats


def post_3d_slabs(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
    shared_2d: SharedSurface,
    *,
    lead_h: int | None = None,
) -> tuple[dict[str, PubMass3D], dict[str, PubMass2D], tuple[float, float, int, float]]:
    k_exp = domain.exported_levels_k
    phi = staggered_face_phi(raw, domain.bdy_trim_cells, k_exp)
    p_pa, p_buf, z_buf, t_buf, tk, spfh, columns = _cloud_and_thermo(raw, domain, shared_2d, lead_h)
    u_e, v_e, omega, stats = _wind_stats(
        raw, domain, science, shared_2d, (p_pa, z_buf, tk, spfh, phi)
    )
    ny, nx = shared_2d[1].shape
    bufs = (
        ("pressure_pa", p_buf),
        ("height_msl_m", z_buf),
        ("temp_k", t_buf),
        ("spfh_kgkg", groom_f32_buf(spfh, 15)),
        ("u_earth_mps", groom_f32_buf(u_e, 15)),
        ("v_earth_mps", groom_f32_buf(v_e, 15)),
        ("omega_pas", groom_f32_buf(omega, 15)),
    )
    f3d = {nm: PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=b)) for nm, b in bufs}
    return f3d, columns, stats


__all__ = ["SharedSurface", "as_f64", "crop2_f64", "groom_2d", "post_3d_slabs"]
