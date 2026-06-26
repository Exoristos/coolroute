"""Fuse cached GeoTIFFs → hourly LST zarr (no GEE calls).

Loads already-downloaded Landsat + MODIS GeoTIFFs from data/raw/, runs the
tested fuse_to_hourly + clear_sky_rmse, writes data/processed/lst_hourly.zarr.
Use after scripts/pull_lst_real.py has cached all scenes.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rioxarray  # noqa: F401
import xarray as xr

from uhi_battery.config import settings
from uhi_battery.data.fusion import clear_sky_rmse_daily, fuse_to_daily

LANDSAT_DIR = Path("data/raw/landsat")
MODIS_DIR = Path("data/raw/modis")
OUT_ZARR = Path("data/processed/lst_hourly.zarr")


def _stack(pattern: str, src_dir: Path) -> xr.DataArray:
    paths = sorted(src_dir.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"No files matching {pattern} in {src_dir}")
    das = []
    for p in paths:
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
        raise RuntimeError(f"No usable GeoTIFFs in {src_dir}/{pattern}")
    return xr.concat(das, dim="time").sortby("time")


def main() -> int:
    print("=== UHI-Battery — fuse cached GeoTIFFs → hourly zarr ===")
    print(f"  res={settings.target_resolution_m}m")

    print("  loading Landsat …")
    landsat_da = _stack("landsat_*.tif", LANDSAT_DIR).rename("landsat_lst")
    print(f"    {landsat_da.sizes}")

    print("  loading MODIS day …")
    modis_day = _stack("mod11a1_day_*.tif", MODIS_DIR).rename("modis_day")
    print(f"    {modis_day.sizes}")

    print("  loading MODIS night …")
    modis_night = _stack("mod11a1_night_*.tif", MODIS_DIR).rename("modis_night")
    print(f"    {modis_night.sizes}")

    print("\nFusing → daily 30 m (+ diurnal params) …")
    ds = fuse_to_daily(
        landsat_da=landsat_da,
        modis_da=modis_day,
        modis_night_da=modis_night,
        target_resolution_m=settings.target_resolution_m,
    )
    print(f"  vars: {list(ds.data_vars)}")
    print(f"  sizes={dict(ds.sizes)}")
    print(f"  time span: {str(ds.time.values[0])[:16]} … {str(ds.time.values[-1])[:16]}")
    print(f"  lst_daily range: {float(ds['lst_daily'].min()):.1f} … {float(ds['lst_daily'].max()):.1f} °C")
    print(f"  diurnal_amp range: {float(ds['diurnal_amp'].min()):.1f} … {float(ds['diurnal_amp'].max()):.1f} °C")

    OUT_ZARR.parent.mkdir(parents=True, exist_ok=True)
    ds.to_zarr(str(OUT_ZARR), mode="w")
    print(f"  ✓ wrote {OUT_ZARR}")

    rmse = clear_sky_rmse_daily(ds, landsat_da)
    print(f"\nClear-sky RMSE = {rmse:.3f} °C  ({'PASS' if rmse < 1.5 else 'over 1.5 target'})")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
