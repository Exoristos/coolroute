"""Cached data loaders for the dashboard.

All data is read from local files under ``data/processed/``. Nothing hits the
API. Each loader is wrapped with ``st.cache_data`` so repeated interactions
(sliders, tab switches) don't re-open the zarr or re-parse the GeoJSON.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import streamlit as st
import xarray as xr

# Resolve paths relative to the project root (two levels up from this file).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESSED_DIR = _PROJECT_ROOT / "data" / "processed"

LST_ZARR = PROCESSED_DIR / "lst_hourly.zarr"
HOTSPOTS_GEOJSON = PROCESSED_DIR / "hotspots.geojson"
PARETO_JSON = PROCESSED_DIR / "pareto_frontiers.json"
METRICS_JSON = PROCESSED_DIR / "metrics.json"
SOH_MODEL_PKL = PROCESSED_DIR / "soh_model.pkl"


@st.cache_data(show_spinner="Opening LST zarr…")
def load_lst_dataset() -> xr.Dataset | None:
    """Open the hourly-derivable LST zarr (daily + diurnal params)."""
    if not LST_ZARR.exists():
        return None
    try:
        return xr.open_zarr(str(LST_ZARR))
    except Exception as exc:  # noqa: BLE001 — surface to the UI
        st.error(f"Could not open LST zarr: {exc}")
        return None


@st.cache_data(show_spinner="Reading hotspots…")
def load_hotspots() -> dict[str, Any] | None:
    """Load the UHI hotspots GeoJSON."""
    if not HOTSPOTS_GEOJSON.exists():
        return None
    try:
        with open(HOTSPOTS_GEOJSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read hotspots GeoJSON: {exc}")
        return None


@st.cache_data
def load_pareto_frontiers() -> dict[str, Any] | None:
    """Load Pareto frontiers JSON."""
    if not PARETO_JSON.exists():
        return None
    try:
        with open(PARETO_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read Pareto frontiers: {exc}")
        return None


@st.cache_data
def load_metrics() -> dict[str, Any] | None:
    """Load the curated metrics summary."""
    if not METRICS_JSON.exists():
        return None
    try:
        with open(METRICS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read metrics: {exc}")
        return None


@st.cache_data
def load_soh_model() -> dict[str, Any] | None:
    """Load the fitted Arrhenius SoH model bundle (model + evaluation)."""
    if not SOH_MODEL_PKL.exists():
        return None
    try:
        with open(SOH_MODEL_PKL, "rb") as f:
            return pickle.load(f)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read SoH model: {exc}")
        return None


@st.cache_data(show_spinner="Reconstructing LST field…")
def reconstruct_lst_field(
    day_idx: int, hour: float, coarsen: int = 4
) -> tuple[np.ndarray, tuple[float, float, float, float], float, float] | None:
    """Reconstruct the LST field at (day, hour) and downsample for display.

    Returns
    -------
    (lst_2d, bbox, vmin, vmax) or None on failure.
    - lst_2d : (ny, nx) float32, downsampled.
    - bbox   : (lon_min, lat_min, lon_max, lat_max) for folium bounds.
    - vmin/vmax : robust min/max for color scaling.
    """
    from uhi_battery.data.fusion import reconstruct_lst_at_hour

    ds = load_lst_dataset()
    if ds is None:
        return None

    day_idx = int(np.clip(day_idx, 0, ds.sizes["time"] - 1))
    hour = float(np.clip(hour, 0.0, 24.0))

    da = reconstruct_lst_at_hour(ds, hour=hour, day_idx=day_idx)
    arr = da.values.astype(np.float32)

    # Block-mean downsample for a browser-friendly grid (~80x120).
    ny, nx = arr.shape
    cy = (ny // coarsen) * coarsen
    cx = (nx // coarsen) * coarsen
    if cy > 0 and cx > 0:
        arr = arr[:cy, :cx].reshape(cy // coarsen, coarsen, cx // coarsen, coarsen)
        arr = arr.mean(axis=(1, 3))

    # Robust color bounds (2nd/98th percentile), guarding NaN.
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return None
    vmin = float(np.nanpercentile(finite, 2))
    vmax = float(np.nanpercentile(finite, 98))
    if vmax - vmin < 1.0:
        vmax = vmin + 1.0

    # Bbox from dataset attrs (lon_min, lat_min, lon_max, lat_max).
    raw = ds.attrs.get("bbox")
    if raw is not None and len(raw) == 4:
        bbox = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    else:
        bbox = (float(da.x.min()), float(da.y.min()), float(da.x.max()), float(da.y.max()))

    return arr, bbox, vmin, vmax


@st.cache_data
def lst_timeseries() -> pd.DatetimeIndex:
    """Return the daily time index of the LST dataset."""
    ds = load_lst_dataset()
    if ds is None:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(pd.to_datetime(ds.time.values))


@st.cache_data(show_spinner="Computing daily temperature curve…")
def daily_temperature_curve(day_idx: int) -> pd.DataFrame:
    """Spatial-mean LST across hours of the day for a chosen day."""
    from uhi_battery.data.fusion import reconstruct_lst_at_hour

    ds = load_lst_dataset()
    if ds is None:
        return pd.DataFrame()

    hours = np.arange(0, 25, 1, dtype=float)
    means = []
    for h in hours:
        da = reconstruct_lst_at_hour(ds, hour=float(h), day_idx=int(day_idx))
        means.append(float(np.nanmean(da.values)))
    return pd.DataFrame({"hour": hours, "lst_c": means})
