"""P3 runner: train battery models (SoH Arrhenius + validate energy physics).

Usage::

    uv run python scripts/train_models.py

Fully offline — no OSMnx, no network.  Completes in < 5 seconds.

Produces:
* ``data/processed/soh_model.pkl`` — Arrhenius SoH model (Ea fixed, A calibrated)
* ``data/processed/energy_model.pkl`` — physics energy parameters + validation
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from uhi_battery.config import settings
from uhi_battery.data.nasa_aging import load_nasa_aging
from uhi_battery.models.energy import save_energy_model, validate_energy_model
from uhi_battery.models.soh import (
    compute_fade_rates,
    evaluate_soh,
    filter_nasa_outliers,
    fit_arrhenius,
    predict_soh,
    save_soh_model,
)

# ── Paths ───────────────────────────────────────────────────────────────────
NASA_DIR = Path("data/raw/nasa_battery")
ENERGY_MODEL_PATH = Path("data/processed/energy_model.pkl")
SOH_MODEL_PATH = Path("data/processed/soh_model.pkl")


def main() -> int:
    t_start = time.perf_counter()

    print("=" * 60)
    print("  P3 — Battery Model Training")
    print("=" * 60)
    print(f"  Random seed: {settings.random_seed}")

    # ═══════════════════════════════════════════════════════════════════
    # 1. SoH Arrhenius model (NASA PCoE)
    # ═══════════════════════════════════════════════════════════════════
    print("\n── 1. SoH Arrhenius calibration (NASA PCoE) ──\n")

    # Load
    df = load_nasa_aging(NASA_DIR)
    print(f"  Raw NASA data: {len(df)} cycles, {df['cell_id'].nunique()} cells")
    print(f"  Retention range: {df['capacity_retention_pct'].min():.1f} – "
          f"{df['capacity_retention_pct'].max():.1f}%")

    # Filter
    df_clean = filter_nasa_outliers(df)
    print(f"  After filtering: {len(df_clean)} cycles, {df_clean['cell_id'].nunique()} cells")

    # Compute exponential fade rates per cell
    fade_df = compute_fade_rates(df_clean)
    print(f"  Fade rates computed for {len(fade_df)} cells")
    print(f"  Mean fade rate (exponential k): {fade_df['fade_rate_per_cycle'].mean():.6f} /cycle")
    print(f"  Regimes: {sorted(fade_df['regime_C'].unique())}")

    # Fit Arrhenius (Ea fixed at 45 kJ/mol, warm side only)
    model = fit_arrhenius(fade_df, min_temp_c=15.0)
    ea_kj = model["Ea"] / 1000.0
    print("\n  Arrhenius fit (Ea fixed from literature):")
    print(f"    Ea = {ea_kj:.1f} kJ/mol  (fixed — LCO 18650 literature)")
    print(f"    A  = {model['A']:.4e}  (95% CI: ±{model['ci_A']:.4e})")
    print(f"    R² = {model['R²']:.4f}  (warm regimes, T ≥ {model['min_temp_c']}°C)")
    print(f"    n  = {model['n_cells']} warm cells ({model['n_total_cells']} total)")

    # Evaluate
    eval_result = evaluate_soh(model, fade_df)
    print("\n  Evaluation (warm regimes only):")
    print(f"    R² (fade rate):      {eval_result['R²']:.4f}")
    print(f"    RMSE (fade rate):    {eval_result['RMSE_fade_rate']:.6f}")
    if eval_result["cold_rmse"] is not None:
        print(
            "    Cold RMSE (no R²):   "
            f"{eval_result['cold_rmse']:.6f} "
            "(lithium plating — model excluded)"
        )

    # Leave-one-cell-out
    loco = eval_result["loco_results"]
    if loco:
        print(f"\n  Leave-one-cell-out (LOCO) — {len(loco)} warm cells:")
        mean_loco = eval_result["loco_mean_rmse"]
        print(f"    Mean LOCO RMSE:      {mean_loco:.6f}")
        print(f"    {'Cell':>12s}  {'n_train':>7s}  {'RMSE':>10s}")
        for r in sorted(loco, key=lambda x: x["RMSE_fade_rate"], reverse=True)[:6]:
            print(
                f"    {r['held_out_cell']:>12s}  "
                f"{r['n_train']:>7d}  "
                f"{r['RMSE_fade_rate']:>10.6f}"
            )

    # Predict examples
    print("\n  Example predictions (SoH after 500 cycles, exponential decay):")
    for t_c in [5, 25, 45]:
        soh = predict_soh(model, t_c, 500)
        flag = " (cold — underestimate)" if t_c < 15 else ""
        print(f"    {t_c:>3d}°C → {soh:.1f}% retention{flag}")

    save_soh_model(model, eval_result, SOH_MODEL_PATH)
    print(f"\n  ✓ Saved → {SOH_MODEL_PATH}")

    # ═══════════════════════════════════════════════════════════════════
    # 2. Energy physics model — validate
    # ═══════════════════════════════════════════════════════════════════
    print("\n── 2. Energy physics model (validation) ──\n")

    validation = validate_energy_model()
    print(f"  {validation['summary']}")
    print()
    for pt in validation["points"]:
        extra = ""
        if "delta_Wh" in pt:
            extra = f"  (Δ={pt['delta_Wh']:+.2f} Wh vs expected)"
        print(f"    {pt['label']}: {pt['energy_Wh']} Wh ({pt['Wh_per_km']} Wh/km){extra}")

    save_energy_model(ENERGY_MODEL_PATH)
    print(f"\n  ✓ Saved → {ENERGY_MODEL_PATH}")

    # ═══════════════════════════════════════════════════════════════════
    elapsed = time.perf_counter() - t_start
    print(f"\n{'=' * 60}")
    print(f"  Training complete in {elapsed:.1f}s.")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
