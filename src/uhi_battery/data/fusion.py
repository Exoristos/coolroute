"""MODIS-anomaly spatiotemporal fusion → hourly 30 m LST.

Implements the R2 fallback approach:
1. Linear temporal interpolation of Landsat clear-sky overpasses → 30 m baseline.
2. MODIS daily anomaly (relative to nearest Landsat date) bilinearly resampled
   from 1 km to 30 m.
3. Fused daily = baseline + anomaly.
4. Diurnal cosine model (MODIS day/night) modulates to hourly resolution.

Assumptions (per R2):
- Landsat overpass ~10:30 local; MODIS day ~10:30 / night ~22:30.
- Diurnal peak at 14:00 local; cosine between MODIS day/night constrains amplitude.
- When MODIS night is unavailable, a default 5 °C diurnal amplitude is used.
- Missing MODIS days are filled via temporal linear interpolation (QC flag).

Reference: AC-009: clear-sky RMSE < 1.5 °C on Landsat overpass days.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

# ---------------------------------------------------------------------------
# Diurnal model constants (local solar time, hours)
# ---------------------------------------------------------------------------
_T_PEAK = 14.0  # hour of peak LST (2 PM)
_T_REF = 10.5  # Landsat / MODIS day reference hour (~10:30 AM)
_COS_REF = float(np.cos(2 * np.pi * (_T_REF - _T_PEAK) / 24))  # ≈ 0.6088
_DIURNAL_DENOM = 2.0 * _COS_REF  # ≈ 1.2176 — converts (day−night) to amplitude


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hour_of_day(times: np.ndarray) -> np.ndarray:
    """Fractional hour of day from numpy datetime64 array."""
    ns = times.astype("datetime64[ns]").astype("int64")
    return (ns % (24 * 3600 * 10**9)).astype(float) / (3600 * 10**9)


def _modis_daily_anomaly(
    modis_da: xr.DataArray, landsat_times: np.ndarray
) -> xr.DataArray:
    """MODIS anomaly per pixel: MODIS(day) − MODIS(nearest Landsat date).

    Parameters
    ----------
    modis_da : xr.DataArray  (time, y, x)
        Gap-filled daily MODIS LST.
    landsat_times : np.ndarray of datetime64
        Landsat overpass timestamps.

    Returns
    -------
    xr.DataArray  (time, y, x)  — same coords as *modis_da*.
    """
    # MODIS at Landsat dates (nearest MODIS obs)
    m_at_l = modis_da.sel(time=landsat_times, method="nearest").values  # (nL, y, x)

    # Nearest Landsat index for each MODIS timestamp
    l_ns = landsat_times.astype("datetime64[ns]").astype("int64")
    m_ns = modis_da.time.values.astype("datetime64[ns]").astype("int64")
    nearest = np.argmin(np.abs(m_ns[:, None] - l_ns[None, :]), axis=1)

    m_ref = m_at_l[nearest]  # (nM, y, x)
    anomaly_vals = modis_da.values - m_ref

    return xr.DataArray(
        anomaly_vals,
        dims=modis_da.dims,
        coords=modis_da.coords,
        name="anomaly",
    )


def _resample_2d(
    src: xr.DataArray, target_y: np.ndarray, target_x: np.ndarray
) -> xr.DataArray:
    """Bilinearly resample *src* to *target_y* × *target_x* spatial grid.

    Also coerces the time coordinate from ``datetime64[us]`` back to
    ``datetime64[ns]``.  xarray's spatial ``interp`` (via scipy) may silently
    degrade the time dtype to microseconds; downstream temporal operations
    (interp/sel) fail because ``dt64[us] != dt64[ns]``.  This coercion is
    intentional and required for correct behaviour everywhere this function
    is called.
    """
    result = src.interp(y=target_y, x=target_x, method="linear")
    # xarray spatial interp may degrade time dtype to us; coerce back to ns
    # so temporal interp/sel against ns-coords works (dt64[us] != dt64[ns]).
    if "time" in result.dims:
        result = result.assign_coords(
            time=result.time.values.astype("datetime64[ns]")
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fuse_to_hourly(
    landsat_da: xr.DataArray,
    modis_da: xr.DataArray,
    modis_night_da: xr.DataArray | None = None,
    target_resolution_m: int = 30,
) -> xr.DataArray:
    """Fuse Landsat (30 m) and MODIS (1 km) LST to hourly 30 m.

    Parameters
    ----------
    landsat_da : xr.DataArray
        Clear-sky Landsat LST (°C).  Dims (time, y, x), ~30 m.
    modis_da : xr.DataArray
        Daily MODIS LST day (°C).  Dims (time, y, x), ~1 km.
    modis_night_da : xr.DataArray | None
        Daily MODIS LST night (°C).  Same grid as *modis_da*.
        If ``None`` a default 5 °C diurnal amplitude is used.
    target_resolution_m : int
        Target resolution in metres (output matches *landsat_da* grid).

    Returns
    -------
    xr.DataArray
        Hourly fused LST (°C).  Dims (time, y, x), CRS EPSG:4326.
    """
    # ---- 1. Time grid ----------------------------------------------------------
    t0 = max(
        landsat_da.time.values.min(), modis_da.time.values.min()
    )
    t1 = min(
        landsat_da.time.values.max(), modis_da.time.values.max()
    )
    # When only one Landsat overpass exists, stretch to MODIS range (baseline
    # will be constant = the single scene, extrapolated via nearest).
    if t0 >= t1:
        t0 = modis_da.time.values.min()
        t1 = modis_da.time.values.max()
    hourly = pd.date_range(t0, t1, freq="h", inclusive="both")
    daily = pd.date_range(
        hourly[0].normalize(), hourly[-1].normalize(), freq="D"
    )
    # Ensure daily falls within MODIS range
    daily = daily[
        (daily >= modis_da.time.values.min())
        & (daily <= modis_da.time.values.max())
    ]
    # Cast to ns — pandas may produce us-resolution arrays that don't match
    # Landsat / MODIS ns coords.
    _hourly_ns = hourly.values.astype("datetime64[ns]")
    _daily_ns = daily.values.astype("datetime64[ns]")

    # ---- 2. Gap-fill MODIS (linear temporal) -----------------------------------
    modis = modis_da.interpolate_na(dim="time", method="linear")
    # Coerce to ns — pandas may yield us-resolution arrays that don't match
    # Landsat ns coords, breaking temporal interp / sel.
    modis = modis.assign_coords(time=modis.time.values.astype("datetime64[ns]"))
    if modis_night_da is not None:
        modis_night = modis_night_da.interpolate_na(dim="time", method="linear")
        modis_night = modis_night.assign_coords(
            time=modis_night.time.values.astype("datetime64[ns]")
        )
    else:
        modis_night = None

    # ---- 3. Landsat baseline at hourly resolution (linear temporal interp) -----
    if landsat_da.sizes["time"] >= 2:
        baseline_da = landsat_da.interp(time=_hourly_ns, method="linear")
    else:
        # Single overpass: extend constant value via nearest-neighbour reindex.
        baseline_da = landsat_da.reindex(time=_hourly_ns, method="nearest")

    # ---- 4. MODIS daily anomaly at 1 km ---------------------------------------
    anomaly_1km = _modis_daily_anomaly(modis, landsat_da.time.values)

    # ---- 5. Resample anomaly → 30 m -------------------------------------------
    landsat_y = landsat_da.y.values.astype(float)
    landsat_x = landsat_da.x.values.astype(float)
    anomaly_30m = _resample_2d(anomaly_1km, landsat_y, landsat_x)

    # ---- 6. Get anomaly for each target day -----------------------------------
    anomaly_daily = anomaly_30m.interp(time=_daily_ns, method="linear")
    anomaly_daily_vals = anomaly_daily.values  # (n_days, n_y, n_x)

    # Map each hour to its day index
    hour_dates = pd.DatetimeIndex(_hourly_ns).normalize().values
    day_idx = np.searchsorted(_daily_ns, hour_dates, side="right") - 1
    day_idx = np.clip(day_idx, 0, len(_daily_ns) - 1)
    anomaly_hourly_vals = anomaly_daily_vals[day_idx]  # (n_hours, n_y, n_x)

    # ---- 7. Fused = baseline + anomaly ----------------------------------------
    fused_vals = baseline_da.values + anomaly_hourly_vals

    # ---- 8. Diurnal modulation -------------------------------------------------
    hours_frac = _hour_of_day(_hourly_ns)  # (n_hours,)

    if modis_night is not None:
        # Fit per-pixel cosine model from MODIS day/night at 30 m
        m_day_30m = _resample_2d(modis, landsat_y, landsat_x)
        m_night_30m = _resample_2d(modis_night, landsat_y, landsat_x)

        m_day_daily = m_day_30m.interp(time=_daily_ns, method="linear")
        m_night_daily = m_night_30m.interp(time=_daily_ns, method="linear")

        m_day_h = m_day_daily.values[day_idx]  # (n_hours, n_y, n_x)
        m_night_h = m_night_daily.values[day_idx]

        mean_h = (m_day_h + m_night_h) / 2.0
        amp_h = (m_day_h - m_night_h) / _DIURNAL_DENOM

        cos_h = np.cos(2 * np.pi * (hours_frac - _T_PEAK) / 24).reshape(-1, 1, 1)
        diurnal = mean_h + amp_h * cos_h  # MODIS-predicted diurnal value at each hour
        diurnal_ref = mean_h + amp_h * _COS_REF  # MODIS-predicted value at ref hour

        # Shift diurnal so that at reference hour it matches fused_vals
        result = diurnal + (fused_vals - diurnal_ref)
    else:
        # Default diurnal: cosine scaled by 5 °C amplitude, anchored at ref hour
        cos_h = np.cos(2 * np.pi * (hours_frac - _T_PEAK) / 24).reshape(-1, 1, 1)
        diurnal_offset = 5.0 * (cos_h - _COS_REF)
        result = fused_vals + diurnal_offset

    # ---- 9. Build output DataArray --------------------------------------------
    _x_vals = landsat_da.x.values.astype(float)
    _y_vals = landsat_da.y.values.astype(float)
    out = xr.DataArray(
        result,
        dims=("time", "y", "x"),
        coords={
            "time": _hourly_ns,
            "y": landsat_da.y,
            "x": landsat_da.x,
        },
        name="lst",
        attrs={
            "units": "°C",
            "crs": "EPSG:4326",
            "resolution_m": target_resolution_m,
            "fusion_method": (
                "MODIS-anomaly + Landsat temporal interpolation + cosine diurnal"
            ),
            # ── HeatLayerSnapshot / LSTGrid contract (§4) ──
            "source": "fused",
            "generated_at": pd.Timestamp.now().isoformat(),
            "bbox": (
                float(_x_vals.min()),
                float(_y_vals.min()),
                float(_x_vals.max()),
                float(_y_vals.max()),
            ),
            "qc_mask_note": (
                "qc_mask pending real GEE QC; all pixels assumed valid until P1.5. "
                "Use build_qc_mask(fused_da) to get the placeholder boolean mask."
            ),
        },
    )
    return out


def _fill_spatial_edges(da: xr.DataArray) -> xr.DataArray:
    """Fill NaN at spatial edges where the source grid doesn't cover the target.

    Uses forward/backward fill along x then y, so edge pixels inherit the
    nearest valid value.  Does not require monotonically increasing coords
    (geospatial y is often north→south decreasing).
    """
    for dim in ("x", "y"):
        if dim in da.dims:
            da = da.ffill(dim).bfill(dim)
    return da


def _interp_time_robust(
    da: xr.DataArray, target_time: np.ndarray, tolerance: str = "1D"
) -> xr.DataArray:
    """Robust time interpolation: reindex+interpolate_na (avoids interp bug).

    xarray's ``interp(method="linear")`` can produce NaN at exact-match
    timestamps due to datetime dtype mismatches.  This helper uses
    ``reindex(method="nearest")`` to snap exact matches, then
    ``interpolate_na`` to fill gaps, then ``ffill/bfill`` for edges.
    """
    da = da.assign_coords(time=da.time.values.astype("datetime64[ns]"))
    result = da.reindex(time=target_time, method="nearest", tolerance=pd.Timedelta(tolerance))
    result = result.interpolate_na(dim="time", method="linear")
    result = result.ffill("time").bfill("time")
    return result


def fuse_to_daily(
    landsat_da: xr.DataArray,
    modis_da: xr.DataArray,
    modis_night_da: xr.DataArray | None = None,
    target_resolution_m: int = 30,
) -> xr.Dataset:
    """Fuse Landsat (30 m) + MODIS (1 km) → daily LST + diurnal parameters.

    Memory-efficient alternative to :func:`fuse_to_hourly`.  Produces daily-
    resolution LST at the reference hour (~10:30) plus per-pixel diurnal mean
    and amplitude.  Hourly LST at any (day, hour) is reconstructed on demand:

        LST(day, hour) = diurnal_mean + diurnal_amp * cos(2π(h−14)/24)
                         + (lst_daily − diurnal_mean − diurnal_amp * _COS_REF)

    Memory: O(n_days × n_y × n_x) — ~120 MB for a 184-day, 335×484 grid in
    float32, vs ~5.7 GB per array for the full hourly version.

    Parameters
    ----------
    landsat_da, modis_da, modis_night_da, target_resolution_m
        See :func:`fuse_to_hourly`.

    Returns
    -------
    xr.Dataset
        Variables: ``lst_daily``, ``diurnal_mean``, ``diurnal_amp``
        (all float32, dims (time, y, x)).
    """
    # Cast to float32 to halve memory
    landsat_da = landsat_da.astype(np.float32)
    modis_da = modis_da.astype(np.float32)
    if modis_night_da is not None:
        modis_night_da = modis_night_da.astype(np.float32)

    # ---- 1. Daily time grid ---------------------------------------------------
    t0 = max(landsat_da.time.values.min(), modis_da.time.values.min())
    t1 = min(landsat_da.time.values.max(), modis_da.time.values.max())
    if t0 >= t1:
        t0 = modis_da.time.values.min()
        t1 = modis_da.time.values.max()
    daily = pd.date_range(t0, t1, freq="D")
    _daily_ns = daily.values.astype("datetime64[ns]")

    # ---- 2. Gap-fill MODIS (linear temporal) ----------------------------------
    modis = modis_da.interpolate_na(dim="time", method="linear")
    modis = modis.assign_coords(time=modis.time.values.astype("datetime64[ns]"))

    # ---- 3. Landsat baseline at daily resolution (NaN-aware) ------------------
    # Cast time to ns to match _daily_ns (avoids interp dtype mismatch).
    landsat_ns = landsat_da.assign_coords(
        time=landsat_da.time.values.astype("datetime64[ns]")
    )
    if landsat_da.sizes["time"] >= 2:
        # reindex+interpolate_na is more robust than interp(method="linear"),
        # which can produce NaN at exact-match timestamps due to dtype issues.
        baseline_da = landsat_ns.reindex(
            time=_daily_ns, method="nearest", tolerance=pd.Timedelta("1D")
        )
        baseline_da = baseline_da.interpolate_na(dim="time", method="linear")
        baseline_da = baseline_da.ffill("time").bfill("time")
    else:
        baseline_da = landsat_ns.reindex(time=_daily_ns, method="nearest")
        baseline_da = baseline_da.ffill("time").bfill("time")

    # ---- 4. MODIS daily anomaly at 1 km ---------------------------------------
    anomaly_1km = _modis_daily_anomaly(modis, landsat_da.time.values)

    # ---- 5. Resample anomaly → 30 m (fill spatial edge NaN) -------------------
    landsat_y = landsat_da.y.values.astype(float)
    landsat_x = landsat_da.x.values.astype(float)
    anomaly_30m = _fill_spatial_edges(_resample_2d(anomaly_1km, landsat_y, landsat_x))

    # ---- 6. Anomaly at target days --------------------------------------------
    anomaly_daily = _interp_time_robust(anomaly_30m, _daily_ns)

    # ---- 7. Fused daily = baseline + anomaly ---------------------------------
    fused_daily = baseline_da + anomaly_daily

    # ---- 8. Diurnal parameters from MODIS day/night ---------------------------
    if modis_night_da is not None:
        modis_night = modis_night_da.interpolate_na(dim="time", method="linear")
        modis_night = modis_night.assign_coords(
            time=modis_night.time.values.astype("datetime64[ns]")
        )
        m_day_30m = _fill_spatial_edges(_resample_2d(modis, landsat_y, landsat_x))
        m_night_30m = _fill_spatial_edges(_resample_2d(modis_night, landsat_y, landsat_x))
        m_day_daily = _interp_time_robust(m_day_30m, _daily_ns)
        m_night_daily = _interp_time_robust(m_night_30m, _daily_ns)
        diurnal_mean = (m_day_daily + m_night_daily) / 2.0
        diurnal_amp = (m_day_daily - m_night_daily) / _DIURNAL_DENOM
    else:
        diurnal_mean = fused_daily.copy()
        diurnal_amp = xr.full_like(fused_daily, 5.0)

    # Fill any remaining NaN in fused_daily (permanently cloudy pixels) with
    # the MODIS-derived diurnal mean at the reference hour.
    diurnal_ref = diurnal_mean + diurnal_amp * _COS_REF
    fused_daily = fused_daily.fillna(diurnal_ref)

    # ---- 9. Build output Dataset ----------------------------------------------
    _x_vals = landsat_da.x.values.astype(float)
    _y_vals = landsat_da.y.values.astype(float)
    ds = xr.Dataset(
        {
            "lst_daily": fused_daily.astype(np.float32),
            "diurnal_mean": diurnal_mean.astype(np.float32),
            "diurnal_amp": diurnal_amp.astype(np.float32),
        },
        coords={"time": _daily_ns, "y": landsat_da.y, "x": landsat_da.x},
        attrs={
            "units": "°C",
            "crs": "EPSG:4326",
            "resolution_m": target_resolution_m,
            "fusion_method": (
                "MODIS-anomaly + Landsat temporal interpolation + cosine diurnal"
            ),
            "source": "fused",
            "generated_at": pd.Timestamp.now().isoformat(),
            "bbox": (
                float(_x_vals.min()),
                float(_y_vals.min()),
                float(_x_vals.max()),
                float(_y_vals.max()),
            ),
            "diurnal_model": (
                "LST(day,hour) = diurnal_mean + diurnal_amp*cos(2*pi*(hour-14)/24)"
                " + (lst_daily - diurnal_mean - diurnal_amp*_COS_REF)"
            ),
            "t_peak": _T_PEAK,
            "t_ref": _T_REF,
            "cos_ref": _COS_REF,
            "qc_mask_note": (
                "qc_mask pending real GEE QC; all pixels assumed valid until P1.5."
            ),
        },
    )
    return ds


def reconstruct_lst_at_hour(
    ds_daily: xr.Dataset,
    hour: float,
    day_idx: int | np.ndarray | None = None,
) -> xr.DataArray:
    """Reconstruct LST at a specific hour of day from the daily fused dataset.

    Uses the cosine diurnal model anchored to the fused daily value:

        LST(hour) = diurnal_mean + diurnal_amp * cos(2π(h−14)/24)
                    + (lst_daily − diurnal_mean − diurnal_amp * _COS_REF)

    Parameters
    ----------
    ds_daily : xr.Dataset
        Output of :func:`fuse_to_daily`.
    hour : float
        Hour of day (0–24).
    day_idx : int | array, optional
        If given, select only these days (0-based).  Default: all days.

    Returns
    -------
    xr.DataArray  (time, y, x) or (y, x) if day_idx is a scalar.
    """
    cos_h = float(np.cos(2 * np.pi * (hour - _T_PEAK) / 24))
    dm = ds_daily["diurnal_mean"]
    da_amp = ds_daily["diurnal_amp"]
    lst_d = ds_daily["lst_daily"]
    diurnal_ref = dm + da_amp * _COS_REF
    correction = lst_d - diurnal_ref
    result = dm + da_amp * cos_h + correction
    if day_idx is not None:
        result = result.isel(time=day_idx)
    result.attrs.update(ds_daily.attrs)
    return result


def clear_sky_rmse_daily(
    ds_daily: xr.Dataset,
    reference_landsat_da: xr.DataArray,
) -> float:
    """RMSE between fused daily LST and Landsat on overpass days.

    Parameters
    ----------
    ds_daily : xr.Dataset
        Output of :func:`fuse_to_daily`.
    reference_landsat_da : xr.DataArray  (time, y, x)
        Held-out Landsat LST at overpass times.

    Returns
    -------
    float
        RMSE in °C.
    """
    fused_at = ds_daily["lst_daily"].sel(
        time=reference_landsat_da.time, method="nearest"
    )
    sq_err = (fused_at - reference_landsat_da) ** 2
    return float(np.sqrt(sq_err.mean().values))


def clear_sky_rmse(
    fused_da: xr.DataArray,
    reference_landsat_da: xr.DataArray,
) -> float:
    """Root-mean-square error between fused and reference Landsat on overpass days.

    Extracts fused values at Landsat timestamps (nearest neighbour) and computes
    RMSE over all clear-sky pixels where both are defined.

    Parameters
    ----------
    fused_da : xr.DataArray  (time, y, x)
        Fused hourly LST from :func:`fuse_to_hourly`.
    reference_landsat_da : xr.DataArray  (time, y, x)
        Held-out Landsat LST at overpass times.

    Returns
    -------
    float
        RMSE in °C.
    """
    fused_at_landsat = fused_da.sel(time=reference_landsat_da.time, method="nearest")
    sq_err = (fused_at_landsat - reference_landsat_da) ** 2
    rmse = float(np.sqrt(sq_err.mean().values))
    return rmse


def build_qc_mask(fused_da: xr.DataArray) -> xr.DataArray:
    """Placeholder QC mask — all pixels assumed valid until P1.5.

    Real quality flags come from the GEE pull (cloud/shadow/edge masks per sensor).
    P1.5 will implement this with actual per-pixel QC from MODIS QC_Day/Landsat
    QA_PIXEL bands stored alongside the LST rasters.

    Parameters
    ----------
    fused_da : xr.DataArray  (time, y, x)
        Fused hourly LST from :func:`fuse_to_hourly`, used only for shape/coords.

    Returns
    -------
    xr.DataArray of bool
        All-True mask, same shape as *fused_da*.
    """
    return xr.ones_like(fused_da, dtype=bool)
