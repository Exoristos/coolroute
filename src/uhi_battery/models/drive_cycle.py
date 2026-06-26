"""Stop-and-go drive cycle simulation via Markov chain + Monte Carlo.

Adds realistic urban stop-and-go dynamics on top of the steady-state physics
energy model.  The steady-state model (:mod:`uhi_battery.models.energy`)
computes cruising energy from rolling + aero + grade forces.  This module
models the *additional* energy from:

* **Acceleration** — kinetic energy ½mv² plus rolling/aero during accel,
  with proper ∫v²dt integration (not v_avg²).
* **Deceleration** — regenerative braking recovery (η_regen=0.30, e-scooter
  realistic, not EV-grade 0.60), with cutoff speed below which friction
  brakes engage.
* **Stopped** — auxiliary load (lights, controller, BMS, comms) at ~12 W.
* **Auxiliary load during ALL states** — global, not just stopped.

Architecture (Council decision):
    MC runs **once per edge** in ``assign_edge_attributes``, NOT inside the
    NSGA-II loop.  Energy is pre-cached on edges; NSGA-II reads cached values.
    Stochastic objectives break EA dominance comparisons.

Integration:
    E_edge_mc = E_edge_steady + Δ_stop_and_go

    The steady-state model is the calibration anchor (4.13 Wh/km reference).
    This module is a *correction layer* — it does not replace
    :func:`compute_trip_energy`.

Physics fixes (Council review):
    * Aero during accel: ∫₀^t_a v(t)² dt = a²·t_a³/3  (linear ramp)
    * Regen efficiency: 0.30 (e-scooter BLDC hub, not passenger EV)
    * Regen cutoff speed: 5 km/h (below → friction brakes)
    * η_accel = 0.75 (motor+controller at high torque/low speed)
    * η_cruise = 0.85 (baseline from energy.py)
    * Aux load: 12 W global (shared-fleet scooter: lights+controller+BMS+LTE)
    * Grade included in decel segment (downhill → gravity adds recoverable KE)
    * Negative energy clamped (can't over-recover into pack)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import numpy as np

from uhi_battery.models.energy import (
    _CDA,
    _CRR,
    _G,
    _MASS_KG,
    _RHO,
    _battery_efficiency,
    compute_trip_energy,
)

# ── Drive cycle states ─────────────────────────────────────────────────────


class DriveState(IntEnum):
    """Markov chain states for e-scooter drive cycle."""

    CRUISING = 0
    ACCELERATING = 1
    DECELERATING = 2
    STOPPED = 3


# ── Physics constants (drive-cycle specific) ────────────────────────────────

_REGEN_EFFICIENCY: float = 0.30  # e-scooter BLDC hub regen (literature 20-40%)
_REGEN_CUTOFF_SPEED_MS: float = 1.39  # 5 km/h — below this, friction brakes
_ETA_ACCEL: float = 0.75  # motor+controller at high torque / low speed
_AUX_POWER_W: float = 12.0  # lights + controller + BMS + LTE/GPS (shared fleet)
_MAX_STOP_DURATION_S: float = 120.0  # cap long red lights
_MIN_EDGE_LENGTH_M: float = 1.0  # below this, no stop-and-go cycles fit

# ── Duration distribution parameters (log-normal) ───────────────────────────
# Log-normal: numpy parameterises by log-space (mu, sigma).
# To get physical mean T: mu = ln(T) - sigma²/2
# Council fix: use correct parameterisation, not ln(T) directly.

_DURATION_PARAMS: dict[DriveState, tuple[float, float]] = {
    # (mu_log, sigma_log) — physical mean ≈ 8s cruise
    DriveState.CRUISING: (float(np.log(8.0) - 0.5**2 / 2), 0.5),
    # physical mean ≈ 3s accel
    DriveState.ACCELERATING: (float(np.log(3.0) - 0.4**2 / 2), 0.4),
    # physical mean ≈ 2.5s decel
    DriveState.DECELERATING: (float(np.log(2.5) - 0.4**2 / 2), 0.4),
    # physical mean ≈ 8s stop (capped at 120s)
    DriveState.STOPPED: (float(np.log(8.0) - 0.7**2 / 2), 0.7),
}


# ── Transition matrix ───────────────────────────────────────────────────────


def build_transition_matrix(
    stops_per_km: float = 12.0,
    cruise_speed_ms: float = 4.17,
) -> np.ndarray:
    """Build a 4×4 Markov transition matrix calibrated to target stop frequency.

    The matrix is derived from the desired ``stops_per_km`` rather than
    hard-coded.  At 15 km/h (4.17 m/s), ``stops_per_km=12`` → one stop every
    ~83 m → one stop every ~20 s of travel.

    Council fixes applied:
    * ACCEL→STOP = 0.05 (physically possible: obstacle)
    * DECEL→ACCEL = 0.05 (yellow-light go)
    * STOP→CRUISE = 0.00 (scooter leaving stop must accelerate first)
    * Stop time fraction target: 10-20% (not 31%)

    Parameters
    ----------
    stops_per_km : float
        Target stop frequency.  Istanbul Kadıköy centre: 12-18.
        Residential: 8-12.  Default 12.
    cruise_speed_ms : float
        Cruise speed in m/s.  Default 4.17 (15 km/h).

    Returns
    -------
    np.ndarray
        4×4 row-stochastic transition matrix.
        Order: [CRUISING, ACCELERATING, DECELERATING, STOPPED]
    """
    # Distance between stops → time between stops
    dist_between_stops = 1000.0 / max(stops_per_km, 0.5)  # metres
    time_between_stops = dist_between_stops / cruise_speed_ms  # seconds

    # Cruise duration ≈ time_between_stops - accel - decel - stop
    # With accel=3s, decel=2.5s, stop=8s → cruise ≈ time_between - 13.5
    cruise_duration = max(time_between_stops - 13.5, 2.0)

    # Transition probabilities derived from expected dwell times
    # P(leave cruise) ≈ 1 / (cruise_duration / state_duration)
    # Higher stops_per_km → shorter cruise → higher P(leave cruise)
    p_cruise_leave = min(1.0 / max(cruise_duration / 8.0, 1.0), 0.5)

    # Split leaving cruise into decel (signal/congestion) and accel (rare)
    p_cruise_to_decel = p_cruise_leave * 0.80
    p_cruise_to_accel = p_cruise_leave * 0.20
    p_cruise_to_stop = 0.02  # rare: sudden obstacle

    # Row: [CRUISE, ACCEL, DECEL, STOP]
    row_cruise = np.array([
        1.0 - p_cruise_to_decel - p_cruise_to_accel - p_cruise_to_stop,
        p_cruise_to_accel,
        p_cruise_to_decel,
        p_cruise_to_stop,
    ])

    # From ACCELERATING: mostly → cruise, small → decel, tiny → stop
    row_accel = np.array([0.85, 0.10, 0.05, 0.00])
    # Council fix: ACCEL→STOP = 0.05 (obstacle)
    row_accel = np.array([0.80, 0.10, 0.05, 0.05])

    # From DECELERATING: mostly → stop, some → cruise, small → accel
    # Council fix: DECEL→ACCEL = 0.05 (yellow-light go)
    row_decel = np.array([0.30, 0.05, 0.10, 0.55])

    # From STOPPED: must accelerate (can't instantly cruise)
    # Council fix: STOP→CRUISE = 0.00
    row_stop = np.array([0.00, 0.90, 0.00, 0.10])

    P = np.vstack([row_cruise, row_accel, row_decel, row_stop])

    # Ensure rows sum to 1 (numerical safety)
    P = P / P.sum(axis=1, keepdims=True)

    return P


# ── Energy computation per segment ─────────────────────────────────────────


def _kinetic_energy_j(mass_kg: float, speed_ms: float) -> float:
    """Kinetic energy ½mv² in Joules."""
    return 0.5 * mass_kg * speed_ms**2


def _acceleration_energy_wh(
    v_target_ms: float,
    duration_s: float,
    mass_kg: float = _MASS_KG,
    temp_c: float = 25.0,
    slope_deg: float = 0.0,
) -> float:
    """Energy during acceleration from 0 to v_target.

    Physics (Council fix: integrate ∫v²dt, not v_avg²):

    For linear acceleration a = v_target / t_a:
        v(t) = a·t
        d = ½·a·t²  →  d = ½·v_target·t_a
        ∫v²dt = a²·t³/3 = v_target²·t_a/3

    Energy = [KE + F_roll·d + F_aero·∫v²dt + F_grade·d] / η_accel / 3600

    Parameters
    ----------
    v_target_ms : float
        Target cruise speed (m/s).
    duration_s : float
        Acceleration duration (s).
    mass_kg : float
        Total mass (scooter + rider).
    temp_c : float
        Ambient temperature (°C).
    slope_deg : float
        Road grade (degrees).

    Returns
    -------
    float
        Energy in Wh.
    """
    if duration_s <= 0 or v_target_ms <= 0:
        return 0.0

    # a = v_target_ms / duration_s  (constant acceleration, used for physics derivation)
    distance = 0.5 * v_target_ms * duration_s  # d = ½·v·t

    # Forces
    f_roll = mass_kg * _G * _CRR
    f_grade = mass_kg * _G * np.sin(np.radians(slope_deg))

    # Aero: ∫v²dt = v_target²·t/3 (Council fix — not v_avg²·t)
    integral_v2 = v_target_ms**2 * duration_s / 3.0
    f_aero_coeff = 0.5 * _RHO * _CDA  # F_aero = coeff · v²
    aero_energy = f_aero_coeff * integral_v2

    # Kinetic energy
    ke = _kinetic_energy_j(mass_kg, v_target_ms)

    # Total mechanical energy (Joules)
    total_j = ke + (f_roll + f_grade) * distance + aero_energy

    # Efficiency during acceleration (lower than cruise)
    eta = _ETA_ACCEL * (1.0 - 0.003 * (25.0 - temp_c))  # temp derating
    eta = np.clip(eta, 0.50, 0.95)

    return total_j / eta / 3600.0


def _deceleration_energy_wh(
    v_initial_ms: float,
    duration_s: float,
    mass_kg: float = _MASS_KG,
    temp_c: float = 25.0,
    slope_deg: float = 0.0,
    regen_efficiency: float = _REGEN_EFFICIENCY,
) -> float:
    """Energy during deceleration (can be negative = regen recovery).

    Physics:
        d = ½·v_initial·t  (linear decel)
        KE_recoverable = ½·m·(v² - v_cutoff²)  (below cutoff → friction brakes)
        E_regen = -η_regen · η_drivetrain · KE_recoverable
        E_roll = F_roll · d / η
        E_aero = F_aero_coeff · ∫v²dt / η
        E_grade = F_grade · d / η  (downhill adds recoverable energy)

    Council fixes:
    * Regen 0.30 (not 0.60)
    * Cutoff speed 5 km/h
    * Grade included

    Returns
    -------
    float
        Energy in Wh (can be negative if regen > losses).
    """
    if duration_s <= 0 or v_initial_ms <= 0:
        return 0.0

    distance = 0.5 * v_initial_ms * duration_s

    # Forces
    f_roll = mass_kg * _G * _CRR
    f_grade = mass_kg * _G * np.sin(np.radians(slope_deg))
    f_aero_coeff = 0.5 * _RHO * _CDA

    # ∫v²dt for linear decel: v(t) = v0·(1 - t/T) → ∫v²dt = v0²·T/3
    integral_v2 = v_initial_ms**2 * duration_s / 3.0

    # Rolling + aero + grade losses (always positive — resisting motion)
    eta_cruise = float(_battery_efficiency(temp_c))
    loss_energy = (f_roll + f_grade) * distance + f_aero_coeff * integral_v2
    loss_wh = loss_energy / eta_cruise / 3600.0

    # Regen recovery: only above cutoff speed
    v_cutoff = _REGEN_CUTOFF_SPEED_MS
    if v_initial_ms > v_cutoff:
        ke_recoverable = _kinetic_energy_j(mass_kg, v_initial_ms) - _kinetic_energy_j(
            mass_kg, v_cutoff
        )
        # Regen: η_regen · η_drivetrain · KE_recoverable
        regen_wh = -(regen_efficiency * eta_cruise * ke_recoverable) / 3600.0
    else:
        regen_wh = 0.0

    total = loss_wh + regen_wh
    # Clamp: can't recover more than ~50% of cruise energy (BMS limit)
    return total


def _cruise_energy_wh(
    distance_m: float,
    speed_ms: float,
    temp_c: float = 25.0,
    slope_deg: float = 0.0,
) -> float:
    """Steady-state cruising energy — delegates to existing physics model."""
    speed_kmh = speed_ms * 3.6
    return float(compute_trip_energy(distance_m, speed_kmh, temp_c, slope_deg))


def _stopped_energy_wh(duration_s: float, aux_power_w: float = _AUX_POWER_W) -> float:
    """Auxiliary load during stop."""
    return aux_power_w * duration_s / 3600.0


# ── Markov chain simulation ─────────────────────────────────────────────────


@dataclass
class DriveCycleResult:
    """Result of a single drive cycle simulation."""

    states: list[DriveState] = field(default_factory=list)
    durations_s: list[float] = field(default_factory=list)
    distances_m: list[float] = field(default_factory=list)
    energy_wh: float = 0.0
    n_stops: int = 0
    total_distance_m: float = 0.0
    total_duration_s: float = 0.0


def simulate_drive_cycle(
    edge_length_m: float,
    cruise_speed_ms: float = 4.17,
    temp_c: float = 25.0,
    slope_deg: float = 0.0,
    stops_per_km: float = 12.0,
    seed: int | None = None,
    max_steps: int = 500,
) -> DriveCycleResult:
    """Simulate a single drive cycle for one edge.

    Drives the Markov chain by **distance** (not transition count).
    Stops when accumulated cruise+accel distance ≥ edge_length.

    Handles partial cycles: the last state is cut short when distance is
    consumed.

    Parameters
    ----------
    edge_length_m : float
        Edge length in metres.
    cruise_speed_ms : float
        Cruise speed (m/s).  Default 4.17 (15 km/h).
    temp_c : float
        Ambient temperature (°C).
    slope_deg : float
        Road grade (degrees).
    stops_per_km : float
        Target stop frequency for transition matrix calibration.
    seed : int | None
        Random seed.  None → non-deterministic.
    max_steps : int
        Safety limit on Markov chain steps.

    Returns
    -------
    DriveCycleResult
    """
    result = DriveCycleResult()

    # Guard: zero-length or tiny edges
    if edge_length_m < _MIN_EDGE_LENGTH_M:
        result.energy_wh = _cruise_energy_wh(edge_length_m, cruise_speed_ms, temp_c, slope_deg)
        result.total_distance_m = edge_length_m
        result.total_duration_s = edge_length_m / max(cruise_speed_ms, 0.1)
        return result

    rng = np.random.default_rng(seed)
    P = build_transition_matrix(stops_per_km=stops_per_km, cruise_speed_ms=cruise_speed_ms)

    # Start from STOPPED (trip start) or CRUISING (mid-route edge)
    current_state = DriveState.STOPPED

    accumulated_distance = 0.0
    total_energy = 0.0
    n_stops = 0

    for _step in range(max_steps):
        # Check if we've covered the edge distance
        if accumulated_distance >= edge_length_m:
            break

        # Sample duration for current state
        mu, sigma = _DURATION_PARAMS[current_state]
        duration = float(rng.lognormal(mu, sigma))

        # Cap stop duration
        if current_state == DriveState.STOPPED:
            duration = min(duration, _MAX_STOP_DURATION_S)
            n_stops += 1

        # Compute distance covered in this state
        if current_state == DriveState.CRUISING:
            state_distance = cruise_speed_ms * duration
        elif current_state == DriveState.ACCELERATING:
            # d = ½·v·t (linear ramp 0→v)
            state_distance = 0.5 * cruise_speed_ms * duration
        elif current_state == DriveState.DECELERATING:
            # d = ½·v·t (linear ramp v→0)
            state_distance = 0.5 * cruise_speed_ms * duration
        else:  # STOPPED
            state_distance = 0.0

        # Check if this state exceeds remaining distance
        remaining = edge_length_m - accumulated_distance
        if current_state in (DriveState.CRUISING, DriveState.ACCELERATING, DriveState.DECELERATING):
            if state_distance > remaining:
                # Cut this state short — partial cycle
                state_distance = remaining
                if current_state == DriveState.CRUISING:
                    duration = state_distance / cruise_speed_ms
                elif current_state == DriveState.ACCELERATING:
                    # Recompute: d = ½·v·t → t = 2d/v
                    duration = 2.0 * state_distance / cruise_speed_ms
                elif current_state == DriveState.DECELERATING:
                    duration = 2.0 * state_distance / cruise_speed_ms

        # Compute energy for this segment
        if current_state == DriveState.CRUISING:
            seg_energy = _cruise_energy_wh(state_distance, cruise_speed_ms, temp_c, slope_deg)
        elif current_state == DriveState.ACCELERATING:
            seg_energy = _acceleration_energy_wh(
                cruise_speed_ms, duration, temp_c=temp_c, slope_deg=slope_deg
            )
            # If partial, scale energy by actual duration fraction
            # (acceleration energy is time-dependent)
        elif current_state == DriveState.DECELERATING:
            seg_energy = _deceleration_energy_wh(
                cruise_speed_ms, duration, temp_c=temp_c, slope_deg=slope_deg
            )
        else:  # STOPPED
            seg_energy = _stopped_energy_wh(duration)

        # Auxiliary load during ALL states (Council fix: global, not just stopped)
        seg_energy += _AUX_POWER_W * duration / 3600.0

        total_energy += seg_energy
        accumulated_distance += state_distance

        result.states.append(current_state)
        result.durations_s.append(duration)
        result.distances_m.append(state_distance)

        # Transition to next state
        current_state = DriveState(int(rng.choice(4, p=P[current_state])))

    # If we didn't cover the full distance (max_steps hit), add remaining cruise
    if accumulated_distance < edge_length_m:
        remaining = edge_length_m - accumulated_distance
        seg_energy = _cruise_energy_wh(remaining, cruise_speed_ms, temp_c, slope_deg)
        seg_energy += _AUX_POWER_W * (remaining / cruise_speed_ms) / 3600.0
        total_energy += seg_energy
        result.states.append(DriveState.CRUISING)
        result.durations_s.append(remaining / cruise_speed_ms)
        result.distances_m.append(remaining)
        accumulated_distance = edge_length_m

    # Clamp: no negative total energy (BMS can't over-recover)
    total_energy = max(total_energy, 0.0)

    result.energy_wh = total_energy
    result.n_stops = n_stops
    result.total_distance_m = accumulated_distance
    result.total_duration_s = sum(result.durations_s)

    return result


# ── Monte Carlo wrapper ─────────────────────────────────────────────────────


@dataclass
class MCEnergyResult:
    """Monte Carlo energy estimation result."""

    mean_wh: float
    std_wh: float
    p5_wh: float
    p95_wh: float
    n_simulations: int
    mean_stops: float
    wh_per_km: float


def compute_edge_energy_mc(
    edge_length_m: float,
    speed_kmh: float = 15.0,
    temp_c: float = 25.0,
    slope_deg: float = 0.0,
    stops_per_km: float = 12.0,
    n_simulations: int = 200,
    seed: int = 42,
) -> MCEnergyResult:
    """Monte Carlo energy estimation for a single edge.

    Runs ``n_simulations`` drive cycle simulations and aggregates results.

    Parameters
    ----------
    edge_length_m : float
        Edge length in metres.
    speed_kmh : float
        Cruise speed (km/h).  Default 15.
    temp_c : float
        Ambient temperature (°C).
    slope_deg : float
        Road grade (degrees).
    stops_per_km : float
        Target stop frequency.
    n_simulations : int
        Number of MC simulations.  Default 200 (edge-level).
        Use 1000 for final trip validation.
    seed : int
        Base random seed.  Each simulation gets seed + i.

    Returns
    -------
    MCEnergyResult
        Mean, std, p5, p95 energy estimates.
    """
    cruise_speed_ms = speed_kmh / 3.6

    # Guard: zero-length edges
    if edge_length_m < _MIN_EDGE_LENGTH_M:
        steady = _cruise_energy_wh(edge_length_m, cruise_speed_ms, temp_c, slope_deg)
        return MCEnergyResult(
            mean_wh=steady,
            std_wh=0.0,
            p5_wh=steady,
            p95_wh=steady,
            n_simulations=0,
            mean_stops=0.0,
            wh_per_km=steady / max(edge_length_m / 1000.0, 0.001),
        )

    energies = np.empty(n_simulations)
    n_stops_arr = np.empty(n_simulations)

    for i in range(n_simulations):
        result = simulate_drive_cycle(
            edge_length_m=edge_length_m,
            cruise_speed_ms=cruise_speed_ms,
            temp_c=temp_c,
            slope_deg=slope_deg,
            stops_per_km=stops_per_km,
            seed=seed + i,
        )
        energies[i] = result.energy_wh
        n_stops_arr[i] = result.n_stops

    wh_per_km = float(np.mean(energies)) / (edge_length_m / 1000.0)

    return MCEnergyResult(
        mean_wh=float(np.mean(energies)),
        std_wh=float(np.std(energies)),
        p5_wh=float(np.percentile(energies, 5)),
        p95_wh=float(np.percentile(energies, 95)),
        n_simulations=n_simulations,
        mean_stops=float(np.mean(n_stops_arr)),
        wh_per_km=wh_per_km,
    )


# ── Vectorised batch MC for multiple edges ──────────────────────────────────


def compute_edge_energies_batch(
    edge_lengths_m: np.ndarray,
    speeds_kmh: np.ndarray | float = 15.0,
    temps_c: np.ndarray | float = 25.0,
    slopes_deg: np.ndarray | float = 0.0,
    stops_per_km: float = 12.0,
    n_simulations: int = 200,
    seed: int = 42,
    chunk_size: int = 1000,
) -> tuple[np.ndarray, np.ndarray]:
    """Batch MC energy computation for many edges.

    Processes edges in chunks to bound memory.  Returns mean energy and std
    per edge — suitable for caching on graph edges in ``assign_edge_attributes``.

    Parameters
    ----------
    edge_lengths_m : np.ndarray
        Array of edge lengths (metres).
    speeds_kmh : np.ndarray | float
        Speed per edge or scalar.
    temps_c : np.ndarray | float
        Temperature per edge or scalar.
    slopes_deg : np.ndarray | float
        Slope per edge or scalar.
    stops_per_km : float
        Target stop frequency.
    n_simulations : int
        MC simulations per edge.
    seed : int
        Base seed.
    chunk_size : int
        Process this many edges at a time (memory control).

    Returns
    -------
    tuple[np.ndarray, np.ndarray]
        (mean_energy_wh, std_energy_wh) — one per edge.
    """
    n_edges = len(edge_lengths_m)
    mean_wh = np.empty(n_edges)
    std_wh = np.empty(n_edges)

    # Broadcast scalars
    speeds_arr = np.broadcast_to(np.asarray(speeds_kmh, dtype=float), n_edges)
    temps_arr = np.broadcast_to(np.asarray(temps_c, dtype=float), n_edges)
    slopes_arr = np.broadcast_to(np.asarray(slopes_deg, dtype=float), n_edges)

    for start in range(0, n_edges, chunk_size):
        end = min(start + chunk_size, n_edges)
        for i in range(start, end):
            result = compute_edge_energy_mc(
                edge_length_m=float(edge_lengths_m[i]),
                speed_kmh=float(speeds_arr[i]),
                temp_c=float(temps_arr[i]),
                slope_deg=float(slopes_arr[i]),
                stops_per_km=stops_per_km,
                n_simulations=n_simulations,
                seed=seed + i,
            )
            mean_wh[i] = result.mean_wh
            std_wh[i] = result.std_wh

    return mean_wh, std_wh


# ── Validation ─────────────────────────────────────────────────────────────


def validate_drive_cycle_model() -> dict[str, Any]:
    """Validate the drive cycle model against literature values.

    Runs MC on a canonical 1 km edge at 15 km/h, 25°C, flat, 12 stops/km.
    Expected: Wh/km in [6, 10] range (literature: 8-15, our model should be
    in the lower-mid range with 30% regen and 12W aux).

    Returns
    -------
    dict
        Validation results with steady-state comparison.
    """
    # Steady-state reference
    steady_wh = _cruise_energy_wh(1000.0, 4.17, 25.0, 0.0)
    steady_wh_per_km = steady_wh  # 1 km edge

    # MC with stop-and-go
    mc_result = compute_edge_energy_mc(
        edge_length_m=1000.0,
        speed_kmh=15.0,
        temp_c=25.0,
        slope_deg=0.0,
        stops_per_km=12.0,
        n_simulations=200,
        seed=42,
    )

    delta_wh = mc_result.mean_wh - steady_wh
    delta_pct = (delta_wh / steady_wh * 100.0) if steady_wh > 0 else 0.0

    within_band = 6.0 <= mc_result.wh_per_km <= 15.0

    return {
        "steady_state_wh_per_km": round(steady_wh_per_km, 2),
        "mc_mean_wh_per_km": round(mc_result.wh_per_km, 2),
        "mc_std_wh": round(mc_result.std_wh, 2),
        "mc_p5_wh": round(mc_result.p5_wh, 2),
        "mc_p95_wh": round(mc_result.p95_wh, 2),
        "delta_wh": round(delta_wh, 2),
        "delta_pct": round(delta_pct, 1),
        "mean_stops": round(mc_result.mean_stops, 1),
        "within_literature_band": within_band,
        "n_simulations": mc_result.n_simulations,
        "summary": (
            f"Steady-state: {steady_wh_per_km:.1f} Wh/km → "
            f"MC mean: {mc_result.wh_per_km:.1f} Wh/km "
            f"(+{delta_pct:.0f}% from stop-and-go). "
            f"{'Within' if within_band else 'OUTSIDE'} literature [6-15] band. "
            f"Mean stops: {mc_result.mean_stops:.1f}/km."
        ),
    }
