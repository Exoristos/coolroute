"""Simulated e-scooter / e-bike trip generator.

Implements R4 fallback (no İBB e-scooter data, pending Martı).  Provides a
`load_trips` dispatcher and a deterministic `simulate_trips` engine backed by
OSM POI sampling.

**Simulator guarantees (R4 / D3):**

* **Spatial plausibility:** origins and destinations cluster near OSM
  amenities/POIs (restaurants, shops, transit stops, etc.), fetched via
  :func:`osmnx.features_from_polygon`.
* **Distance distribution:** target median 2–5 km, 90th percentile < 10 km.
  Achieved via log-normal distance sampling + rejection against POI positions.
* **Volume calibration:** ``n_trips`` configurable.
* **Deterministic:** set `seed` (default from ``settings.random_seed``) —
  same seed → identical trips.

.. note::

    OSMnx calls are isolated behind ``_fetch_pois(bbox)`` /
    ``_fetch_network(bbox)`` helpers so tests can monkeypatch them.
    No network requests during unit tests.

Future providers:
    * ``source="ibb"`` — İBB Açık Veri (no e-scooter data, per R4).
    * ``source="operator"`` — Martı telemetry (pending).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from geopandas import GeoSeries
from shapely.geometry import LineString, Point

logger = logging.getLogger(__name__)

# ── Target distance distribution parameters (log-normal) ──────────────────
# Median ≈ 3.5 km, p90 ≈ 8.0 km.  Fits pilot corridor (~5–10 km²).
_DIST_MU: float = float(np.log(3500.0))  # ≈ 8.16 — log-scale mean
_DIST_SIGMA: float = 0.65  # log-scale std; exp(σ × 1.28155) ≈ 2.30 → p90 ≈ 8 km
_DIST_MAX_KM: float = 10.0  # hard ceiling; reject trips above this
_SCOOTER_SPEED_KPH: float = 15.0  # typical e-scooter cruising speed

# ── Sampler tuning ─────────────────────────────────────────────────────────
_MAX_ATTEMPTS_PER_TRIP: int = 200  # max rejection-sampling iterations per trip
_MIN_DISTANCE_M: float = 500.0  # minimum O/D distance for fallback (metres)
_SKIPPED_WARN_THRESHOLD: float = 0.10  # warn if >10% of requested trips skipped


# ── OSMnx isolation helpers (monkeypatch targets for tests) ───────────────


def _fetch_pois(bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Fetch OSM amenities/POIs within *bbox* via osmnx 2.x API.

    Returns a GeoDataFrame of POI points (centroids).  Tags cover typical
    e-scooter trip attractors: restaurants, cafes, shops, transit, education,
    healthcare, etc.
    """
    import osmnx as ox
    from shapely.geometry import box as shapely_box

    polygon = shapely_box(bbox[0], bbox[1], bbox[2], bbox[3])
    tags: dict[str, bool | str] = {
        "amenity": True,  # all amenities
        "shop": True,  # shops
        "leisure": True,  # parks, sports
        "tourism": True,  # attractions
    }
    try:
        gdf = ox.features_from_polygon(polygon, tags=tags)
    except Exception:
        # Fallback: try geometries_from_polygon (osmnx < 1.5 compat)
        gdf = ox.geometries_from_polygon(polygon, tags=tags)

    if gdf.empty:
        return gdf

    # Keep only Point geometries (drop polygons/linestrings), use centroids
    # for polygonal POIs so we have a dense set of candidate locations.
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf["geometry"] = gdf.geometry.apply(
        lambda g: g.centroid if g is not None and g.geom_type != "Point" else g
    )
    points = gdf[gdf.geometry.geom_type == "Point"]
    return points[["geometry"]]


def _fetch_network(bbox: tuple[float, float, float, float]) -> object:
    """Fetch OSM drivable network graph within *bbox*.

    Reserved for future routing (P5).  Returns a ``networkx.MultiDiGraph``.
    """
    import osmnx as ox

    return ox.graph_from_polygon(
        (bbox[0], bbox[1], bbox[2], bbox[3]), network_type="drive"
    )


# ── Internal trip builder ─────────────────────────────────────────────────


def _sample_trips(
    pois: gpd.GeoDataFrame,
    n_trips: int,
    start: str,
    end: str,
    seed: int,
) -> gpd.GeoDataFrame:
    """Build a deterministic set of simulated trips from POI locations.

    Parameters
    ----------
    pois : gpd.GeoDataFrame
        Candidate origin/destination points (must have Point geometry).
    n_trips : int
        Number of trips to generate.
    start : str
        ISO-format start datetime (e.g. ``"2024-06-01"``).
    end : str
        ISO-format end datetime.
    seed : int
        Random seed for reproducibility.
    """
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp(start)
    t1 = pd.Timestamp(end)
    total_seconds = (t1 - t0).total_seconds()

    poi_indices = np.arange(len(pois))
    # Pre-extract coordinates (faster than repeated .iloc)
    poi_x = pois.geometry.x.values
    poi_y = pois.geometry.y.values

    # ── Build records with separate geometry arrays ────────────────────
    records: list[dict[str, Any]] = []

    for trip_i in range(n_trips):
        # ── Sample a target distance from log-normal distribution ─────
        target_m: float = float(rng.lognormal(_DIST_MU, _DIST_SIGMA))
        if target_m > _DIST_MAX_KM * 1000:
            target_m = _DIST_MAX_KM * 1000

        # ── Sample origin uniformly from POIs ────────────────────────
        o_idx = int(rng.choice(poi_indices))
        ox_val, oy_val = poi_x[o_idx], poi_y[o_idx]

        # ── Find a destination POI at ~target distance ───────────────
        d_idx: int | None = None

        # Compute distances from origin to ALL POIs (vectorised)
        dx_all = poi_x - ox_val
        dy_all = poi_y - oy_val
        # Approximate metres: 1° lat ≈ 111320 m, 1° lon ≈ 111320·cos(lat)
        cos_lat = np.cos(np.radians(oy_val))
        dists_all = np.sqrt((dy_all * 111320.0) ** 2 + (dx_all * 111320.0 * cos_lat) ** 2)

        # First pass: candidates within ±30% of target
        rel_tol = 0.30
        for _attempt in range(_MAX_ATTEMPTS_PER_TRIP):
            mask = np.abs(dists_all - target_m) <= target_m * rel_tol
            # Exclude origin itself (distance ≈ 0)
            mask[o_idx] = False
            candidates = np.where(mask)[0]
            if len(candidates) > 0:
                d_idx = int(rng.choice(candidates))
                break
            rel_tol *= 1.3  # widen tolerance each attempt

        if d_idx is None:
            # Fallback: closest POI that is not the origin and > min distance
            sorted_idx = np.argsort(dists_all)
            for si in sorted_idx:
                if si != o_idx and dists_all[si] >= _MIN_DISTANCE_M:
                    d_idx = int(si)
                    break
        if d_idx is None or d_idx == o_idx:
            # Still no valid destination — skip this trip
            continue

        dist_m = float(dists_all[d_idx])

        origin = Point(ox_val, oy_val)
        destination = Point(poi_x[d_idx], poi_y[d_idx])
        path = LineString([origin, destination])

        # Random datetime within window
        offset_s = rng.uniform(0, total_seconds)
        dt = t0 + timedelta(seconds=float(offset_s))

        # Estimated speed (km/h) with some noise
        est_speed = float(rng.normal(_SCOOTER_SPEED_KPH, 2.0))
        est_speed = max(5.0, min(est_speed, 30.0))

        records.append(
            {
                "trip_id": trip_i,
                "origin": origin,
                "destination": destination,
                "path": path,
                "datetime": dt.to_pydatetime(),
                "distance_m": dist_m,
                "est_speed": est_speed,
            }
        )

    # ── Warn if too many trips skipped ────────────────────────────────
    skipped = n_trips - len(records)
    if skipped > 0:
        skip_pct = skipped / n_trips
        if skip_pct > _SKIPPED_WARN_THRESHOLD:
            logger.warning(
                "Trip sampler skipped %.1f%% of requested trips (%d/%d). "
                "POI density may be too low for the bbox+trip volume.",
                skip_pct * 100,
                skipped,
                n_trips,
            )

    # ── Assemble GeoDataFrame with geometry columns ───────────────────
    gdf = gpd.GeoDataFrame(records, geometry="origin", crs="EPSG:4326")
    # Ensure destination and path are proper geometry columns (not object)
    gdf["destination"] = GeoSeries(gdf["destination"].values, crs="EPSG:4326")
    gdf["path"] = GeoSeries(gdf["path"].values, crs="EPSG:4326")
    return gdf


# ── Public API ────────────────────────────────────────────────────────────


def simulate_trips(
    bbox: tuple[float, float, float, float],
    n_trips: int,
    start: str,
    end: str,
    seed: int = 42,
) -> gpd.GeoDataFrame:
    """Generate a deterministic set of simulated e-scooter trips.

    Parameters
    ----------
    bbox : tuple[float, float, float, float]
        (west, south, east, north) — pilot corridor bounding box.
    n_trips : int
        Number of trips to produce (volume calibration).
    start : str
        ISO-format start date (e.g. ``"2024-06-01"``).
    end : str
        ISO-format end date (e.g. ``"2024-10-31"``).
    seed : int
        Random seed for strict determinism.  Default: 42.

    Returns
    -------
    gpd.GeoDataFrame
        Trip schema: ``trip_id, origin(Point), destination(Point),
        path(LineString), datetime, distance_m, est_speed``.  CRS EPSG:4326.
    """
    pois = _fetch_pois(bbox)
    if pois.empty:
        raise RuntimeError(
            f"No OSM POI points found in bbox {bbox}. "
            "Try a larger bounding box or check osmnx connectivity."
        )
    return _sample_trips(pois, n_trips, start, end, seed)


def load_trips(
    source: str = "simulation",
    **kw: Any,
) -> gpd.GeoDataFrame:
    """Load trip data from the specified *source*.

    ``"simulation"`` delegates to :func:`simulate_trips` — passes through all
    keyword arguments.  ``"ibb"`` and ``"operator"`` raise
    :class:`NotImplementedError` (purposefully, for future extension).

    Parameters
    ----------
    source : str
        Data provider: ``"simulation"``, ``"ibb"``, or ``"operator"``.
    **kw
        Forwarded to the underlying provider (e.g. ``bbox, n_trips, start,
        end, seed`` for simulation).

    Returns
    -------
    gpd.GeoDataFrame
        Trip GeoDataFrame (same schema regardless of provider).
    """
    if source == "simulation":
        return simulate_trips(**kw)
    if source == "ibb":
        raise NotImplementedError(
            "ibb: no e-scooter trip data available on İBB Açık Veri Portalı. "
            "Only 'Bisiklet ve Mikromobilite Park Alanları' (static points) exist. "
            "(Confirmed R4 research.)"
        )
    if source == "operator":
        raise NotImplementedError(
            "operator: pending Martı telemetry integration. "
            "Real operator data not yet available."
        )
    raise ValueError(
        f"Unknown trip source '{source}'. Use 'simulation', 'ibb', or 'operator'."
    )
