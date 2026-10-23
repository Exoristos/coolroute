"""Tests for energy consumption model.

All synthetic — no real NASA data, no LST data, no network required.
Tests the vectorised physics model directly.
"""

from __future__ import annotations

import numpy as np
import pytest

from uhi_battery.models.energy import (
    _battery_efficiency,
    compute_energy_wh_per_km,
    compute_trip_energy,
    save_energy_model,
    validate_energy_model,
)


class TestBatteryEfficiency:
    """Unit tests for _battery_efficiency."""

    def test_at_reference_temp_returns_ref_eta(self) -> None:
        eta = _battery_efficiency(25.0)
        assert 0.84 <= eta <= 0.86

    def test_cold_reduces_efficiency(self) -> None:
        assert _battery_efficiency(5.0) < _battery_efficiency(25.0)

    def test_hot_increases_efficiency(self) -> None:
        assert _battery_efficiency(45.0) > _battery_efficiency(25.0)

    def test_clamped_to_min(self) -> None:
        assert _battery_efficiency(-40.0) >= 0.50

    def test_clamped_to_max(self) -> None:
        assert _battery_efficiency(100.0) <= 0.95

    def test_vectorised(self) -> None:
        temps = np.array([-40.0, 5.0, 25.0, 45.0, 100.0])
        eta = _battery_efficiency(temps)
        assert eta.shape == (5,)
        assert eta[0] <= eta[1] <= eta[2] <= eta[3]
        assert eta[2] == pytest.approx(0.85, abs=0.01)


# ── Known physics reference value ───────────────────────────────────────────
_REF_ENERGY_WH = compute_trip_energy(3000.0, 15.0, 25.0)


class TestComputeTripEnergy:
    """Physics-based energy computation — scalar inputs."""

    def test_returns_positive_energy(self) -> None:
        assert compute_trip_energy(3000.0, 15.0, 25.0) > 0

    def test_energy_matches_reference(self) -> None:
        e = compute_trip_energy(3000.0, 15.0, 25.0)
        assert e == pytest.approx(_REF_ENERGY_WH, rel=0.01)

    def test_longer_distance_more_energy(self) -> None:
        assert compute_trip_energy(5000.0, 15.0, 25.0) > compute_trip_energy(1000.0, 15.0, 25.0)

    def test_higher_speed_more_energy(self) -> None:
        assert compute_trip_energy(1000.0, 25.0, 25.0) > compute_trip_energy(1000.0, 10.0, 25.0)

    def test_cold_temp_more_energy(self) -> None:
        assert compute_trip_energy(3000.0, 15.0, 5.0) > compute_trip_energy(3000.0, 15.0, 25.0)

    def test_hot_temp_less_energy(self) -> None:
        assert compute_trip_energy(3000.0, 15.0, 45.0) < compute_trip_energy(3000.0, 15.0, 25.0)

    def test_default_temp(self) -> None:
        assert compute_trip_energy(3000.0, 15.0) > 0

    def test_custom_params(self) -> None:
        e = compute_trip_energy(3000.0, 15.0, 25.0, params={"m": 200.0})
        assert e > compute_trip_energy(3000.0, 15.0, 25.0)

    def test_energy_linear_in_distance(self) -> None:
        e1 = compute_trip_energy(1000.0, 15.0, 25.0)
        e3 = compute_trip_energy(3000.0, 15.0, 25.0)
        assert e3 == pytest.approx(3 * e1, rel=0.02)

    def test_uphill_more_energy(self) -> None:
        e_flat = compute_trip_energy(3000.0, 10.0, 25.0, slope_deg=0.0)
        e_up = compute_trip_energy(3000.0, 10.0, 25.0, slope_deg=3.0)
        assert e_up > e_flat * 1.5

    def test_downhill_less_energy(self) -> None:
        e_flat = compute_trip_energy(3000.0, 10.0, 25.0, slope_deg=0.0)
        e_down = compute_trip_energy(3000.0, 10.0, 25.0, slope_deg=-2.0)
        assert e_down < e_flat


class TestComputeTripEnergyVectorised:
    """Vectorised inputs — arrays instead of scalars."""

    def test_array_inputs_return_array(self) -> None:
        dists = np.array([1000.0, 3000.0, 5000.0])
        speeds = np.array([10.0, 15.0, 20.0])
        temps = np.array([5.0, 25.0, 45.0])
        e = compute_trip_energy(dists, speeds, temps)
        assert isinstance(e, np.ndarray)
        assert e.shape == (3,)
        assert np.all(e > 0)

    def test_broadcasting(self) -> None:
        """Scalar temp broadcasts across multiple trips."""
        dists = np.array([1000.0, 3000.0])
        e = compute_trip_energy(dists, 15.0, 25.0)
        assert e.shape == (2,)
        assert e[1] > e[0]

    def test_wh_per_km_vectorised(self) -> None:
        speeds = np.array([5.0, 15.0, 25.0])
        wh_km = compute_energy_wh_per_km(speeds, 25.0)
        assert wh_km.shape == (3,)
        assert wh_km[2] > wh_km[0]  # faster → more Wh/km

    def test_slope_vectorised(self) -> None:
        slopes = np.array([-5.0, 0.0, 5.0])
        e = compute_trip_energy(1000.0, 10.0, 25.0, slope_deg=slopes)
        assert e.shape == (3,)
        assert e[2] > e[1] > e[0]  # uphill > flat > downhill


class TestValidateEnergyModel:
    """Validation report."""

    def test_returns_dict(self) -> None:
        v = validate_energy_model()
        assert "points" in v
        assert "summary" in v
        assert len(v["points"]) == 3

    def test_reference_point(self) -> None:
        v = validate_energy_model()
        ref = v["points"][0]
        assert ref["label"].startswith("3km")
        assert ref["energy_Wh"] > 0
        assert ref["Wh_per_km"] > 0

    def test_save_load(self, tmp_path) -> None:
        import pickle

        p = tmp_path / "energy.pkl"
        save_energy_model(p)
        with open(p, "rb") as f:
            loaded = pickle.load(f)
        assert loaded["model_type"] == "physics"
        assert "params" in loaded
        assert loaded["params"]["m_kg"] == 100.0
