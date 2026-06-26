"""Real-data LST pull (synchronous) for the pilot window.

Replaces the async Drive-export path with synchronous getDownloadURL downloads.
The pilot AOI is tiny (~10 km^2), so per-scene GeoTIFFs are small and this is
reliable + single-pass (no Drive, no manual steps). Reuses the tested
fuse_to_hourly / clear_sky_rmse from uhi_battery.data.fusion.

Window comes from .env (DATA_START_DATE / DATA_END_DATE). Cached GeoTIFFs land
in data/raw/{landsat,modis}; fused hourly output in data/processed/lst_hourly.zarr.
"""
from __future__ import annotations

import datetime
import time
from pathlib import Path

import numpy as np
import requests
import rioxarray  # noqa: F401
import xarray as xr

import ee

from uhi_battery.config import settings
from uhi_battery.data.fusion import clear_sky_rmse, fuse_to_hourly
from uhi_battery.data.gee_lst import init_ee

LANDSAT_DIR = Path("data/raw/landsat")
MODIS_DIR = Path("data/raw/modis")
OUT_ZARR = Path("data/processed/lst_hourly.zarr")


def _dl(image: ee.Image, fname: str, out_dir: Path, scale: int, region: ee.Geometry) -> Path | None:
    """Synchronously download a single-band ee.Image as a GeoTIFF (cached)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / fname
    if path.exists() and path.stat().st_size > 0:
        return path
    for attempt in range(3):
        try:
            url = image.getDownloadURL(
                {"scale": scale, "crs": "EPSG:4326", "region": region, "format": "GEO_TIFF"}
            )
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            if len(r.content) < 100:  # too small → likely error payload
                raise RuntimeError(f"tiny response ({len(r.content)} B)")
            path.write_bytes(r.content)
            return path
        except Exception as exc:  # noqa: BLE001
            print(f"    [retry {attempt+1}] {fname}: {exc}")
            time.sleep(2 * (attempt + 1))
    print(f"    [SKIP] {fname} after 3 attempts")
    return None


def _stack(paths: list[Path]) -> xr.DataArray:
    das = []
    for p in sorted(paths):
        try:
            da = xr.open_dataarray(p, engine="rasterio").squeeze("band", drop=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    [bad tiff] {p.name}: {exc}")
            continue
        date_str = p.stem.split("_")[-1]
        iso_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        da = da.expand_dims(time=[np.datetime64(iso_date)])
        das.append(da)
    if not das:
        raise RuntimeError("No usable GeoTIFFs to stack.")
    return xr.concat(das, dim="time").sortby("time")


def pull_landsat(region: ee.Geometry, start: str, end: str) -> xr.DataArray:
    print(f"  Landsat 8/9 clear-sky LST ({start}..{end})…")
    col = (
        ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
        .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
        .filterBounds(region)
        .filterDate(start, end)
        # cloud mask via QA_PIXEL bits 3 (cloud) & 4 (shadow)
        .map(
            lambda img: img.updateMask(
                img.select("QA_PIXEL").bitwiseAnd(1 << 3).eq(0).And(
                    img.select("QA_PIXEL").bitwiseAnd(1 << 4).eq(0)
                )
            )
        )
        # ST_B10 → °C (C2 L2 scale 0.00341802, offset 149.0 K); preserve timestamp
        .map(
            lambda img: img.select("ST_B10")
            .multiply(0.00341802)
            .add(149.0)
            .subtract(273.15)
            .rename("LST_C")
            .copyProperties(img, ["system:time_start"])
        )
    )
    n = int(col.size().getInfo())
    print(f"    found {n} scenes")
    timestamps = col.aggregate_array("system:time_start").getInfo()
    img_list = col.toList(n)
    paths = []
    for i in range(n):
        ts = timestamps[i]
        if ts is None:
            print(f"    [skip] scene {i}: no timestamp")
            continue
        d = datetime.datetime.fromtimestamp(ts / 1000, tz=datetime.timezone.utc).strftime("%Y%m%d")
        img = ee.Image(img_list.get(i)).select("LST_C")
        p = _dl(img, f"landsat_{d}.tif", LANDSAT_DIR, 30, region)
        if p:
            paths.append(p)
            print(f"    ✓ landsat_{d}")
    print(f"    downloaded {len(paths)}/{n}")
    return _stack(paths).rename("landsat_lst")


def pull_modis(region: ee.Geometry, start: str, end: str) -> tuple[xr.DataArray, xr.DataArray]:
    print(f"  MODIS MOD11A1 day+night ({start}..{end})…")
    base = (
        ee.ImageCollection("MODIS/061/MOD11A1")
        .filterBounds(region)
        .filterDate(start, end)
        # QC_Day/Night bits 0-1 == 0 → good quality
        .map(lambda img: img.updateMask(img.select("QC_Day").bitwiseAnd(3).eq(0)))
    )
    paths_d, paths_n = [], []
    n = int(base.size().getInfo())
    print(f"    found {n} days")
    timestamps = base.aggregate_array("system:time_start").getInfo()
    img_list = base.toList(n)
    for i in range(n):
        ts = timestamps[i]
        if ts is None:
            continue
        d = datetime.datetime.fromtimestamp(ts / 1000, tz=datetime.timezone.utc).strftime("%Y%m%d")
        img = ee.Image(img_list.get(i))
        # LST_Day/Night_1km: DN × 0.02 = K → °C
        day = img.select("LST_Day_1km").multiply(0.02).subtract(273.15).rename("LST_C")
        night = img.select("LST_Night_1km").multiply(0.02).subtract(273.15).rename("LST_C")
        pd = _dl(day, f"mod11a1_day_{d}.tif", MODIS_DIR, 1000, region)
        pn = _dl(night, f"mod11a1_night_{d}.tif", MODIS_DIR, 1000, region)
        if pd:
            paths_d.append(pd)
        if pn:
            paths_n.append(pn)
        print(f"    ✓ mod11a1 {d} (day={'y' if pd else 'n'} night={'y' if pn else 'n'})")
    print(f"    downloaded day={len(paths_d)}/{n} night={len(paths_n)}/{n}")
    return _stack(paths_d).rename("modis_day"), _stack(paths_n).rename("modis_night")


def main() -> int:
    bbox = settings.pilot_bbox
    start, end = settings.data_start_date, settings.data_end_date
    print("=== UHI-Battery — REAL sync LST pull + fusion ===")
    print(f"  bbox={bbox}  window={start}..{end}  res={settings.target_resolution_m}m")
    init_ee(settings)
    region = ee.Geometry.Rectangle([bbox[0], bbox[1], bbox[2], bbox[3]], "EPSG:4326", False)

    landsat_da = pull_landsat(region, start, end)
    modis_day, modis_night = pull_modis(region, start, end)

    print("\nFusing → hourly 30 m …")
    fused = fuse_to_hourly(
        landsat_da=landsat_da,
        modis_da=modis_day,
        modis_night_da=modis_night,
        target_resolution_m=settings.target_resolution_m,
    )
    print(f"  fused sizes={dict(fused.sizes)}")
    print(f"  time span: {str(fused.time.values[0])[:16]} … {str(fused.time.values[-1])[:16]}")
    print(f"  LST range: {float(fused.min()):.1f} … {float(fused.max()):.1f} °C")

    OUT_ZARR.parent.mkdir(parents=True, exist_ok=True)
    fused.to_zarr(str(OUT_ZARR), mode="w")
    print(f"  ✓ wrote {OUT_ZARR}")

    rmse = clear_sky_rmse(fused, landsat_da)
    print(f"\nClear-sky RMSE = {rmse:.3f} °C  ({'PASS' if rmse < 1.5 else 'over 1.5 target'})")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
