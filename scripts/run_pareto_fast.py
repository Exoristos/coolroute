"""Run Pareto routing on pre-computed MC graph (expanded area)."""
from __future__ import annotations

import json
import pickle
import time
import warnings

import osmnx as ox

from uhi_battery.routing.pareto import check_objective_correlation, solve_pareto

warnings.filterwarnings("ignore")

OD_PAIRS = [
    {"label": "A: Moda->Kozyatagi", "o": (29.02, 40.97), "d": (29.10, 40.91)},
    {"label": "B: Kadikoy->Moda", "o": (29.06, 40.98), "d": (29.02, 40.97)},
    {"label": "C: Bostanci->Hasanpasa", "o": (29.09, 40.96), "d": (29.05, 40.99)},
    {"label": "D: Uskudar->Atasehir", "o": (29.01, 41.02), "d": (29.20, 40.99)},
    {"label": "E: Kalamis->Camlica", "o": (29.03, 40.96), "d": (29.12, 41.03)},
    {"label": "F: Maltepe->Umraniye", "o": (29.13, 40.92), "d": (29.22, 41.00)},
]


def main() -> int:
    t0 = time.perf_counter()
    print("=== Pareto Routing (6 OD pairs, expanded area) ===")

    with open("data/processed/graph_mc.pkl", "rb") as f:
        G = pickle.load(f)
    elapsed = time.perf_counter() - t0
    print(
        f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges "
        f"({elapsed:.1f}s)"
    )

    print("\nCorrelation pre-check...")
    corr = check_objective_correlation(G, n_samples=30, seed=42)
    r_val = corr["r"]
    print(f"  r={r_val:.4f}, decision={corr['decision']}")

    all_results = []
    non_degenerate = 0

    for pair in OD_PAIRS:
        label = pair["label"]
        print(f"\n  {label}")
        try:
            o_node = int(ox.distance.nearest_nodes(G, pair["o"][0], pair["o"][1]))
            d_node = int(ox.distance.nearest_nodes(G, pair["d"][0], pair["d"][1]))

            frontier = solve_pareto(
                G, o_node, d_node, pop_size=20, n_gen=20, seed=42, obj2_attr="degree_hours"
            )
            f_size = len(frontier)
            print(f"    Frontier size: {f_size}")

            if f_size > 1:
                non_degenerate += 1
                e_vals = [f["energy_wh"] for f in frontier]
                dh_vals = [f["obj2_value"] for f in frontier]
                energy_delta = max(e_vals) - min(e_vals)
                print(
                    f"    Energy: {min(e_vals):.1f}-{max(e_vals):.1f} Wh "
                    f"(delta={energy_delta:.1f})"
                )
                degree_delta = max(dh_vals) - min(dh_vals)
                print(
                    f"    Degree-hrs: {min(dh_vals):.2f}-{max(dh_vals):.2f} "
                    f"(delta={degree_delta:.2f})"
                )

                min_e = min(frontier, key=lambda x: x["energy_wh"])
                min_dh = min(frontier, key=lambda x: x["obj2_value"])
                if min_e["route"] != min_dh["route"]:
                    print(
                        f"    Non-degenerate! Min-E: {len(min_e['route'])} nodes, "
                        f"Min-DH: {len(min_dh['route'])} nodes"
                    )
                else:
                    print("    Same route (degenerate)")
            else:
                print("    Degenerate (single route)")

            all_results.append({
                "label": label,
                "origin_node": o_node,
                "dest_node": d_node,
                "frontier_size": f_size,
                "frontier": frontier,
            })
        except Exception as exc:
            print(f"    [ERROR] {exc}")
            all_results.append({"label": label, "error": str(exc)})

    print("\n=== Summary ===")
    print(f"Non-degenerate: {non_degenerate}/{len(OD_PAIRS)}")
    print(f"Total time: {time.perf_counter()-t0:.1f}s")

    output = {
        "bbox": [28.95, 40.88, 29.25, 41.12],
        "correlation_r": r_val,
        "obj2": "degree_hours",
        "non_degenerate_count": non_degenerate,
        "pairs": all_results,
    }
    with open("data/processed/pareto_frontiers.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("Saved to data/processed/pareto_frontiers.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
