"""P1(a) runner: pull Landsat + MODIS LST → fuse → write hourly zarr.

Usage::

    uv run python scripts/pull_lst.py

Requires GEE credentials in ``.env`` (see ``.env.example``).  Without them the
script prints instructions and exits gracefully.

Steps
-----
1. Load :class:`~uhi_battery.config.Settings` from ``.env``.
2. Authenticate Earth Engine via :func:`~uhi_battery.data.gee_lst.init_ee`.
3. Pull Landsat 8/9 LST (clear-sky) and MODIS daily LST day + night.
4. Load GeoTIFFs into :mod:`xarray` DataArrays.
5. Fuse to hourly 30 m via :func:`~uhi_battery.data.fusion.fuse_to_hourly`.
6. Write ``data/processed/lst_hourly.zarr``.
7. Compute and print clear-sky RMSE (AC-009).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rioxarray  # noqa: F401 — registers .rio accessor
import xarray as xr

# Guard import so the module is importable without GEE credentials
try:
    from uhi_battery.config import settings
    from uhi_battery.data.fusion import clear_sky_rmse, fuse_to_hourly
    from uhi_battery.data.gee_lst import init_ee, pull_landsat_lst, pull_modis_lst
except ImportError as exc:
    print(f"Import error: {exc}", file=sys.stderr)
    print("Run with: uv run python scripts/pull_lst.py", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_geotiffs(paths: list[Path], band: int = 1) -> xr.DataArray:
    """Stack a list of single-band GeoTIFFs into a time-indexed DataArray."""
    das: list[xr.DataArray] = []
    for p in sorted(paths):
        if not p.exists():
            print(f"  [WARN] missing file, skipping: {p}")
            continue
        da = xr.open_dataarray(p, engine="rasterio").squeeze("band", drop=True)
        # Extract date from filename (e.g. mod11a1_day_20240615.tif)
        date_str = p.stem.split("_")[-1]
        da = da.expand_dims(time=[np.datetime64(date_str)])
        das.append(da)
    if not das:
        raise FileNotFoundError(f"No GeoTIFFs found in {paths[0].parent if paths else '?'}")
    return xr.concat(das, dim="time").sortby("time")


def _has_gee_creds() -> bool:
    """Check whether GEE credentials are configured."""
    return bool(settings.gee_service_account and settings.gee_private_key_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the full LST pull + fusion pipeline."""
    bbox = settings.pilot_bbox
    start = settings.data_start_date
    end = settings.data_end_date
    resolution = settings.target_resolution_m

    print("=== UHI Battery — P1(a) LST Pull + Fusion ===")
    print(f"  Pilot bbox: {bbox}")
    print(f"  Window:     {start} → {end}")
    print(f"  Resolution: {resolution} m")
    print()

    # ---- 1. GEE auth ------------------------------------------------------
    if not _has_gee_creds():
        print(
            "GEE credentials not found. Set in .env:\n"
            "  GEE_SERVICE_ACCOUNT=your-sa@project.iam.gserviceaccount.com\n"
            "  GEE_PRIVATE_KEY_PATH=/path/to/key.json\n"
            "  GEE_PROJECT_ID=your-gcp-project  (optional, auto-detected from key)\n"
            "\n"
            "Exiting gracefully (exit code 0)."
        )
        return 0

    print("Authenticating Earth Engine …")
    try:
        init_ee(settings)
        print("  ✓ authenticated")
    except Exception as exc:
        print(f"  ✗ auth failed: {exc}", file=sys.stderr)
        return 1

    # ---- 2. Pull Landsat --------------------------------------------------
    print("\nPulling Landsat 8/9 LST …")
    landsat_paths = pull_landsat_lst(bbox, start, end, out_dir="data/raw/landsat")
    print(f"  → {len(landsat_paths)} scenes exported (check GEE Tasks)")

    # ---- 3. Pull MODIS ----------------------------------------------------
    print("Pulling MODIS daily LST …")
    day_paths, night_paths = pull_modis_lst(bbox, start, end, out_dir="data/raw/modis")
    print(f"  → {len(day_paths)} day + {len(night_paths)} night GeoTIFFs exported")

    # ---- 4. Load into xarray ----------------------------------------------
    print("\nLoading GeoTIFFs into xarray …")
    try:
        landsat_da = _load_geotiffs(landsat_paths)
        print(f"  Landsat: {landsat_da.sizes}")
    except FileNotFoundError:
        print("  Landsat: no files yet — GEE tasks may still be running.")
        print("  Re-run after tasks complete, or load manually.")
        landsat_da = None

    try:
        modis_da = _load_geotiffs(day_paths)
        print(f"  MODIS day: {modis_da.sizes}")
    except FileNotFoundError:
        modis_da = None
        print("  MODIS day: no files yet.")

    try:
        modis_night_da = _load_geotiffs(night_paths)
        print(f"  MODIS night: {modis_night_da.sizes}")
    except FileNotFoundError:
        modis_night_da = None
        print("  MODIS night: no files yet.")

    if landsat_da is None or modis_da is None:
        print("\n⚠  One or more data sources empty — cannot fuse.", file=sys.stderr)
        print("  Wait for GEE tasks to finish, then re-run.", file=sys.stderr)
        return 0

    # ---- 5. Fusion --------------------------------------------------------
    print("\nFusing to hourly 30 m …")
    fused = fuse_to_hourly(
        landsat_da=landsat_da,
        modis_da=modis_da,
        modis_night_da=modis_night_da,
        target_resolution_m=resolution,
    )
    print(f"  Fused: {fused.sizes} — time={fused.time.values[0]} … {fused.time.values[-1]}")

    # ---- 6. Write zarr ----------------------------------------------------
    out_path = Path("data/processed/lst_hourly.zarr")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"\nWriting → {out_path}")
    fused.to_zarr(str(out_path), mode="w")
    print(f"  ✓ wrote {out_path}")

    # ---- 7. Clear-sky RMSE (AC-009) ---------------------------------------
    print("\n=== Clear-sky RMSE (AC-009) ===")
    try:
        # Compute RMSE against the Landsat scenes we fused from
        rmse = clear_sky_rmse(fused, landsat_da)
        print(f"  RMSE: {rmse:.3f} °C")
        if rmse < 1.5:
            print("  ✓ AC-009 PASS (RMSE < 1.5 °C)")
        else:
            print(f"  ⚠ AC-009 target not met ({rmse:.3f} ≥ 1.5 °C)")
    except Exception as exc:
        print(f"  RMSE computation failed: {exc}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
