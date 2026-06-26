"""State-of-Health (SoH) degradation model — Arrhenius calibration.

Fits an exponential Arrhenius-type capacity-fade model to NASA PCoE aging data:

    retention(%) = 100 · exp(−k · n_cycles)
    k = A · exp(−Ea / (R·T))

where *k* is the exponential decay constant (per cycle), *Ea* the activation
energy (J/mol), *A* the pre-exponential factor (1/cycle), *R* the universal
gas constant (8.314 J/mol·K), and *T* the absolute temperature (K).

**Ea is fixed from literature** for LCO 18650 chemistry (45 kJ/mol).
Only *A* is calibrated from NASA PCoE data.  This reflects the well-established
activation energy for SEI growth / lithium plating in LCO cells.

.. warning::

    * Calibrated on NASA PCoE lab data (LCO 18650, 5/12/24/40/44 °C regimes).
      Not validated on field telemetry or heterogeneous chemistries.
    * **Cold-temperature degradation** (< 15 °C, lithium plating) is
      **not captured** — the Arrhenius form only describes warm-side degradation.
      See ``evaluate_soh`` for cold-regime diagnostics.
    * ``dwell_time_h`` (calendar aging) is **not available** from NASA PCoE
      (see :mod:`uhi_battery.data.nasa_aging` module docstring).

Reference: Oracle locked — NASA primary, Oxford validation only.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import linregress
from scipy.stats import t as t_dist

# ── Constants ───────────────────────────────────────────────────────────────
_R: float = 8.314  # universal gas constant (J/mol·K)
_KELVIN_OFFSET: float = 273.15  # °C → K

# Literature-fixed activation energy for LCO 18650 cycle aging (J/mol).
# Source: consensus Ea for SEI growth / lithium plating in LCO (40-50 kJ/mol).
# We fix this to avoid overfitting the small NASA dataset and to improve
# generalisability.
_EA_FIXED: float = 45000.0  # 45 kJ/mol

# NASA PCoE nominal test temperatures (°C).
NASA_REGIMES: tuple[float, ...] = (5.0, 12.0, 24.0, 40.0, 44.0)


# ── Internal helpers ────────────────────────────────────────────────────────


def _nearest_regime(temp_c: float | np.ndarray) -> float | np.ndarray:
    """Map measured temperature(s) to the nearest NASA regime."""
    regimes = np.array(NASA_REGIMES)
    if isinstance(temp_c, np.ndarray):
        idx = np.abs(temp_c[:, None] - regimes[None, :]).argmin(axis=1)
        return regimes[idx]
    return float(min(regimes, key=lambda r: abs(float(r - temp_c))))


# ── Public API ──────────────────────────────────────────────────────────────


def filter_nasa_outliers(df: pd.DataFrame, min_cycles: int = 10) -> pd.DataFrame:
    """Filter out anomalous retention values and degenerate cells.

    Drops:
    * Rows where ``capacity_retention_pct < 0`` or ``> 100``.
    * Cells with fewer than *min_cycles* total data points.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`~uhi_battery.data.nasa_aging.load_nasa_aging`.
    min_cycles : int
        Minimum number of cycles per cell to retain.

    Returns
    -------
    pd.DataFrame
        Filtered copy of *df*.
    """
    df = df.copy()
    df = df[(df["capacity_retention_pct"] >= 0) & (df["capacity_retention_pct"] <= 100)]

    cycle_counts = df.groupby("cell_id")["cycles"].max()
    valid_cells = cycle_counts[cycle_counts >= min_cycles].index
    df = df[df["cell_id"].isin(valid_cells)]

    return df


def compute_fade_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Compute per-cell exponential decay rates from NASA PCoE data.

    For each cell, fits the exponential decay model:

        retention(%) = 100 · exp(−k · cycles)

    via linearised regression:  ln(retention/100) = −k · cycles.
    The slope *k* is the **fade rate** (exponential decay constant per cycle).

    Parameters
    ----------
    df : pd.DataFrame
        Filtered NASA aging data (see :func:`filter_nasa_outliers`).

    Returns
    -------
    pd.DataFrame
        ``[cell_id, regime_C, temp_C, temp_K, fade_rate_per_cycle,
        n_cycles, final_retention, r_sq]``.
    """
    records: list[dict[str, Any]] = []

    for cell_id, grp in df.groupby("cell_id"):
        cyc = grp["cycles"].values.astype(float)
        ret = grp["capacity_retention_pct"].values.astype(float)

        # Drop rows with non-positive retention (can't take log)
        valid = ret > 0.0
        if valid.sum() < 3:
            continue
        cyc_v, ret_v = cyc[valid], ret[valid]

        # Linearise: ln(ret/100) = -k * cycles
        y = np.log(ret_v / 100.0)
        result = linregress(cyc_v, y)

        k = float(-result.slope)  # positive fade rate
        r_sq = float(result.rvalue) ** 2

        avg_temp = float(grp["temp_C"].mean())
        regime_c = float(_nearest_regime(avg_temp))  # type: ignore[arg-type]
        last = grp.loc[grp["cycles"].idxmax()]

        records.append(
            {
                "cell_id": cell_id,
                "regime_C": regime_c,
                "temp_C": avg_temp,
                "temp_K": avg_temp + _KELVIN_OFFSET,
                "fade_rate_per_cycle": k,
                "n_cycles": int(last["cycles"]),
                "final_retention": float(last["capacity_retention_pct"]),
                "r_sq": r_sq,
            }
        )

    return pd.DataFrame(records)


def regime_fade_rates(
    fade_df: pd.DataFrame,
) -> pd.DataFrame:
    """Average fade rates within NASA temperature regimes.

    Groups cells into the 5 known NASA regimes (5/12/24/40/44 °C) and computes
    geometric-mean fade rate per regime.

    Parameters
    ----------
    fade_df : pd.DataFrame
        Output of :func:`compute_fade_rates`.

    Returns
    -------
    pd.DataFrame
        ``[regime_C, temp_K, fade_rate_per_cycle, n_cells]``.
    """
    df = fade_df[fade_df["fade_rate_per_cycle"] > 0].copy()
    df["regime"] = _nearest_regime(df["temp_C"].values)

    records = []
    for regime, grp in df.groupby("regime"):
        log_fade = np.log(grp["fade_rate_per_cycle"])
        avg_fade = float(np.exp(log_fade.mean()))
        avg_temp_k = float(grp["temp_K"].mean())

        records.append(
            {
                "regime_C": round(float(regime), 1),
                "temp_K": avg_temp_k,
                "fade_rate_per_cycle": avg_fade,
                "n_cells": len(grp),
            }
        )

    return pd.DataFrame(records)


def fit_arrhenius(
    fade_df: pd.DataFrame,
    min_temp_c: float | None = 15.0,
) -> dict[str, Any]:
    """Fit the Arrhenius model with literature-fixed Ea.

    Activation energy *Ea* is fixed to 45 kJ/mol (LCO 18650 literature value).
    Only the pre-exponential factor *A* is calibrated from the data:

        ln(k) + Ea/(R·T) = ln(A)
        →  A = exp(mean(ln(k) + Ea/(R·T)))

    95% CI on A is computed from the log-normal distribution of the data.

    .. note::
        Fits only on warm regimes (T ≥ 15 °C) by default.  Cold-regime
        degradation (lithium plating) is not captured by this Arrhenius form.

    Parameters
    ----------
    fade_df : pd.DataFrame
        Output of :func:`compute_fade_rates`.
    min_temp_c : float | None
        Minimum temperature (°C) for fitting.  Default 15 °C to exclude
        cold-plating regime.  Set to ``None`` to use all data.

    Returns
    -------
    dict
        Keys: ``Ea`` (fixed = 45000 J/mol), ``A``, ``ci_A`` (95% CI half-width),
        ``n_cells``, ``min_temp_c``, ``R²`` (on included data).
    """
    df = fade_df[fade_df["fade_rate_per_cycle"] > 0].copy()

    if min_temp_c is not None:
        df = df[df["temp_K"] >= (min_temp_c + _KELVIN_OFFSET)]

    if len(df) < 2:
        raise ValueError(
            f"Need ≥2 cells with positive fade rates; got {len(df)}. "
            "Try lowering min_temp_c."
        )

    # ln(k) + Ea/(R·T) = ln(A)
    t_k = df["temp_K"].values
    k_vals = df["fade_rate_per_cycle"].values
    ln_a_vals = np.log(k_vals) + _EA_FIXED / (_R * t_k)

    # A = geometric mean of A_i → exp(mean(ln(A_i)))
    ln_a = float(np.mean(ln_a_vals))
    a_pre = float(np.exp(ln_a))

    # 95% CI on A (log-normal)
    n = len(df)
    std_ln_a = float(np.std(ln_a_vals, ddof=1))
    t_crit = t_dist.ppf(0.975, n - 1)
    ci_ln_a = t_crit * std_ln_a / np.sqrt(n)
    ci_a = float(a_pre * (np.exp(ci_ln_a) - np.exp(-ci_ln_a)) / 2.0)

    # R²: how much variance in ln(k) is explained by -Ea/(R·T)?
    # This is just 1 - var(residual)/var(ln(k)). With fixed Ea, it measures
    # how well the Arrhenius slope fits the data.
    pred_ln_k = ln_a - _EA_FIXED / (_R * t_k)
    ss_res = np.sum((np.log(k_vals) - pred_ln_k) ** 2)
    ss_tot = np.sum((np.log(k_vals) - np.log(k_vals).mean()) ** 2)
    r_sq = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    total_cells = fade_df["cell_id"].nunique() if "cell_id" in fade_df.columns else n

    return {
        "Ea": _EA_FIXED,
        "A": a_pre,
        "ci_A": ci_a,
        "R²": r_sq,
        "n_cells": n,
        "n_total_cells": total_cells,
        "min_temp_c": min_temp_c,
    }


def predict_soh(
    model: dict[str, Any],
    temp_c: float,
    n_cycles: float,
) -> float:
    """Predict capacity retention (%) after *n_cycles* at temperature *temp_c*.

    Uses exponential decay::

        SoH(%) = 100 · exp(−k · n_cycles)
        k = A · exp(−Ea / (R·T))

    Exponential decay never goes negative — physically realistic.

    .. warning::
        Predictions at cold temperatures (≤ 10 °C) **underestimate**
        degradation from cold-specific mechanisms (lithium plating).

    Parameters
    ----------
    model : dict
        Arrhenius model from :func:`fit_arrhenius` (must contain ``A`` and ``Ea``).
    temp_c : float
        Operating temperature (°C).
    n_cycles : float
        Number of equivalent full discharge cycles.

    Returns
    -------
    float
        Predicted capacity retention (%), clamped to [0, 100].
    """
    t_k = temp_c + _KELVIN_OFFSET
    fade_rate = model["A"] * np.exp(-model["Ea"] / (_R * t_k))
    retention = 100.0 * np.exp(-fade_rate * n_cycles)
    return float(np.clip(retention, 0.0, 100.0))


def evaluate_soh(
    model: dict[str, Any],
    fade_df: pd.DataFrame,
) -> dict[str, Any]:
    """Evaluate the Arrhenius model.

    * **Warm regimes** (≥ 15 °C): R² and RMSE on regime-level fade rates.
    * **Cold regimes** (< 15 °C): RMSE only (model explicitly doesn't capture
      lithium plating).
    * **Leave-one-cell-out** (LOCO) on warm cells: for each warm cell, fit A
      from all other warm cells, predict the held-out cell's fade rate.

    Parameters
    ----------
    model : dict
        From :func:`fit_arrhenius`.
    fade_df : pd.DataFrame
        From :func:`compute_fade_rates`.

    Returns
    -------
    dict
        Keys: ``R²``, ``RMSE_fade_rate``, ``cold_rmse``, ``loco_results``.
    """
    df = fade_df[fade_df["fade_rate_per_cycle"] > 0].copy()
    min_temp = model.get("min_temp_c", 15.0)
    min_tk = (min_temp if min_temp is not None else -np.inf) + _KELVIN_OFFSET

    warm = df[df["temp_K"] >= min_tk]
    cold = df[df["temp_K"] < min_tk]

    # ── Warm-regime R² + RMSE (regime-level) ───────────────────────────
    r_sq = 0.0
    rmse = 0.0
    if len(warm) > 0:
        regime = regime_fade_rates(warm)
        pred_fade = np.array([
            model["A"] * np.exp(-model["Ea"] / (_R * row["temp_K"]))
            for _, row in regime.iterrows()
        ])
        actual_fade = regime["fade_rate_per_cycle"].values
        ss_res = np.sum((actual_fade - pred_fade) ** 2)
        ss_tot = np.sum((actual_fade - actual_fade.mean()) ** 2)
        r_sq = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
        rmse = float(np.sqrt(np.mean((actual_fade - pred_fade) ** 2)))

    # ── Cold-regime RMSE only (no R² — model doesn't capture lithium plating) ─
    cold_rmse = None
    if len(cold) > 0:
        regime_cold = regime_fade_rates(cold)
        pred_cold = np.array([
            model["A"] * np.exp(-model["Ea"] / (_R * row["temp_K"]))
            for _, row in regime_cold.iterrows()
        ])
        cold_rmse = float(np.sqrt(np.mean(
            (regime_cold["fade_rate_per_cycle"].values - pred_cold) ** 2
        )))

    # ── Leave-one-cell-out (LOCO) on warm cells ─────────────────────────
    loco_results: list[dict[str, Any]] = []
    if len(warm) >= 3:
        warm_cells = warm["cell_id"].unique()
        for held_out in warm_cells:
            train = warm[warm["cell_id"] != held_out]
            test = warm[warm["cell_id"] == held_out]
            if len(train) < 2 or len(test) == 0:
                continue

            # Fit A from training cells (Ea fixed)
            ln_a_train = np.log(train["fade_rate_per_cycle"].values) + \
                          _EA_FIXED / (_R * train["temp_K"].values)
            a_loco = float(np.exp(np.mean(ln_a_train)))

            test_k = test["fade_rate_per_cycle"].values
            test_tk = test["temp_K"].values
            pred_k = a_loco * np.exp(-_EA_FIXED / (_R * test_tk))
            loco_rmse = float(np.sqrt(np.mean((test_k - pred_k) ** 2)))

            loco_results.append({
                "held_out_cell": str(held_out),
                "n_train": len(train),
                "n_test": len(test),
                "RMSE_fade_rate": loco_rmse,
            })

    return {
        "R²": r_sq,
        "RMSE_fade_rate": rmse,
        "cold_rmse": cold_rmse,
        "loco_results": loco_results,
        "loco_mean_rmse": (
            float(np.mean([r["RMSE_fade_rate"] for r in loco_results]))
            if loco_results
            else None
        ),
    }


def save_soh_model(
    model: dict[str, Any],
    eval_results: dict[str, Any],
    path: str | Path = "data/processed/soh_model.pkl",
) -> None:
    """Save the Arrhenius SoH model and evaluation results.

    Parameters
    ----------
    model : dict
        From :func:`fit_arrhenius`.
    eval_results : dict
        From :func:`evaluate_soh`.
    path : str | Path
        Output file path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {"model": model, "evaluation": eval_results}
    with open(path, "wb") as f:
        pickle.dump(bundle, f)
