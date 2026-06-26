"""Pareto multi-objective routing — alpha-based NSGA-II.

Trades off energy consumption (Wh) vs thermal stress (degree-hours) for
e-scooter routes.  The decision variable alpha in [0, 1] blends the two
objectives into a composite edge weight; NSGA-II explores the alpha-space
to find the Pareto frontier of non-dominated routes.

Edge attributes (per edge):
    * ``energy_wh`` — physics-based trip energy (Wh)
    * ``degree_hours`` — max(0, T - 25) * length_km, thermal stress metric
    * ``temp_c`` — ambient temperature (°C) at edge midpoint

Correlation pre-check (Oracle Decision 3):
    If Pearson r between energy and degree-hours across random OD routes
    exceeds ``settings.pareto_correlation_threshold`` (0.8), switch the
    2nd objective to route length.
"""

from __future__ import annotations

import logging
from typing import Any

import networkx as nx
import numpy as np
from pymoo.core.problem import Problem
from scipy.stats import pearsonr

from uhi_battery.config import settings
from uhi_battery.models.energy import compute_trip_energy

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────
_DEGREE_HOUR_THRESHOLD_C: float = 25.0
_DEFAULT_SPEED_KMH: float = 15.0
_DEFAULT_HOUR: float = 14.0


# ── Internal helpers ────────────────────────────────────────────────────────


def _sample_lst_at_midpoint(
    lst_ds: Any,
    u_xy: tuple[float, float],
    v_xy: tuple[float, float],
    hour: float = _DEFAULT_HOUR,
    day_idx: int = 0,
) -> float:
    """Sample LST (°C) at the midpoint of an edge."""
    mid_lon = (u_xy[0] + v_xy[0]) / 2.0
    mid_lat = (u_xy[1] + v_xy[1]) / 2.0
    try:
        from uhi_battery.data.fusion import reconstruct_lst_at_hour

        da = reconstruct_lst_at_hour(lst_ds, hour, day_idx=day_idx)
        val = float(da.sel(x=mid_lon, y=mid_lat, method="nearest").values)
        return val
    except (ImportError, KeyError, ValueError, OSError):
        return 25.0


def _edge_distance_km(G: nx.MultiDiGraph, u: int, v: int, k: int) -> float:
    """Return edge length in km."""
    return float(G[u][v][k].get("length", 0)) / 1000.0


def _normalise_edge_attribute(
    G: nx.MultiDiGraph, attr: str
) -> dict[tuple[int, int, int], float]:
    """Min-max normalise an edge attribute across the graph."""
    vals = [float(G[u][v][k].get(attr, 0)) for u, v, k in G.edges(keys=True)]
    if not vals:
        return {}
    vals_arr = np.array(vals)
    v_min, v_max = float(vals_arr.min()), float(vals_arr.max())
    denom = v_max - v_min if v_max > v_min else 1.0
    return {
        (u, v, k): (float(G[u][v][k].get(attr, 0)) - v_min) / denom
        for u, v, k in G.edges(keys=True)
    }


# ── Edge attribute assignment ───────────────────────────────────────────────


def assign_edge_attributes(
    G: nx.MultiDiGraph,
    lst_ds: Any | None = None,
    hour: float = _DEFAULT_HOUR,
    day_idx: int = 0,
    speed_kmh: float = _DEFAULT_SPEED_KMH,
    use_mc: bool = False,
    n_mc_simulations: int = 50,
    stops_per_km: float = 12.0,
) -> nx.MultiDiGraph:
    """Add per-edge energy and thermal-stress attributes.

    For each edge: ``temp_c``, ``grade_deg``, ``energy_wh``, ``degree_hours``.
    When ``use_mc=True``, also adds ``energy_std`` (MC std) and uses the
    stop-and-go drive cycle model instead of steady-state physics.

    Parameters
    ----------
    G : networkx.MultiDiGraph
        OSMnx drive graph with optional ``grade`` and ``length`` attributes.
    lst_ds : xr.Dataset | None
        Fused LST dataset.  If None, uses T_ref = 25 °C.
    hour : float
        Hour of day (0-24) for LST sampling.
    day_idx : int
        Day index (0-based).
    speed_kmh : float
        Assumed e-scooter speed (km/h).
    use_mc : bool
        If True, use Monte Carlo drive cycle model (stop-and-go).
        If False (default), use steady-state physics (backward compatible).
    n_mc_simulations : int
        Number of MC simulations per edge (only if use_mc=True).
        Default 50 (edge-level).  Use 200 for final validation.
    stops_per_km : float
        Target stop frequency for MC model.  Default 12.

    Returns
    -------
    networkx.MultiDiGraph
        Graph with per-edge attributes set.
    """
    # Pre-compute hourly LST grid ONCE (avoid 12k × reconstruct_lst_at_hour calls)
    hourly_lst = None
    if lst_ds is not None:
        # Try full fusion zarr first (has diurnal_mean + diurnal_amp)
        try:
            from uhi_battery.data.fusion import reconstruct_lst_at_hour

            hourly_lst = reconstruct_lst_at_hour(lst_ds, hour, day_idx=day_idx)
        except (ImportError, KeyError, ValueError, OSError):
            pass

        # Fallback: simple single-day netCDF (just lst_daily, no diurnal params)
        if hourly_lst is None and "lst_daily" in lst_ds:
            try:
                hourly_lst = lst_ds["lst_daily"].isel(time=0)
            except (KeyError, ValueError):
                pass

    # Lazy import MC model (only if needed)
    mc_func = None
    if use_mc:
        from uhi_battery.models.drive_cycle import compute_edge_energy_mc

        mc_func = compute_edge_energy_mc

    for u, v, k in G.edges(keys=True):
        dist_km = _edge_distance_km(G, u, v, k)
        grade = float(G[u][v][k].get("grade", 0.0))

        if hourly_lst is not None:
            u_xy = (float(G.nodes[u].get("x", 0)), float(G.nodes[u].get("y", 0)))
            v_xy = (float(G.nodes[v].get("x", 0)), float(G.nodes[v].get("y", 0)))
            mid_lon = (u_xy[0] + v_xy[0]) / 2.0
            mid_lat = (u_xy[1] + v_xy[1]) / 2.0
            try:
                temp_c = float(hourly_lst.sel(x=mid_lon, y=mid_lat, method="nearest").values)
            except (KeyError, ValueError):
                temp_c = 25.0
        else:
            temp_c = 25.0

        if use_mc and mc_func is not None:
            mc_result = mc_func(
                edge_length_m=dist_km * 1000.0,
                speed_kmh=speed_kmh,
                temp_c=temp_c,
                slope_deg=grade,
                stops_per_km=stops_per_km,
                n_simulations=n_mc_simulations,
                seed=42,
            )
            energy_wh = mc_result.mean_wh
            G[u][v][k]["energy_std"] = mc_result.std_wh
        else:
            energy_wh = float(
                compute_trip_energy(
                    distance_m=dist_km * 1000.0,
                    speed_kmh=speed_kmh,
                    temp_c=temp_c,
                    slope_deg=grade,
                )
            )

        degree_hours = max(0.0, temp_c - _DEGREE_HOUR_THRESHOLD_C) * dist_km

        G[u][v][k]["temp_c"] = temp_c
        G[u][v][k]["grade_deg"] = grade
        G[u][v][k]["energy_wh"] = energy_wh
        G[u][v][k]["degree_hours"] = degree_hours

    return G


# ── Correlation pre-check ───────────────────────────────────────────────────


def check_objective_correlation(
    G: nx.MultiDiGraph,
    n_samples: int = 50,
    seed: int = 42,
) -> dict[str, Any]:
    """Pearson r between energy and degree-hours across random OD routes.

    Parameters
    ----------
    G : networkx.MultiDiGraph
        Graph with ``energy_wh`` and ``degree_hours`` edge attributes.
    n_samples : int
        Number of random OD pairs.
    seed : int
        Random seed.

    Returns
    -------
    dict
        ``r``, ``p_value``, ``decision`` ("energy" or "route_length"),
        ``n_samples``, ``threshold``.
    """
    rng = np.random.default_rng(seed)
    nodes = list(G.nodes())
    if len(nodes) < 2:
        return {"r": 0.0, "p_value": 1.0, "decision": "energy", "n_samples": 0}

    energies: list[float] = []
    dh_list: list[float] = []

    for _ in range(n_samples):
        o, d = int(rng.choice(nodes)), int(rng.choice(nodes))
        if o == d:
            continue
        try:
            path = nx.shortest_path(G, o, d, weight="length")
        except nx.NetworkXNoPath:
            continue

        total_e = 0.0
        total_dh = 0.0
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            total_e += float(G[u][v][0].get("energy_wh", 0))
            total_dh += float(G[u][v][0].get("degree_hours", 0))
        energies.append(total_e)
        dh_list.append(total_dh)

    if len(energies) < 3:
        return {"r": 0.0, "p_value": 1.0, "decision": "energy", "n_samples": len(energies)}

    r, p = pearsonr(energies, dh_list)
    threshold = settings.pareto_corr_switch_threshold
    decision = "route_length" if abs(r) > threshold else "energy"

    return {
        "r": float(r),
        "p_value": float(p),
        "decision": decision,
        "n_samples": len(energies),
        "threshold": threshold,
    }


# ── NSGA-II Problem ─────────────────────────────────────────────────────────


class ParetoRoutingProblem(Problem):
    """pymoo Problem: 1 variable (alpha in [0,1]), 2 objectives.

    Edge weight = alpha * energy_norm + (1-alpha) * obj2_norm.
    Route = nx.shortest_path(weight=composite).
    Objectives = (sum(energy_wh), sum(obj2_attr)).
    """

    def __init__(
        self,
        G: nx.MultiDiGraph,
        origin: int,
        dest: int,
        energy_norm: dict[tuple[int, int, int], float],
        obj2_norm: dict[tuple[int, int, int], float],
        obj2_attr: str = "degree_hours",
        obj1_attr: str = "energy_wh",
    ):
        super().__init__(n_var=1, n_obj=2, xl=np.array([0.0]), xu=np.array([1.0]))
        self.G = G
        self.origin = origin
        self.dest = dest
        self.energy_norm = energy_norm
        self.obj2_norm = obj2_norm
        self.obj2_attr = obj2_attr
        self.obj1_attr = obj1_attr

    def _evaluate(self, X: np.ndarray, out: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        n = X.shape[0]
        F = np.zeros((n, 2))

        for i in range(n):
            alpha = float(np.clip(X[i, 0], 0.0, 1.0))

            for u, v, k in self.G.edges(keys=True):
                w = alpha * self.energy_norm[(u, v, k)] + (1.0 - alpha) * self.obj2_norm[(u, v, k)]
                self.G[u][v][k]["_composite"] = w

            try:
                path = nx.shortest_path(self.G, self.origin, self.dest, weight="_composite")
            except nx.NetworkXNoPath:
                F[i, 0] = 1e9
                F[i, 1] = 1e9
                continue

            total_e = 0.0
            total_o2 = 0.0
            for j in range(len(path) - 1):
                u, v = path[j], path[j + 1]
                total_e += float(self.G[u][v][0].get(self.obj1_attr, 0))
                total_o2 += float(self.G[u][v][0].get(self.obj2_attr, 0))

            F[i, 0] = total_e
            F[i, 1] = total_o2

        out["F"] = F


# ── Public API ──────────────────────────────────────────────────────────────


def solve_pareto(
    G: nx.MultiDiGraph,
    origin: int,
    dest: int,
    pop_size: int = 50,
    n_gen: int = 50,
    seed: int = 42,
    obj2_attr: str = "degree_hours",
) -> list[dict[str, Any]]:
    """Run NSGA-II to find the Pareto frontier.

    Parameters
    ----------
    G : networkx.MultiDiGraph
        Graph with ``energy_wh`` and *obj2_attr* edge attributes.
    origin, dest : int
        OSMnx node IDs.
    pop_size : int
        Population size.
    n_gen : int
        Generations.
    seed : int
        Random seed.
    obj2_attr : str
        Second objective attribute (``"degree_hours"`` or ``"length"``).

    Returns
    -------
    list[dict]
        Non-dominated routes: ``alpha``, ``energy_wh``, ``obj2_value``,
        ``obj2_name``, ``route`` (list of node IDs).
    """
    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.optimize import minimize

    energy_norm = _normalise_edge_attribute(G, "energy_wh")
    obj2_norm = _normalise_edge_attribute(G, obj2_attr)

    problem = ParetoRoutingProblem(
        G=G,
        origin=origin,
        dest=dest,
        energy_norm=energy_norm,
        obj2_norm=obj2_norm,
        obj2_attr=obj2_attr,
    )

    algorithm = NSGA2(pop_size=pop_size)
    res = minimize(problem, algorithm, ("n_gen", n_gen), seed=seed, verbose=False)

    # Build frontier routes
    frontiers: list[dict[str, Any]] = []
    n_solutions = res.F.shape[0]
    for i in range(n_solutions):
        f_vals = res.F[i] if n_solutions > 0 else []
        alpha = float(res.X[i, 0]) if res.X.ndim > 1 else float(res.X[i])

        try:
            for u, v, k in G.edges(keys=True):
                w = alpha * energy_norm[(u, v, k)] + (1.0 - alpha) * obj2_norm[(u, v, k)]
                G[u][v][k]["_composite"] = w
            path = nx.shortest_path(G, origin, dest, weight="_composite")
        except nx.NetworkXNoPath:
            continue

        frontiers.append({
            "alpha": round(alpha, 4),
            "energy_wh": round(float(f_vals[0]), 2),
            "obj2_value": round(float(f_vals[1]), 2),
            "obj2_name": obj2_attr,
            "route": path,
        })

    # Deduplicate
    seen = set()
    unique: list[dict[str, Any]] = []
    for f in frontiers:
        key = tuple(f["route"])
        if key not in seen:
            seen.add(key)
            unique.append(f)

    return unique
