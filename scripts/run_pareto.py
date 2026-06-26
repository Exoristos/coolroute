"""P5 runner: Pareto multi-objective routing with COASTAL OD pairs.

Uses geographically meaningful OD pairs where coastal (cooler) detours exist,
creating genuine energy-vs-thermal trade-offs.  Forces degree_hours as the
2nd objective to capture thermal heterogeneity even when global correlation
is high.

Usage::

    uv run python scripts/run_pareto.py

Requires internet (Overpass API + GEE).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import xarray as xr

from uhi_battery.config import settings
from uhi_battery.data.gee_lst import init_ee
from uhi_battery.routing.network import load_network, pull_dem
from uhi_battery.routing.pareto import (
    assign_edge_attributes,
    check_objective_correlation,
    solve_pareto,
)

# ── Paths ───────────────────────────────────────────────────────────────────
DEM_PATH = Path("data/raw/dem/srtm.tif")
LST_ZARR = Path("data/processed/lst_hourly.zarr")
LST_EXPANDED = Path("data/processed/lst_expanded.nc")
OUT_JSON = Path("data/processed/pareto_frontiers.json")

# ── Coastal OD pairs (expanded Anatolian side) ──────────────────────────────
# All pairs stay on the Asian side of the Bosphorus (Council recommendation:
# cross-strait routes funnel through bridges → degenerate frontiers).
# Pairs span coast↔inland across the expanded Kadıköy+Üsküdar+Ataşehir area.
COASTAL_OD_PAIRS: list[dict[str, tuple[float, float] | str]] = [
    {
        "label": "Pair A: Moda coast → Kozyatağı inland",
        "origin_coords": (29.02, 40.97),
        "dest_coords": (29.10, 40.91),
        "desc": "Coast (cool) to inland (hot) — coastal detour possible",
    },
    {
        "label": "Pair B: Kadıköy center → Moda coast",
        "origin_coords": (29.06, 40.98),
        "dest_coords": (29.02, 40.97),
        "desc": "Center (warmer) to coast (cooler) — detour via waterfront",
    },
    {
        "label": "Pair C: Bostancı coast → Hasanpaşa inland",
        "origin_coords": (29.09, 40.96),
        "dest_coords": (29.05, 40.99),
        "desc": "Coast (cool) to inland (hot) — inland vs coastal route trade-off",
    },
    {
        "label": "Pair D: Üsküdar coast → Ataşehir inland",
        "origin_coords": (29.01, 41.02),
        "dest_coords": (29.20, 40.99),
        "desc": "Üsküdar waterfront (cool) to Ataşehir inland (hot) — long coast-inland gradient",
    },
    {
        "label": "Pair E: Kalamış coast → Çamlıca hill",
        "origin_coords": (29.03, 40.96),
        "dest_coords": (29.12, 41.03),
        "desc": "Coast (cool, low) to Çamlıca hill (hot, elevated) — strong gradient",
    },
    {
        "label": "Pair F: Maltepe coast → Ümraniye inland",
        "origin_coords": (29.13, 40.92),
        "dest_coords": (29.22, 41.00),
        "desc": "South coast (cool) to northeast inland (hot) — cross-city gradient",
    },
]


def _find_node_near(
    G, lon: float, lat: float
) -> int:
    """Find the nearest network node to (lon, lat) using OSMnx."""
    import osmnx as ox
    return int(ox.distance.nearest_nodes(G, lon, lat))


def main() -> int:
    t_start = time.perf_counter()
    print("=" * 60)
    print("  P5 — Pareto Routing (COASTAL OD PAIRS)")
    print("=" * 60)
    print(f"  Pilot bbox: {settings.pilot_bbox}")

    # ── 1. DEM ──────────────────────────────────────────────────────────
    print("\n── 1. DEM (SRTM) ──")
    try:
        init_ee(settings)
        dem = pull_dem(settings.pilot_bbox, DEM_PATH)
        print(f"  DEM: {dem}")
    except Exception as exc:
        print(f"  [WARN] DEM pull failed ({exc}) — using flat terrain")

    # ── 2. Network ──────────────────────────────────────────────────────
    print("\n── 2. Network (OSMnx) ──")
    try:
        G = load_network(settings.pilot_bbox, DEM_PATH)
        n_nodes = G.number_of_nodes()
        n_edges = G.number_of_edges()
        print(f"  Nodes: {n_nodes}, Edges: {n_edges}")
    except Exception as exc:
        print(f"  [ERROR] Network load failed: {exc}")
        return 1

    # ── 3. LST (expanded single-day) ───────────────────────────────────
    print("\n── 3. LST (expanded single-day) ──")
    lst_ds = None
    hottest_idx = 0
    if LST_EXPANDED.exists():
        try:
            lst_ds = xr.open_dataset(str(LST_EXPANDED))
            print(f"  Loaded expanded LST: {lst_ds.sizes}")
            print(f"  LST range: {float(lst_ds.lst_daily.min()):.1f} - {float(lst_ds.lst_daily.max()):.1f} C")
            print(f"  Mean LST: {float(lst_ds.lst_daily.mean()):.1f} C")
        except Exception as exc:
            print(f"  [WARN] Expanded LST load failed: {exc}")
    elif LST_ZARR.exists():
        try:
            lst_ds = xr.open_zarr(str(LST_ZARR))
            print(f"  Loaded (old bbox) zarr: {lst_ds.sizes}")
            hottest_idx = int(
                lst_ds["lst_daily"].mean(dim=["x", "y"]).argmax().values
            )
            print(f"  Hottest day index: {hottest_idx}")
        except Exception as exc:
            print(f"  [WARN] LST load failed: {exc}")
    else:
        print(f"  [WARN] No LST file found")

    # ── 4. Edge attributes (MC drive cycle) ─────────────────────────────
    print("\n── 4. Edge attributes (MC drive cycle, n_sim=50) ──")
    try:
        G = assign_edge_attributes(
            G, lst_ds=lst_ds, day_idx=hottest_idx, hour=14.0,
            use_mc=True, n_mc_simulations=50, stops_per_km=12.0,
        )
        e_count = sum(
            1 for u, v, k in G.edges(keys=True)
            if G[u][v][k].get("energy_wh", 0) > 0
        )
        print(f"  Edges with energy: {e_count}")
        if lst_ds is not None:
            temps = [
                float(G[u][v][k].get("temp_c", 0))
                for u, v, k in G.edges(keys=True)
            ]
            if temps:
                print(f"  Edge temps: {min(temps):.1f} – {max(temps):.1f} °C")
                cool = sum(1 for t in temps if t < 30)
                hot = sum(1 for t in temps if t >= 35)
                print(
                    f"  Cool edges (<30°C): {cool}, Hot edges (≥35°C): {hot}"
                )
    except Exception as exc:
        print(f"  [ERROR] Attribute assignment failed: {exc}")
        return 1

    # ── 5. Correlation check ────────────────────────────────────────────
    print("\n── 5. Correlation pre-check ──")
    corr = {"decision": "energy", "r": 0.0}
    try:
        corr = check_objective_correlation(G, n_samples=50, seed=42)
        r_val = corr.get("r", 0)
        print(f"  Pearson r = {r_val:.4f} (p = {corr['p_value']:.4f})")
        print(f"  Decision: 2nd objective = {corr['decision']}")
    except Exception as exc:
        print(f"  [WARN] Correlation check failed: {exc}")

    # ═══════════════════════════════════════════════════════════════════
    # 6. Coastal OD pairs — FORCE degree_hours as 2nd objective
    # ═══════════════════════════════════════════════════════════════════
    obj2 = "degree_hours"  # force thermal objective for coastal pairs
    pop_size, n_gen = 20, 20
    print(
        f"\n── 6. Pareto frontiers (NSGA-II, "
        f"obj2={obj2}, pop={pop_size}, gen={n_gen}) ──"
    )

    all_results: list[dict] = []
    non_degenerate = 0

    for idx, pair in enumerate(COASTAL_OD_PAIRS):
        label = pair["label"]
        o_lon, o_lat = pair["origin_coords"]
        d_lon, d_lat = pair["dest_coords"]
        desc = pair.get("desc", "")

        print(f"\n  {label}")
        print(f"    {desc}")

        try:
            o_node = _find_node_near(G, o_lon, o_lat)
            d_node = _find_node_near(G, d_lon, d_lat)
            print(f"    Nodes: {o_node} → {d_node}")
        except Exception as exc:
            print(f"    [ERROR] Node lookup failed: {exc}")
            continue

        try:
            frontier = solve_pareto(
                G, o_node, d_node,
                pop_size=pop_size, n_gen=n_gen, seed=42,
                obj2_attr=obj2,
            )
            f_size = len(frontier)
            print(f"    Frontier size: {f_size}")

            if f_size > 1:
                non_degenerate += 1

            if frontier:
                e_vals = [f["energy_wh"] for f in frontier]
                dh_vals = [f["obj2_value"] for f in frontier]
                print(
                    f"    Energy:     {min(e_vals):.1f} – {max(e_vals):.1f} Wh "
                    f"(Δ={max(e_vals)-min(e_vals):.1f})"
                )
                print(
                    f"    Degree-hrs: {min(dh_vals):.2f} – {max(dh_vals):.2f} "
                    f"(Δ={max(dh_vals)-min(dh_vals):.2f})"
                )

                # Route descriptions: min-energy vs min-degree-hours
                min_e = min(frontier, key=lambda x: x["energy_wh"])
                min_dh = min(frontier, key=lambda x: x["obj2_value"])
                if min_e["route"] != min_dh["route"]:
                    print(
                        f"    Min-energy route: {len(min_e['route'])} nodes, "
                        f"{min_e['energy_wh']:.1f} Wh, "
                        f"{min_e['obj2_value']:.2f} dh"
                    )
                    print(
                        f"    Coolest route:    {len(min_dh['route'])} nodes, "
                        f"{min_dh['energy_wh']:.1f} Wh, "
                        f"{min_dh['obj2_value']:.2f} dh"
                    )
                else:
                    # Check alpha spread
                    alphas = sorted(f["alpha"] for f in frontier)
                    print(
                        f"    Alpha range: {alphas[0]:.3f} – {alphas[-1]:.3f} "
                        f"(same route for all α → degenerate)"
                    )
        except Exception as exc:
            print(f"    [ERROR] Pareto solve failed: {exc}")
            import traceback
            traceback.print_exc()
            frontier = []

        all_results.append({
            "pair_idx": idx + 1,
            "label": label,
            "origin_node": o_node,
            "dest_node": d_node,
            "obj2": obj2,
            "frontier_size": len(frontier) if frontier else 0,
            "frontier": frontier,
        })

    # ═══════════════════════════════════════════════════════════════════
    # 7. Summary
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print("  Summary")
    print(f"  Global correlation r: {corr.get('r', 0):.4f}")
    print(f"  Forced 2nd objective: {obj2}")
    sizes = [r.get("frontier_size", 0) for r in all_results]
    print(f"  Frontier sizes: {sizes}")
    print(f"  Non-degenerate: {non_degenerate}/{len(COASTAL_OD_PAIRS)}")
    if non_degenerate == 0:
        print("  ⚠ All frontiers degenerate — spatial thermal heterogeneity")
        print("    insufficient in pilot bbox for energy-vs-thermal trade-off.")
        print("    Expected: compact urban area with strong temp gradient")
        print("    from Marmara coast (cooler) to inland (hotter) not enough")
        print("    for distinct route alternatives at the α-resolution used.")
    print(f"{'=' * 60}")

    # ── 8. Save ─────────────────────────────────────────────────────────
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "bbox": list(settings.pilot_bbox),
        "correlation_r": corr.get("r", 0),
        "obj2": obj2,
        "pop_size": pop_size,
        "n_gen": n_gen,
        "non_degenerate_count": non_degenerate,
        "pairs": all_results,
    }
    OUT_JSON.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n  ✓ Saved → {OUT_JSON}")

    elapsed = time.perf_counter() - t_start
    print(f"\n  P5 complete in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
