"""P1(b) runner: generate simulated e-scooter trips → parquet.

Usage::

    uv run python scripts/gen_trips.py

Produces ``data/processed/trips.parquet`` with deterministic, seed-controlled
trip data for the pilot corridor.

No network calls when OSM data is already cached (osmnx uses a local cache by
default).  Without connectivity, the script will fail on the OSM POI fetch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from uhi_battery.config import settings
from uhi_battery.data.trips import simulate_trips


def main() -> int:
    """Generate trips and write parquet."""
    bbox = settings.pilot_bbox
    seed = settings.random_seed
    start = settings.data_start_date
    end = settings.data_end_date

    # ── Volume calibration ────────────────────────────────────────────
    # 2024 summer: ~150 days × reasonable daily trip density for pilot
    # corridor (~5–10 km²).  For a ~7 km² area, 20 trips/day gives ~3000.
    n_trips = 3000

    print("=== UHI Battery — P1(b) Trip Generation ===")
    print(f"  Pilot bbox: {bbox}")
    print(f"  Window:     {start} → {end}")
    print(f"  n_trips:    {n_trips}")
    print(f"  Seed:       {seed}")
    print()

    # ── Generate ──────────────────────────────────────────────────────
    print("Fetching OSM POIs and generating trips …")
    try:
        trips = simulate_trips(
            bbox=bbox,
            n_trips=n_trips,
            start=start,
            end=end,
            seed=seed,
        )
    except RuntimeError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1

    # ── Summary stats ─────────────────────────────────────────────────
    dists = trips["distance_m"].values
    median_m = float(np.median(dists))
    p90_m = float(np.percentile(dists, 90))
    print(f"\n  Trips generated: {len(trips)}")
    print(f"  Distance (median): {median_m:.0f} m ({median_m/1000:.1f} km)")
    print(f"  Distance (p90):    {p90_m:.0f} m ({p90_m/1000:.1f} km)")
    print(f"  Distance (max):    {dists.max():.0f} m ({dists.max()/1000:.1f} km)")

    # ── Write parquet ─────────────────────────────────────────────────
    out_path = Path("data/processed/trips.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    trips.to_parquet(out_path, index=False)
    print(f"\n  ✓ Wrote {out_path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
