"""Pull DEM + single-day Landsat LST for expanded bbox.

Fast alternative to full 177-day fusion: pulls one Landsat scene nearest to
the hottest day (2025-07-22) for the expanded pilot area.  Used for Pareto
routing where only one day/hour is needed.

Usage::

    uv run python scripts/pull_expanded_data.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

# ── Paths ───────────────────────────────────────────────────────────────────
DEM_PATH = Path("data/raw/dem/srtm.tif")
LST_EXPANDED = Path("data/processed/lst_expanded.nc")
TARGET_DATE = "2025-07-22"
DATE_WINDOW_DAYS = 7  # search ±7 days for cloud-free Landsat


def main() -> int:
    from uhi_battery.config import settings
    from uhi_battery.data.gee_lst import init_ee

    bbox = settings.pilot_bbox
    print("=" * 60)
    print("  Pull Expanded Data (DEM + single-day LST)")
    print("=" * 60)
    print(f"  Bbox: {bbox}")
    print(f"  Target date: {TARGET_DATE} (±{DATE_WINDOW_DAYS} days)")

    # ── 1. Init GEE ────────────────────────────────────────────────────
    print("\n── 1. GEE init ──")
    try:
        init_ee(settings)
        print("  GEE authenticated")
    except Exception as exc:
        print(f"  [ERROR] GEE init failed: {exc}")
        return 1

    import ee
    import requests

    # ── 2. DEM ──────────────────────────────────────────────────────────
    print("\n── 2. DEM (SRTM 30m) ──")
    DEM_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEM_PATH.exists() and DEM_PATH.stat().st_size > 0:
        print(f"  DEM cached at {DEM_PATH}")
    else:
        region = ee.Geometry.Rectangle([bbox[0], bbox[1], bbox[2], bbox[3]])
        dem = ee.Image("USGS/SRTMGL1_003").select("elevation").clip(region)
        for attempt in range(3):
            try:
                url = dem.getDownloadURL(
                    {"scale": 30, "crs": "EPSG:4326", "region": region, "format": "GEO_TIFF"}
                )
                r = requests.get(url, timeout=180)
                r.raise_for_status()
                DEM_PATH.write_bytes(r.content)
                print(f"  DEM saved: {DEM_PATH} ({len(r.content)} bytes)")
                break
            except Exception as exc:
                print(f"  Attempt {attempt+1} failed: {exc}")
                time.sleep(2 * (attempt + 1))
        else:
            print("  [ERROR] DEM pull failed after 3 attempts")
            return 1

    # ── 3. Single-day Landsat LST ───────────────────────────────────────
    print("\n── 3. Landsat LST (single day) ──")
    LST_EXPANDED.parent.mkdir(parents=True, exist_ok=True)

    region = ee.Geometry.Rectangle([bbox[0], bbox[1], bbox[2], bbox[3]])

    # Landsat 8 Collection 2 Level 2 — ST_B10 is surface temperature
    # Scale: 0.00341802, offset: 149.0 → Kelvin
    # Convert: C = K - 273.15
    target = ee.Date(TARGET_DATE)
    start = target.advance(-DATE_WINDOW_DAYS, "day")
    end = target.advance(DATE_WINDOW_DAYS, "day")

    # Try Landsat 8 first, then Landsat 9
    for collection_name, st_band in [
        ("LANDSAT/LC08/C02/T1_L2", "ST_B10"),
        ("LANDSAT/LC09/C02/T1_L2", "ST_B10"),
    ]:
        print(f"  Trying {collection_name}...")
        col = (
            ee.ImageCollection(collection_name)
            .filterDate(start, end)
            .filterBounds(region)
            .filter(ee.Filter.lt("CLOUD_COVER", 30))
        )

        try:
            n_images = int(col.size().getInfo())
            print(f"    Found {n_images} images with <30% cloud")
        except Exception:
            n_images = 0

        if n_images == 0:
            print("    No images found, trying next collection")
            continue

        # Get the best (least cloudy) image
        best = col.sort("CLOUD_COVER").first()

        # Surface temperature: scale and offset → Celsius
        # ST_B10: scale=0.00341802, offset=149.0 (Kelvin)
        lst_c = best.select(st_band).multiply(0.00341802).add(149.0).subtract(273.15)
        lst_c = lst_c.rename("lst").clip(region)

        # Download as GeoTIFF
        for attempt in range(3):
            try:
                url = lst_c.getDownloadURL(
                    {"scale": 30, "crs": "EPSG:4326", "region": region, "format": "GEO_TIFF"}
                )
                r = requests.get(url, timeout=300)
                r.raise_for_status()
                if len(r.content) < 1000:
                    raise RuntimeError(f"tiny response ({len(r.content)} B)")

                # Save raw GeoTIFF
                raw_tif = LST_EXPANDED.with_suffix(".tif")
                raw_tif.write_bytes(r.content)
                print(f"  LST GeoTIFF saved: {raw_tif} ({len(r.content)} bytes)")
                break
            except Exception as exc:
                print(f"  Attempt {attempt+1} failed: {exc}")
                time.sleep(3 * (attempt + 1))
        else:
            print(f"  [ERROR] LST pull failed for {collection_name}")
            continue

        # Convert GeoTIFF to netCDF with xarray
        print("  Converting to netCDF...")
        try:
            import rioxarray as rxr

            da = rxr.open_rasterio(str(raw_tif), masked=True)
            da = da.squeeze().drop_vars("band", errors="ignore")
            da.name = "lst_daily"

            # Rename coords to match fusion zarr format (x, y)
            if "x" not in da.dims:
                da = da.rename({"longitude": "x", "latitude": "y"})
            if "x" not in da.dims:
                da = da.rename({"x": "x", "y": "y"})

            # Add time dimension
            da = da.expand_dims("time").assign_coords(time=[np.datetime64(TARGET_DATE)])

            # Save as netCDF
            ds = da.to_dataset()
            ds.to_netcdf(str(LST_EXPANDED))
            print(f"  LST netCDF saved: {LST_EXPANDED}")
            print(f"  Shape: {dict(ds.sizes)}")
            lst_min = float(ds.lst_daily.min())
            lst_max = float(ds.lst_daily.max())
            print(f"  LST range: {lst_min:.1f} - {lst_max:.1f} C")
            print(f"  Mean LST: {float(ds.lst_daily.mean()):.1f} C")
            break

        except Exception as exc:
            print(f"  [ERROR] netCDF conversion failed: {exc}")
            import traceback
            traceback.print_exc()
            continue
    else:
        print("  [ERROR] No Landsat images found in any collection")
        print("  Falling back: will use 25°C default for all edges")
        return 0  # not fatal — routing will use 25°C default

    elapsed = time.perf_counter()
    print(f"\n  Done in {elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
