"""Tests for metrics module.

All synthetic — no real files needed.
"""

from __future__ import annotations

import pytest

from uhi_battery.metrics import (
    compute_energy_saving,
    compute_soh_impact,
    data_quality_summary,
    energy_model_decomposition,
    model_quality_summary,
    pareto_dominance_test,
    sensitivity_analysis,
)

# ── Synthetic frontiers ─────────────────────────────────────────────────────


def _make_frontier(
    energies: list[float],
    obj2s: list[float],
    obj2_name: str = "degree_hours",
) -> list[dict]:
    return [
        {"energy_wh": e, "obj2_value": o, "obj2_name": obj2_name}
        for e, o in zip(energies, obj2s, strict=True)
    ]


def _make_soh_model() -> dict:
    return {"Ea": 45000.0, "A": 3e4}


# ── Tests ───────────────────────────────────────────────────────────────────


class TestComputeEnergySaving:
    """Energy saving metric."""

    def test_positive_saving(self) -> None:
        f = _make_frontier([100.0, 90.0], [10.0, 9.0])
        result = compute_energy_saving(f, 110.0)
        assert result["saving_pct"] > 0
        assert result["pareto_wh"] == 90.0

    def test_zero_saving_degenerate(self) -> None:
        f = _make_frontier([100.0], [10.0])
        result = compute_energy_saving(f, 100.0)
        assert result["saving_pct"] == 0.0
        assert "degenerate" in result.get("note", "")

    def test_empty_frontier(self) -> None:
        result = compute_energy_saving([], 100.0)
        assert result["saving_pct"] == 0.0
        assert "empty" in result.get("note", "")


class TestComputeSoHImpact:
    """SoH impact metric."""

    def test_returns_dict(self) -> None:
        f = _make_frontier([100.0], [5.0])
        model = _make_soh_model()
        result = compute_soh_impact(f, 10.0, model)
        assert "soh_diff_pct" in result
        assert "note" in result

    def test_empty_frontier(self) -> None:
        model = _make_soh_model()
        result = compute_soh_impact([], 10.0, model)
        assert "empty" in result.get("note", "")


class TestEnergyDecomposition:
    """Energy decomposition into components."""

    def test_components_sum_to_total(self) -> None:
        d = energy_model_decomposition(
            distance_m=3000.0, speed_kmh=15.0, temp_c=25.0, slope_deg=0.0
        )
        comp_sum = d["rolling_wh"] + d["aero_wh"] + d["grade_wh"]
        assert comp_sum == pytest.approx(d["total_wh"], rel=0.02)

    def test_grade_zero_on_flat(self) -> None:
        d = energy_model_decomposition(slope_deg=0.0)
        assert d["grade_wh"] == 0.0

    def test_grade_positive_on_uphill(self) -> None:
        d = energy_model_decomposition(slope_deg=3.0)
        assert d["grade_wh"] > 0

    def test_cold_derating_positive(self) -> None:
        d = energy_model_decomposition(temp_c=5.0)
        # Cold → less efficient → more energy needed → positive derating
        # derating_pct = (E_cold - E_ref) / E_ref * 100
        assert d["temp_derating_pct"] > 0

    def test_hot_derating_negative(self) -> None:
        d = energy_model_decomposition(temp_c=45.0)
        # Hot → more efficient → less energy → negative derating
        assert d["temp_derating_pct"] < 0

    def test_aero_increases_with_speed(self) -> None:
        d_slow = energy_model_decomposition(speed_kmh=10.0)
        d_fast = energy_model_decomposition(speed_kmh=25.0)
        assert d_fast["aero_wh"] > d_slow["aero_wh"]


class TestParetoDominance:
    """Pareto dominance verification."""

    def test_empty_frontier_passes(self) -> None:
        result = pareto_dominance_test([])
        assert result["passed"]

    def test_single_solution_passes(self) -> None:
        f = _make_frontier([100.0], [10.0])
        result = pareto_dominance_test(f)
        assert result["passed"]
        assert "trivial" in result.get("details", "")

    def test_non_dominated_passes(self) -> None:
        f = _make_frontier([100.0, 80.0], [10.0, 15.0])
        # (100,10) vs (80,15): neither dominates (energy better in one, obj2 in other)
        result = pareto_dominance_test(f)
        assert result["passed"]

    def test_dominated_fails(self) -> None:
        f = _make_frontier([100.0, 90.0], [10.0, 9.0])
        # (90,9) dominates (100,10) on both objectives
        result = pareto_dominance_test(f)
        assert not result["passed"]
        assert result["n_violations"] > 0

    def test_identical_solutions_not_dominated(self) -> None:
        """Identical solutions do not dominate each other (strict inequality required)."""
        f = _make_frontier([100.0, 100.0], [10.0, 10.0])
        result = pareto_dominance_test(f)
        assert result["passed"]  # neither strictly dominates the other
        assert result["n_solutions"] == 2


class TestSensitivityAnalysis:
    """Sensitivity analysis."""

    def test_perturbed_energy(self) -> None:
        result = sensitivity_analysis(
            frontier=[],
            baseline_energy=100.0,
            energy_perturbed=[100.0, 102.0, 98.0, 105.0],
        )
        assert result["n_tests"] == 4
        assert result["mean_energy_diff_wh"] > 0
        assert 0.0 <= result["route_change_rate"] <= 1.0

    def test_no_change(self) -> None:
        result = sensitivity_analysis(
            frontier=[],
            baseline_energy=100.0,
            energy_perturbed=[100.0, 100.0, 100.0],
        )
        assert result["route_change_rate"] == 0.0

    def test_all_changed(self) -> None:
        result = sensitivity_analysis(
            frontier=[],
            baseline_energy=100.0,
            energy_perturbed=[95.0, 105.0, 90.0, 110.0],
        )
        assert result["route_change_rate"] > 0.5  # most changed


class TestDataQualitySummary:
    """Data quality report."""

    def test_defaults(self) -> None:
        dq = data_quality_summary()
        assert dq["lst"]["days"] == 177
        assert dq["nasa_battery"]["cells"] == 26

    def test_custom_values(self) -> None:
        dq = data_quality_summary(lst_days=100, nasa_cells=10, nasa_regimes=3,
                                  network_nodes=500, network_edges=1200)
        assert dq["lst"]["days"] == 100
        assert dq["network"]["nodes"] == 500


class TestModelQualitySummary:
    """Model quality report."""

    def test_defaults(self) -> None:
        mq = model_quality_summary()
        assert mq["soh"]["ea_kj_per_mol"] == 45.0
        assert mq["soh"]["approach"] == "exponential decay Arrhenius"

    def test_with_soh_data(self) -> None:
        soh_model = {"Ea": 45000.0, "A": 33871.0, "ci_A": 9989.0, "n_cells": 14}
        eval_results = {"cold_rmse": 0.0032}
        mq = model_quality_summary(soh_model=soh_model, eval_results=eval_results)
        assert mq["soh"]["a_pre_exponential"] == 33871.0
        assert mq["soh"]["cold_rmse"] == 0.0032
