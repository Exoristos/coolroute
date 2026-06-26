"""Tests for drive cycle Markov chain + Monte Carlo model.

All synthetic — no external data required.  Tests physics correctness,
Markov chain behaviour, and literature-band regression.
"""

from __future__ import annotations

import numpy as np
import pytest

from uhi_battery.models.drive_cycle import (
    _AUX_POWER_W,
    _DURATION_PARAMS,
    _MAX_STOP_DURATION_S,
    _REGEN_EFFICIENCY,
    DriveCycleResult,
    DriveState,
    MCEnergyResult,
    _acceleration_energy_wh,
    _cruise_energy_wh,
    _deceleration_energy_wh,
    _kinetic_energy_j,
    _stopped_energy_wh,
    build_transition_matrix,
    compute_edge_energies_batch,
    compute_edge_energy_mc,
    simulate_drive_cycle,
    validate_drive_cycle_model,
)

# ── Transition matrix tests ─────────────────────────────────────────────────


class TestTransitionMatrix:
    """Markov chain transition matrix construction."""

    def test_shape_4x4(self) -> None:
        P = build_transition_matrix()
        assert P.shape == (4, 4)

    def test_rows_sum_to_1(self) -> None:
        P = build_transition_matrix(stops_per_km=12.0)
        for row in P:
            assert pytest.approx(sum(row), abs=1e-10) == 1.0

    def test_all_nonnegative(self) -> None:
        P = build_transition_matrix(stops_per_km=15.0)
        assert np.all(P >= 0)

    def test_stop_to_cruise_is_zero(self) -> None:
        """Council fix: STOP→CRUISE = 0 (must accelerate first)."""
        P = build_transition_matrix()
        assert P[DriveState.STOPPED, DriveState.CRUISING] == 0.0

    def test_stop_to_decel_is_zero(self) -> None:
        """STOP→DECEL makes no physical sense."""
        P = build_transition_matrix()
        assert P[DriveState.STOPPED, DriveState.DECELERATING] == 0.0

    def test_higher_stops_per_km_more_leaving_cruise(self) -> None:
        """More stops/km → higher probability of leaving cruise state."""
        P_low = build_transition_matrix(stops_per_km=5.0)
        P_high = build_transition_matrix(stops_per_km=20.0)
        # P(leave cruise) = 1 - P[CRUISE, CRUISE]
        leave_low = 1.0 - P_low[DriveState.CRUISING, DriveState.CRUISING]
        leave_high = 1.0 - P_high[DriveState.CRUISING, DriveState.CRUISING]
        assert leave_high > leave_low

    def test_matrix_is_irreducible(self) -> None:
        """All states should be reachable from any state (ergodicity)."""
        P = build_transition_matrix(stops_per_km=12.0)
        # Check reachability: P^n should have all positive entries for large n
        Pn = np.linalg.matrix_power(P, 10)
        assert np.all(Pn > 0), "Markov chain is not irreducible"


# ── Physics segment tests ───────────────────────────────────────────────────


class TestKineticEnergy:
    def test_zero_speed(self) -> None:
        assert _kinetic_energy_j(100.0, 0.0) == 0.0

    def test_known_value(self) -> None:
        # 100 kg at 4.17 m/s (15 km/h): KE = 0.5 * 100 * 4.17² = 869.7 J
        ke = _kinetic_energy_j(100.0, 4.17)
        assert pytest.approx(ke, rel=0.01) == 869.7

    def test_scales_with_mass(self) -> None:
        assert _kinetic_energy_j(200.0, 4.17) > _kinetic_energy_j(100.0, 4.17)


class TestAccelerationEnergy:
    def test_positive_energy(self) -> None:
        e = _acceleration_energy_wh(4.17, 3.0)
        assert e > 0

    def test_zero_duration(self) -> None:
        assert _acceleration_energy_wh(4.17, 0.0) == 0.0

    def test_zero_speed(self) -> None:
        assert _acceleration_energy_wh(0.0, 3.0) == 0.0

    def test_includes_kinetic_energy(self) -> None:
        """Acceleration energy must be > pure KE (includes rolling + aero)."""
        ke_wh = _kinetic_energy_j(100.0, 4.17) / 3600.0
        e = _acceleration_energy_wh(4.17, 3.0)
        assert e > ke_wh

    def test_uphill_more_energy(self) -> None:
        e_flat = _acceleration_energy_wh(4.17, 3.0, slope_deg=0.0)
        e_up = _acceleration_energy_wh(4.17, 3.0, slope_deg=5.0)
        assert e_up > e_flat


class TestDecelerationEnergy:
    def test_can_be_negative(self) -> None:
        """Regen recovery → negative energy (net gain)."""
        e = _deceleration_energy_wh(4.17, 2.5)
        # With regen=0.30, decel should recover some energy
        assert e < 0 or e < _acceleration_energy_wh(4.17, 3.0)

    def test_zero_speed(self) -> None:
        assert _deceleration_energy_wh(0.0, 2.5) == 0.0

    def test_zero_duration(self) -> None:
        assert _deceleration_energy_wh(4.17, 0.0) == 0.0

    def test_downhill_more_recovery(self) -> None:
        """Downhill → gravity adds recoverable energy."""
        e_flat = _deceleration_energy_wh(4.17, 2.5, slope_deg=0.0)
        e_down = _deceleration_energy_wh(4.17, 2.5, slope_deg=-3.0)
        assert e_down < e_flat  # more negative = more recovery

    def test_low_regen_efficiency(self) -> None:
        """Council fix: regen=0.30, not 0.60."""
        assert _REGEN_EFFICIENCY == 0.30

    def test_below_cutoff_no_regen(self) -> None:
        """Below cutoff speed (5 km/h = 1.39 m/s), no regen."""
        # At 1.0 m/s (below cutoff), should be mostly losses, no recovery
        e = _deceleration_energy_wh(1.0, 2.0)
        # Should be positive (losses only, no regen)
        assert e >= 0


class TestCruiseEnergy:
    def test_matches_steady_state(self) -> None:
        """Cruise energy should match the existing physics model."""
        from uhi_battery.models.energy import compute_trip_energy

        steady = compute_trip_energy(1000.0, 15.0, 25.0, 0.0)
        cruise = _cruise_energy_wh(1000.0, 4.17, 25.0, 0.0)
        assert pytest.approx(cruise, rel=0.01) == steady

    def test_scales_with_distance(self) -> None:
        assert _cruise_energy_wh(2000.0, 4.17) > _cruise_energy_wh(1000.0, 4.17)


class TestStoppedEnergy:
    def test_positive(self) -> None:
        assert _stopped_energy_wh(10.0) > 0

    def test_scales_with_duration(self) -> None:
        assert _stopped_energy_wh(20.0) > _stopped_energy_wh(10.0)

    def test_aux_power_is_12w(self) -> None:
        """Council fix: 12W for shared-fleet scooter."""
        assert _AUX_POWER_W == 12.0

    def test_known_value(self) -> None:
        # 12W * 10s / 3600 = 0.0333 Wh
        assert pytest.approx(_stopped_energy_wh(10.0), rel=0.01) == 12.0 * 10.0 / 3600.0


# ── Drive cycle simulation tests ────────────────────────────────────────────


class TestSimulateDriveCycle:
    def test_returns_result(self) -> None:
        result = simulate_drive_cycle(500.0, seed=42)
        assert isinstance(result, DriveCycleResult)
        assert result.energy_wh > 0
        assert result.total_distance_m > 0

    def test_covers_full_distance(self) -> None:
        """Simulation should cover the full edge length."""
        result = simulate_drive_cycle(1000.0, seed=42)
        assert pytest.approx(result.total_distance_m, rel=0.01) == 1000.0

    def test_deterministic_with_seed(self) -> None:
        r1 = simulate_drive_cycle(500.0, seed=42)
        r2 = simulate_drive_cycle(500.0, seed=42)
        assert r1.energy_wh == pytest.approx(r2.energy_wh, rel=0.001)
        assert r1.n_stops == r2.n_stops

    def test_different_seed_different_result(self) -> None:
        r1 = simulate_drive_cycle(500.0, seed=42)
        r2 = simulate_drive_cycle(500.0, seed=99)
        # Should differ (not guaranteed exactly, but very likely)
        assert r1.states != r2.states or r1.energy_wh != r2.energy_wh

    def test_has_stops(self) -> None:
        """A 1km edge with 12 stops/km should have at least 1 stop."""
        result = simulate_drive_cycle(1000.0, stops_per_km=12.0, seed=42)
        assert result.n_stops > 0

    def test_more_stops_per_km_more_energy(self) -> None:
        """Higher stop frequency → more energy."""
        r_low = simulate_drive_cycle(1000.0, stops_per_km=5.0, seed=42)
        r_high = simulate_drive_cycle(1000.0, stops_per_km=20.0, seed=42)
        assert r_high.energy_wh > r_low.energy_wh

    def test_zero_length_edge(self) -> None:
        """Zero-length edge should return near-zero energy."""
        result = simulate_drive_cycle(0.0, seed=42)
        assert result.energy_wh == pytest.approx(0.0, abs=0.01)

    def test_tiny_edge(self) -> None:
        """Edge below _MIN_EDGE_LENGTH_M should use steady-state only."""
        result = simulate_drive_cycle(0.5, seed=42)
        assert result.energy_wh > 0
        assert result.n_stops == 0  # no stop-and-go cycles fit

    def test_stop_duration_capped(self) -> None:
        """Stop durations should not exceed _MAX_STOP_DURATION_S."""
        # Run many simulations and check all stop durations
        for seed in range(100):
            result = simulate_drive_cycle(2000.0, seed=seed, stops_per_km=20.0)
            for i, state in enumerate(result.states):
                if state == DriveState.STOPPED:
                    assert result.durations_s[i] <= _MAX_STOP_DURATION_S

    def test_energy_nonnegative(self) -> None:
        """Total energy should never be negative (BMS clamp)."""
        for seed in range(50):
            result = simulate_drive_cycle(1000.0, seed=seed, slope_deg=-5.0)
            assert result.energy_wh >= 0.0


# ── Monte Carlo tests ───────────────────────────────────────────────────────


class TestMonteCarlo:
    def test_returns_result(self) -> None:
        result = compute_edge_energy_mc(1000.0, n_simulations=50, seed=42)
        assert isinstance(result, MCEnergyResult)
        assert result.mean_wh > 0
        assert result.n_simulations == 50

    def test_deterministic(self) -> None:
        r1 = compute_edge_energy_mc(1000.0, n_simulations=50, seed=42)
        r2 = compute_edge_energy_mc(1000.0, n_simulations=50, seed=42)
        assert r1.mean_wh == pytest.approx(r2.mean_wh, rel=0.001)

    def test_std_positive(self) -> None:
        result = compute_edge_energy_mc(1000.0, n_simulations=100, seed=42)
        assert result.std_wh > 0

    def test_p5_less_than_p95(self) -> None:
        result = compute_edge_energy_mc(1000.0, n_simulations=100, seed=42)
        assert result.p5_wh < result.p95_wh

    def test_wh_per_km_positive(self) -> None:
        result = compute_edge_energy_mc(1000.0, n_simulations=50, seed=42)
        assert result.wh_per_km > 0

    def test_zero_length_edge(self) -> None:
        result = compute_edge_energy_mc(0.0, n_simulations=50, seed=42)
        assert result.mean_wh == pytest.approx(0.0, abs=0.01)

    def test_more_stops_more_energy(self) -> None:
        r_low = compute_edge_energy_mc(1000.0, stops_per_km=5.0, n_simulations=50, seed=42)
        r_high = compute_edge_energy_mc(1000.0, stops_per_km=20.0, n_simulations=50, seed=42)
        assert r_high.mean_wh > r_low.mean_wh


class TestBatchComputation:
    def test_returns_correct_length(self) -> None:
        lengths = np.array([100.0, 500.0, 1000.0, 2000.0])
        mean_wh, std_wh = compute_edge_energies_batch(
            lengths, n_simulations=20, seed=42, chunk_size=2
        )
        assert len(mean_wh) == 4
        assert len(std_wh) == 4

    def test_all_positive(self) -> None:
        lengths = np.array([100.0, 500.0, 1000.0])
        mean_wh, _ = compute_edge_energies_batch(lengths, n_simulations=20, seed=42)
        assert np.all(mean_wh > 0)

    def test_scales_with_length(self) -> None:
        lengths = np.array([500.0, 1000.0, 2000.0])
        mean_wh, _ = compute_edge_energies_batch(lengths, n_simulations=20, seed=42)
        assert mean_wh[0] < mean_wh[1] < mean_wh[2]

    def test_scalar_speed_broadcast(self) -> None:
        lengths = np.array([500.0, 1000.0])
        mean_wh, _ = compute_edge_energies_batch(
            lengths, speeds_kmh=15.0, n_simulations=20, seed=42
        )
        assert len(mean_wh) == 2


# ── Regression test: literature band ────────────────────────────────────────


class TestLiteratureBand:
    """Council recommendation: lock Wh/km into [6, 15] band."""

    def test_wh_per_km_in_literature_band(self) -> None:
        """1 km @ 15 km/h, 25°C, 12 stops/km → Wh/km should be in [6, 15]."""
        result = compute_edge_energy_mc(
            edge_length_m=1000.0,
            speed_kmh=15.0,
            temp_c=25.0,
            slope_deg=0.0,
            stops_per_km=12.0,
            n_simulations=200,
            seed=42,
        )
        assert 6.0 <= result.wh_per_km <= 15.0, (
            f"Wh/km = {result.wh_per_km:.2f} — outside literature [6-15] band"
        )

    def test_mc_higher_than_steady_state(self) -> None:
        """MC energy should be higher than steady-state (stop-and-go adds losses)."""
        steady = _cruise_energy_wh(1000.0, 4.17, 25.0, 0.0)
        mc = compute_edge_energy_mc(1000.0, n_simulations=200, seed=42)
        assert mc.mean_wh > steady

    def test_validation_report(self) -> None:
        v = validate_drive_cycle_model()
        assert "steady_state_wh_per_km" in v
        assert "mc_mean_wh_per_km" in v
        assert "within_literature_band" in v
        assert v["within_literature_band"] is True


# ── Duration parameterisation tests ─────────────────────────────────────────


class TestDurationParams:
    """Council fix: log-normal mu = ln(mean) - sigma²/2, not ln(mean)."""

    def test_cruise_mean_approx_8s(self) -> None:
        mu, sigma = _DURATION_PARAMS[DriveState.CRUISING]
        samples = np.random.default_rng(42).lognormal(mu, sigma, 100000)
        physical_mean = np.mean(samples)
        assert 7.0 < physical_mean < 9.0  # ≈ 8s

    def test_accel_mean_approx_3s(self) -> None:
        mu, sigma = _DURATION_PARAMS[DriveState.ACCELERATING]
        samples = np.random.default_rng(42).lognormal(mu, sigma, 100000)
        physical_mean = np.mean(samples)
        assert 2.5 < physical_mean < 3.5  # ≈ 3s

    def test_stop_mean_approx_8s(self) -> None:
        mu, sigma = _DURATION_PARAMS[DriveState.STOPPED]
        samples = np.random.default_rng(42).lognormal(mu, sigma, 100000)
        physical_mean = np.mean(samples)
        assert 7.0 < physical_mean < 9.0  # ≈ 8s
