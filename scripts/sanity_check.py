"""P1.5 sanity check on fused LST zarr."""
import numpy as np
import xarray as xr

from uhi_battery.data.fusion import reconstruct_lst_at_hour

ds = xr.open_zarr("data/processed/lst_hourly.zarr")
print("=== P1.5 Sanity Check ===")
print(f"Variables: {list(ds.data_vars)}")
print(f"Sizes: {dict(ds.sizes)}")
start = str(ds.time.values[0])[:10]
end = str(ds.time.values[-1])[:10]
print(f"Time: {start} .. {end} ({len(ds.time)} days)")
print(f"CRS: {ds.attrs.get('crs')}")
print(f"Resolution: {ds.attrs.get('resolution_m')}m")
print(f"BBox: {ds.attrs.get('bbox')}")

for v in ds.data_vars:
    vals = ds[v].values
    finite = vals[np.isfinite(vals)]
    print(f"\n{v}:")
    print(f"  range: {finite.min():.1f} .. {finite.max():.1f} C")
    percentiles = np.percentile(finite, [1, 5, 50, 95, 99])
    p1, p5, p50, p95, p99 = percentiles
    print(
        f"  p1={p1:.1f} p5={p5:.1f} p50={p50:.1f} "
        f"p95={p95:.1f} p99={p99:.1f}"
    )
    nan_pct = 100 * np.isnan(vals).sum() / vals.size
    print(f"  NaN: {np.isnan(vals).sum()}/{vals.size} ({nan_pct:.1f}%)")

print("\nMonthly mean lst_daily:")
for m in range(5, 11):
    mask = ds.time.dt.month == m
    if mask.any():
        mvals = ds["lst_daily"].sel(time=ds.time[mask]).values
        mf = mvals[np.isfinite(mvals)]
        print(f"  2025-{m:02d}: mean={mf.mean():.1f}C  (n={int(mask.sum().values)} days)")

# Reconstruct a sample hour (14:00 = peak) for first day
lst_14 = reconstruct_lst_at_hour(ds, hour=14.0, day_idx=0)
lst_14_min, lst_14_max = float(lst_14.min()), float(lst_14.max())
print(f"\nReconstructed LST at 14:00 (day 0): {lst_14_min:.1f} .. {lst_14_max:.1f} C")
lst_06 = reconstruct_lst_at_hour(ds, hour=6.0, day_idx=0)
lst_06_min, lst_06_max = float(lst_06.min()), float(lst_06.max())
print(f"Reconstructed LST at 06:00 (day 0): {lst_06_min:.1f} .. {lst_06_max:.1f} C")
print("  (14:00 should be warmer than 06:00)")
