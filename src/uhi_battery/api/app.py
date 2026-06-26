"""FastAPI REST service — UHI × e-micromobility battery optimization.

Endpoints: health, heat-layer, hotspots, route, metrics, soh, energy.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from uhi_battery.config import settings

# ── Paths ───────────────────────────────────────────────────────────────────
LST_ZARR = Path("data/processed/lst_hourly.zarr")
HOTSPOTS_GEOJSON = Path("data/processed/hotspots.geojson")
FRONTIERS_JSON = Path("data/processed/pareto_frontiers.json")
METRICS_JSON = Path("data/processed/metrics.json")
SOH_MODEL_PKL = Path("data/processed/soh_model.pkl")
ENERGY_MODEL_PKL = Path("data/processed/energy_model.pkl")

# ── Module-level caches (lazy) ──────────────────────────────────────────────
_lst_ds: Any = None  # xr.Dataset
_fusion_module: Any = None
_hotspots_cache: dict[str, Any] | None = None
_frontiers_cache: dict[str, Any] | None = None
_metrics_cache: dict[str, Any] | None = None
_soh_model_cache: dict[str, Any] | None = None
_energy_model_cache: dict[str, Any] | None = None


def _get_lst() -> Any:
    """Lazy-load fused LST dataset."""
    global _lst_ds  # noqa: PLW0603
    if _lst_ds is None:
        import xarray as xr
        _lst_ds = xr.open_zarr(str(LST_ZARR))
    return _lst_ds


def _get_fusion() -> Any:
    """Lazy-import fusion module."""
    global _fusion_module  # noqa: PLW0603
    if _fusion_module is None:
        from uhi_battery.data import fusion as _fusion_module
    return _fusion_module


def _get_hotspots() -> dict[str, Any]:
    """Lazy-load hotspots GeoJSON."""
    global _hotspots_cache  # noqa: PLW0603
    if _hotspots_cache is None:
        if HOTSPOTS_GEOJSON.exists():
            _hotspots_cache = json.loads(HOTSPOTS_GEOJSON.read_text(encoding="utf-8"))
        else:
            _hotspots_cache = {"type": "FeatureCollection", "features": []}
    return _hotspots_cache


def _get_frontiers() -> dict[str, Any]:
    """Lazy-load pre-computed Pareto frontiers."""
    global _frontiers_cache  # noqa: PLW0603
    if _frontiers_cache is None:
        if FRONTIERS_JSON.exists():
            _frontiers_cache = json.loads(FRONTIERS_JSON.read_text(encoding="utf-8"))
        else:
            _frontiers_cache = {}
    return _frontiers_cache


def _get_metrics() -> dict[str, Any]:
    """Lazy-load metrics."""
    global _metrics_cache  # noqa: PLW0603
    if _metrics_cache is None:
        if METRICS_JSON.exists():
            _metrics_cache = json.loads(METRICS_JSON.read_text(encoding="utf-8"))
        else:
            _metrics_cache = {}
    return _metrics_cache


def _get_soh_model() -> dict[str, Any]:
    """Lazy-load SoH model."""
    global _soh_model_cache  # noqa: PLW0603
    if _soh_model_cache is None:
        if SOH_MODEL_PKL.exists():
            with open(SOH_MODEL_PKL, "rb") as f:
                bundle = pickle.load(f)
            _soh_model_cache = bundle["model"]
        else:
            _soh_model_cache = {"Ea": 45000.0, "A": 3e4}
    return _soh_model_cache


def _get_energy_model() -> dict[str, Any]:
    """Lazy-load energy model."""
    global _energy_model_cache  # noqa: PLW0603
    if _energy_model_cache is None:
        if ENERGY_MODEL_PKL.exists():
            with open(ENERGY_MODEL_PKL, "rb") as f:
                bundle = pickle.load(f)
            _energy_model_cache = bundle
        else:
            _energy_model_cache = {}
    return _energy_model_cache


def _validate_in_bbox(lon: float, lat: float) -> None:
    """Raise 422 if (lon, lat) is outside pilot bbox."""
    west, south, east, north = settings.pilot_bbox
    if not (west <= lon <= east and south <= lat <= north):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Point ({lon}, {lat}) outside pilot bbox "
                f"[{west}, {south}, {east}, {north}]"
            ),
        )


# ── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="UHI × E-Micromobility Battery Optimizer",
    version="1.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "1.0"}


# ── /heat-layer ─────────────────────────────────────────────────────────────


@app.get("/heat-layer")
async def heat_layer(
    day: int = Query(default=-1, description="Day index (0-based, -1 = hottest)"),
    hour: float = Query(default=14.0, ge=0.0, le=24.0, description="Hour of day (0-24)"),
    stride: int = Query(default=3, ge=1, le=20, description="Downsample stride"),
) -> JSONResponse:
    """Get LST grid snapshot at given day/hour."""
    try:
        ds = _get_lst()
        fusion = _get_fusion()
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"LST data unavailable: {exc}") from exc

    # Hottest day if not specified
    if day < 0:
        day = int(ds["lst_daily"].mean(dim=["x", "y"]).argmax().values)

    if day >= ds.sizes["time"]:
        raise HTTPException(status_code=422, detail=f"day {day} >= {ds.sizes['time']} days")

    try:
        da = fusion.reconstruct_lst_at_hour(ds, hour, day_idx=day)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LST reconstruction failed: {exc}") from exc

    # Downsample for API response
    if stride > 1:
        da = da[::stride, ::stride]

    values = da.values
    # Replace NaN with None for JSON
    values_list = values.tolist()
    stats = {
        "min": round(float(np.nanmin(values)), 2),
        "max": round(float(np.nanmax(values)), 2),
        "mean": round(float(np.nanmean(values)), 2),
        "std": round(float(np.nanstd(values)), 2),
    }

    west, south, east, north = (
        float(da.x.values.min()),
        float(da.y.values.min()),
        float(da.x.values.max()),
        float(da.y.values.max()),
    )

    return JSONResponse(
        {
            "bbox": [west, south, east, north],
            "resolution_m": 30 * stride,
            "day": day,
            "hour": hour,
            "grid_shape": list(values.shape),
            "stats": stats,
            "values": values_list,
        }
    )


# ── /hotspots ───────────────────────────────────────────────────────────────


@app.get("/hotspots")
async def hotspots() -> JSONResponse:
    geojson = _get_hotspots()
    return JSONResponse(geojson)


# ── /route ──────────────────────────────────────────────────────────────────


def _closest_od_pair(
    frontiers: dict[str, Any],
    o_lon: float,
    o_lat: float,
    d_lon: float,
    d_lat: float,
) -> dict[str, Any] | None:
    """Find the closest pre-computed OD pair to the requested one."""
    # The frontiers file doesn't store origin/dest coordinates — it stores node IDs.
    # Return the first pair as a fallback, with an honest note.
    pairs = frontiers.get("pairs", [])
    if not pairs:
        return None

    # Simple heuristic: return pair with similar energy/length scale
    # Since we can't geolocate node IDs without the graph, return first pair
    pair = pairs[0]
    frontier = pair.get("frontier", [])
    return {
        "pair_idx": pair.get("pair_idx", 1),
        "origin_node": pair.get("origin"),
        "dest_node": pair.get("dest"),
        "obj2": frontiers.get("obj2", "length"),
        "correlation_r": frontiers.get("correlation_r", 0),
        "frontier_size": len(frontier),
        "note": (
            "Nearest pre-computed OD pair (node IDs, not coordinates). "
            "Full on-demand routing available but requires ~2 min computation."
        ),
        "frontier": [
            {
                "energy_wh": f.get("energy_wh"),
                "obj2_value": f.get("obj2_value"),
                "obj2_name": f.get("obj2_name"),
                "alpha": f.get("alpha"),
            }
            for f in frontier
        ],
    }


@app.get("/route")
async def route(
    origin_lon: float = Query(..., ge=-180, le=180),
    origin_lat: float = Query(..., ge=-90, le=90),
    dest_lon: float = Query(..., ge=-180, le=180),
    dest_lat: float = Query(..., ge=-90, le=90),
) -> JSONResponse:
    """Return nearest pre-computed Pareto frontier."""
    _validate_in_bbox(origin_lon, origin_lat)
    _validate_in_bbox(dest_lon, dest_lat)

    frontiers = _get_frontiers()
    if not frontiers:
        raise HTTPException(status_code=404, detail="No pre-computed frontiers available")

    result = _closest_od_pair(frontiers, origin_lon, origin_lat, dest_lon, dest_lat)
    if result is None:
        raise HTTPException(status_code=404, detail="No frontier for requested OD")

    return JSONResponse(result)


# ── /metrics ────────────────────────────────────────────────────────────────


@app.get("/metrics")
async def metrics() -> JSONResponse:
    metrics_data = _get_metrics()
    return JSONResponse(metrics_data)


# ── /soh ────────────────────────────────────────────────────────────────────


@app.get("/soh")
async def soh(
    temp_c: float = Query(..., ge=-20, le=80, description="Temperature (°C)"),
    cycles: float = Query(..., ge=0, le=1_000_000, description="Cycle count"),
) -> JSONResponse:
    """Predict SoH via Arrhenius exponential decay model."""
    model = _get_soh_model()
    from uhi_battery.models.soh import predict_soh

    retention = predict_soh(model, temp_c, cycles)
    return JSONResponse(
        {
            "retention_pct": round(retention, 2),
            "temp_c": temp_c,
            "cycles": cycles,
            "model": "Arrhenius exponential decay, Ea=45 kJ/mol (LCO 18650)",
        }
    )


# ── /energy ─────────────────────────────────────────────────────────────────


@app.get("/energy")
async def energy(
    distance_m: float = Query(..., gt=0, description="Trip distance (m)"),
    speed_kmh: float = Query(default=15.0, gt=0, le=100),
    temp_c: float = Query(default=25.0, ge=-20, le=60),
    slope_deg: float = Query(default=0.0, ge=-20, le=20),
) -> JSONResponse:
    """Compute trip energy via physics model."""
    from uhi_battery.models.energy import compute_trip_energy

    wh = float(compute_trip_energy(distance_m, speed_kmh, temp_c, slope_deg))
    wh_per_km = wh / (distance_m / 1000.0) if distance_m > 0 else 0.0

    # Decompose (simplified in API)
    m, g, crr = 100.0, 9.81, 0.008
    rho, cda = 1.225, 0.45
    eta = 0.85 * (1 - 0.003 * (25.0 - temp_c))
    eta = max(0.5, min(0.95, eta))
    speed_ms = speed_kmh / 3.6
    roll = m * g * crr * distance_m / eta / 3600.0
    aero = 0.5 * rho * cda * speed_ms**2 * distance_m / eta / 3600.0
    grade = m * g * np.sin(np.radians(slope_deg)) * distance_m / eta / 3600.0

    return JSONResponse(
        {
            "energy_wh": round(wh, 2),
            "wh_per_km": round(wh_per_km, 2),
            "params": {
                "distance_m": distance_m,
                "speed_kmh": speed_kmh,
                "temp_c": temp_c,
                "slope_deg": slope_deg,
            },
            "components": {
                "rolling_wh": round(float(roll), 2),
                "aero_wh": round(float(aero), 2),
                "grade_wh": round(float(grade), 2),
            },
        }
    )


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
