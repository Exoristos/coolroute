"""Network loading + DEM pull for OSMnx-based routing.

All OSMnx and GEE calls are isolated behind helper functions so tests can
monkeypatch them.  No network requests during unit tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from shapely.geometry import box as shapely_box

logger = logging.getLogger(__name__)


def pull_dem(
    bbox: tuple[float, float, float, float],
    out_path: str | Path = "data/raw/dem/srtm.tif",
) -> Path:
    """Pull SRTM 30m DEM from Google Earth Engine for *bbox* → local GeoTIFF.

    Uses the same synchronous ``getDownloadURL`` pattern as the LST pull script.
    Cached — skips download if *out_path* already exists.

    Parameters
    ----------
    bbox : tuple[float, float, float, float]
        (west, south, east, north).
    out_path : str | Path
        Output GeoTIFF path.

    Returns
    -------
    Path
        Path to the downloaded GeoTIFF.
    """
    import time

    import ee
    import requests

    out_path = Path(out_path)
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("DEM already cached at %s", out_path)
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)

    region = ee.Geometry.Rectangle([bbox[0], bbox[1], bbox[2], bbox[3]])
    dem = ee.Image("USGS/SRTMGL1_003").select("elevation").clip(region)

    for attempt in range(3):
        try:
            url = dem.getDownloadURL(
                {"scale": 30, "crs": "EPSG:4326", "region": region, "format": "GEO_TIFF"}
            )
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            if len(r.content) < 100:
                raise RuntimeError(f"tiny response ({len(r.content)} B)")
            out_path.write_bytes(r.content)
            logger.info("DEM downloaded to %s", out_path)
            return out_path
        except Exception as exc:
            logger.warning("DEM pull attempt %d failed: %s", attempt + 1, exc)
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"Failed to download DEM after 3 attempts to {out_path}")


def load_network(
    bbox: tuple[float, float, float, float],
    dem_path: str | Path = "data/raw/dem/srtm.tif",
) -> object:
    """Load OSMnx drive network with elevations and road grades.

    1. Downloads drive network from Overpass API.
    2. Attaches elevation from SRTM DEM raster to each node.
    3. Computes edge grades (rise/run) from node elevations.

    Parameters
    ----------
    bbox : tuple[float, float, float, float]
        (west, south, east, north).
    dem_path : str | Path
        Path to SRTM GeoTIFF (see :func:`pull_dem`).

    Returns
    -------
    networkx.MultiDiGraph
        OSMnx graph with node elevations and edge ``grade`` attributes.
    """
    import osmnx as ox

    ox.settings.use_cache = True
    polygon = shapely_box(bbox[0], bbox[1], bbox[2], bbox[3])

    logger.info("Downloading drive network for bbox %s ...", bbox)
    G = ox.graph_from_polygon(polygon, network_type="drive")

    dem_path = Path(dem_path)
    if dem_path.exists():
        logger.info("Attaching elevations from %s ...", dem_path)
        G = ox.elevation.add_node_elevations_raster(G, str(dem_path), band=1)
        G = ox.elevation.add_edge_grades(G)
    else:
        logger.warning(
            "DEM file %s not found — skipping elevation/grade assignment. "
            "Edges will have no grade data.",
            dem_path,
        )

    return G
