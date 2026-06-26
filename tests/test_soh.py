"""Tests for SoH Arrhenius degradation model.

All synthetic — no real NASA data required.
Tests exponential decay model with literature-fixed Ea.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from uhi_battery.models.soh import (
    _EA_FIXED,
    _KELVIN_OFFSET,
    _R,
    NASA_REGIMES,
    compute_fade_rates,
    evaluate_soh,
    filter_nasa_outliers,
    fit_arrhenius,
    predict_soh,
    regime_fade_rates,
)

# ── Synthetic data builders ─────────────────────────────────────────────────


def _synthetic_nasa_df(
    n_cells: int = 10,
    cycles_per_cell: int = 100,
    temp_c: float = 25.0,
    fade_rate: float = 0.0003,
    rng_seed: int = 42,
    cell_id_prefix: str = "B",
) -> pd.DataFrame:
    """Build synthetic NASA-like DataFrame with **exponential** fade per cell.

    retention = 100 * exp(-fade_rate * cycles) + noise
    """
    rng = np.random.default_rng(rng_seed)
    records = []
    for c in range(n_cells):
        cell_id = f"{cell_id_prefix}{c:04d}"
        for cyc in range(1, cycles_per_cell + 1):
            retention = 100.0 * np.exp(-fade_rate * cyc) + rng.normal(0, 0.1)
            records.append(
                {
                    "cell_id": cell_id,
                    "temp_C": temp_c + rng.normal(0, 0.5),
                    "cycles": cyc,
                    "capacity_Ah": 2.0 * retention / 100.0,
                    "capacity_retention_pct": retention,
                    "source": "NASA_PCoE",
                }
            )
    return pd.DataFrame(records)


def _synthetic_fade_df(
    temps_c: list[float] | None = None,
    ea_true: float = 45000.0,
    a_true: float = 3e4,
    noise: float = 0.05,
    rng_seed: int = 42,
) -> pd.DataFrame:
    """Build synthetic fade-rate data following true Arrhenius kinetics.

    fade_rate = A_true * exp(-Ea_true / (R*T)) * (1 + noise)

    With A=3e4, Ea=45 kJ/mol → ~0.00075/cycle at 25°C, ~0.0012 at 45°C.
    SoH after 500 cycles: ~68% at 25°C, ~55% at 45°C (plausible).
    """
    if temps_c is None:
        temps_c = [5.0, 15.0, 25.0, 35.0, 45.0]

    rng = np.random.default_rng(rng_seed)
    records = []
    for i, tc in enumerate(temps_c):
        tk = tc + _KELVIN_OFFSET
        true_fr = a_true * np.exp(-ea_true / (_R * tk))
        observed_fr = true_fr * (1.0 + rng.normal(0, noise))
        if observed_fr <= 0:
            observed_fr = true_fr * 1e-6
        records.append(
            {
                "cell_id": f"SYNTH{i:03d}",
                "regime_C": float(tc),
                "temp_C": tc,
                "temp_K": tk,
                "fade_rate_per_cycle": observed_fr,
                "n_cycles": 200,
                "final_retention": max(0.0, 100.0 * np.exp(-observed_fr * 200)),
                "r_sq": 0.95,
            }
        )
    return pd.DataFrame(records)


# ── Tests ───────────────────────────────────────────────────────────────────


class TestFilterNasaOutliers:
    """Data cleaning."""

    def test_drops_negative_retention(self) -> None:
        df = _synthetic_nasa_df(n_cells=3, cycles_per_cell=50)
        df.loc[0, "capacity_retention_pct"] = -5.0
        df.loc[1, "capacity_retention_pct"] = 120.0
        cleaned = filter_nasa_outliers(df)
        assert len(cleaned) < len(df)
        assert (cleaned["capacity_retention_pct"] >= 0).all()
        assert (cleaned["capacity_retention_pct"] <= 100).all()

    def test_drops_over_100_retention(self) -> None:
        df = _synthetic_nasa_df(n_cells=2, cycles_per_cell=30)
        df["capacity_retention_pct"] = 110.0
        cleaned = filter_nasa_outliers(df)
        assert len(cleaned) == 0

    def test_keeps_valid_retention(self) -> None:
        df = _synthetic_nasa_df(n_cells=5, cycles_per_cell=40, fade_rate=0.0005)
        cleaned = filter_nasa_outliers(df)
        assert len(cleaned) > 0
        assert (cleaned["capacity_retention_pct"].between(0, 100)).all()

    def test_drops_cells_with_few_cycles(self) -> None:
        df = _synthetic_nasa_df(n_cells=3, cycles_per_cell=5, fade_rate=0.001)
        cleaned = filter_nasa_outliers(df, min_cycles=10)
        assert len(cleaned) == 0


class TestComputeFadeRates:
    """Exponential fade rate computation."""

    def test_returns_positive_fade_rates(self) -> None:
        df = _synthetic_nasa_df(n_cells=10, cycles_per_cell=100, fade_rate=0.0003)
        fade_df = compute_fade_rates(df)
        assert len(fade_df) == 10
        assert (fade_df["fade_rate_per_cycle"] > 0).all()

    def test_faster_fade_at_higher_temp(self) -> None:
        df_cold = _synthetic_nasa_df(
            n_cells=5, cycles_per_cell=100, temp_c=5.0,
            fade_rate=0.0002, rng_seed=1, cell_id_prefix="C",
        )
        df_hot = _synthetic_nasa_df(
            n_cells=5, cycles_per_cell=100, temp_c=45.0,
            fade_rate=0.0008, rng_seed=2, cell_id_prefix="H",
        )
        df = pd.concat([df_cold, df_hot], ignore_index=True)
        fade_df = compute_fade_rates(df)
        cold_k = fade_df[fade_df["temp_C"] < 20]["fade_rate_per_cycle"].mean()
        hot_k = fade_df[fade_df["temp_C"] > 30]["fade_rate_per_cycle"].mean()
        assert hot_k > cold_k

    def test_columns_present(self) -> None:
        df = _synthetic_nasa_df()
        fade_df = compute_fade_rates(df)
        for col in ["cell_id", "regime_C", "temp_C", "temp_K",
                      "fade_rate_per_cycle", "n_cycles", "final_retention"]:
            assert col in fade_df.columns

    def test_exponential_fit_matches_generator(self) -> None:
        """Fitted exponential k should approximately match the true fade_rate."""
        true_k = 0.0005
        df = _synthetic_nasa_df(
            n_cells=1, cycles_per_cell=200, fade_rate=true_k, rng_seed=99,
        )
        fade_df = compute_fade_rates(df)
        est_k = fade_df["fade_rate_per_cycle"].iloc[0]
        rel_err = abs(est_k - true_k) / true_k
        assert rel_err < 0.20, f"k error {rel_err*100:.1f}%"


class TestRegimeFadeRates:
    """Regime-level averaging."""

    def test_reduces_to_regimes(self) -> None:
        fade_df = _synthetic_fade_df(temps_c=[5.0, 6.0, 24.0, 25.0, 44.0, 45.0])
        regime = regime_fade_rates(fade_df)
        assert len(regime) < len(fade_df)
        assert "regime_C" in regime.columns
        assert "n_cells" in regime.columns


class TestFitArrhenius:
    """Arrhenius with literature-fixed Ea."""

    def test_ea_is_fixed(self) -> None:
        fade_df = _synthetic_fade_df()
        model = fit_arrhenius(fade_df, min_temp_c=None)
        assert model["Ea"] == _EA_FIXED

    def test_recovers_a_approximately(self) -> None:
        a_true = 3e4
        fade_df = _synthetic_fade_df(a_true=a_true, noise=0.005)
        model = fit_arrhenius(fade_df, min_temp_c=None)
        # A should be within ~10% of true (only 1 param to estimate)
        rel_err = abs(model["A"] - a_true) / a_true
        assert rel_err < 0.15, f"A error {rel_err*100:.1f}%"

    def test_returns_r_squared(self) -> None:
        fade_df = _synthetic_fade_df(noise=0.01)
        model = fit_arrhenius(fade_df, min_temp_c=None)
        assert model["R²"] > 0.95, f"R²={model['R²']:.4f}"

    def test_returns_ci(self) -> None:
        fade_df = _synthetic_fade_df()
        model = fit_arrhenius(fade_df, min_temp_c=None)
        assert model["ci_A"] > 0

    def test_rejects_insufficient_data(self) -> None:
        fade_df = _synthetic_fade_df(temps_c=[25.0], noise=0.0)
        fade_df["fade_rate_per_cycle"] = 0.0
        with pytest.raises(ValueError, match="≥2 cells"):
            fit_arrhenius(fade_df, min_temp_c=None)

    def test_cold_filter_works(self) -> None:
        """With min_temp_c=15, cold cells are excluded."""
        fade_df = _synthetic_fade_df(temps_c=[5.0, 25.0, 45.0], noise=0.0)
        model = fit_arrhenius(fade_df, min_temp_c=15.0)
        assert model["n_cells"] == 2  # only 25 and 45


class TestPredictSoH:
    """SoH prediction with exponential decay."""

    @pytest.fixture
    def model(self) -> dict:
        fade_df = _synthetic_fade_df(noise=0.0)
        return fit_arrhenius(fade_df, min_temp_c=None)

    def test_retention_decreases_with_cycles(self, model: dict) -> None:
        soh_100 = predict_soh(model, 25.0, 100)
        soh_500 = predict_soh(model, 25.0, 500)
        assert soh_500 < soh_100

    def test_higher_temp_faster_degradation(self, model: dict) -> None:
        soh_cold = predict_soh(model, 5.0, 500)
        soh_hot = predict_soh(model, 45.0, 500)
        assert soh_hot < soh_cold, (
            f"{soh_hot:.1f}% (45°C) should be < {soh_cold:.1f}% (5°C)"
        )

    def test_retention_clamped_0_100(self, model: dict) -> None:
        soh = predict_soh(model, 100.0, 100000)
        assert 0.0 <= soh <= 100.0

    def test_fresh_cell_returns_100(self, model: dict) -> None:
        assert predict_soh(model, 25.0, 0) == 100.0

    def test_never_negative(self, model: dict) -> None:
        """Exponential decay asymptotically approaches 0, never goes negative."""
        soh = predict_soh(model, 80.0, 1_000_000)
        assert soh >= 0.0

    def test_hot_retention_above_zero(self, model: dict) -> None:
        """At 45°C, 500 cycles should have reasonable retention (> 20%)."""
        soh = predict_soh(model, 45.0, 500)
        assert soh > 20.0, f"SoH at 45°C/500cyc = {soh:.1f}% — too low"


class TestEvaluateSoH:
    """Model evaluation with warm/cold split and LOCO."""

    def test_returns_metrics_dict(self) -> None:
        fade_df = _synthetic_fade_df(noise=0.02)
        model = fit_arrhenius(fade_df, min_temp_c=None)
        result = evaluate_soh(model, fade_df)
        assert "R²" in result
        assert "RMSE_fade_rate" in result
        assert "loco_results" in result

    def test_loco_has_valid_metrics(self) -> None:
        # Many data points for LOCO
        temps = [25.0, 25.0, 26.0, 26.0, 27.0, 27.0,
                 35.0, 35.0, 36.0, 36.0, 45.0, 45.0]
        fade_df = _synthetic_fade_df(temps_c=temps, noise=0.02)
        model = fit_arrhenius(fade_df, min_temp_c=15.0)
        result = evaluate_soh(model, fade_df)
        assert len(result["loco_results"]) >= 1
        for r in result["loco_results"]:
            assert r["RMSE_fade_rate"] >= 0
            assert r["n_train"] >= r["n_test"]

    def test_cold_rmse_reported(self) -> None:
        temps = [5.0, 25.0, 45.0]
        fade_df = _synthetic_fade_df(temps_c=temps, noise=0.0)
        model = fit_arrhenius(fade_df, min_temp_c=15.0)
        result = evaluate_soh(model, fade_df)
        assert result["cold_rmse"] is not None
        assert result["cold_rmse"] >= 0


class TestNasaRegimes:
    """NASA regime constants."""

    def test_five_regimes(self) -> None:
        assert len(NASA_REGIMES) == 5
        assert 5.0 in NASA_REGIMES
        assert 44.0 in NASA_REGIMES


class TestArrheniusConstants:
    """Verify physical constants."""

    def test_r_gas_constant(self) -> None:
        assert abs(_R - 8.314) < 0.001

    def test_kelvin_offset(self) -> None:
        assert abs(_KELVIN_OFFSET - 273.15) < 0.001

    def test_ea_fixed(self) -> None:
        assert _EA_FIXED == 45000.0
