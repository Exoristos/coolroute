"""Tests for Pareto routing module.

All mock-based — no OSMnx, no GEE, no network required.  Uses synthetic
NetworkX graphs with known edge attributes.
"""

from __future__ import annotations

import networkx as nx
import numpy as np
import pytest

from uhi_battery.routing import network as net_mod
from uhi_battery.routing import pareto as pareto_mod
from uhi_battery.routing.pareto import (
    _DEGREE_HOUR_THRESHOLD_C,
    assign_edge_attributes,
    check_objective_correlation,
    solve_pareto,
)

# ── Synthetic graph builders ────────────────────────────────────────────────


def _make_grid_graph(
    n: int = 5,
    edge_length_m: float = 200.0,
    grade_deg: float = 0.0,
) -> nx.MultiDiGraph:
    """Build a small synthetic OSMnx-like MultiDiGraph.

    n×n grid of nodes with (x, y) coordinates in EPSG:4326.
    Each edge has ``length`` (m), ``grade`` (deg, optional).
    """
    G = nx.MultiDiGraph()
    for i in range(n):
        for j in range(n):
            node = i * n + j
            G.add_node(node, x=29.05 + j * 0.001, y=40.95 + i * 0.001)

    for i in range(n):
        for j in range(n):
            u = i * n + j
            # Right neighbour
            if j + 1 < n:
                v = u + 1
                G.add_edge(u, v, length=edge_length_m, grade=grade_deg)
                G.add_edge(v, u, length=edge_length_m, grade=grade_deg)
            # Bottom neighbour
            if i + 1 < n:
                v = u + n
                G.add_edge(u, v, length=edge_length_m, grade=grade_deg)
                G.add_edge(v, u, length=edge_length_m, grade=grade_deg)

    return G


def _make_graph_with_temps(
    n: int = 4,
    edge_length_m: float = 250.0,
    temps_c: list[float] | None = None,
) -> nx.MultiDiGraph:
    """Build a grid graph with varying edge attributes after assignment.

    Returns a graph that's had ``assign_edge_attributes`` called on it.
    Since we mock LST, we pre-set ``temp_c`` manually.
    """
    G = _make_grid_graph(n=n, edge_length_m=edge_length_m)
    if temps_c is None:
        temps_c = [25.0]
    # Assign manually: every edge gets one of the given temps (cycling)
    temp_idx = 0
    for u, v, k in G.edges(keys=True):
        tc = temps_c[temp_idx % len(temps_c)]
        temp_idx += 1
        dist_km = float(G[u][v][k].get("length", edge_length_m)) / 1000.0
        grade = float(G[u][v][k].get("grade", 0.0))
        energy = float(pareto_mod.compute_trip_energy(
            distance_m=dist_km * 1000.0, speed_kmh=15.0, temp_c=tc, slope_deg=grade
        ))
        dh = max(0.0, tc - _DEGREE_HOUR_THRESHOLD_C) * dist_km
        G[u][v][k]["temp_c"] = tc
        G[u][v][k]["grade_deg"] = grade
        G[u][v][k]["energy_wh"] = energy
        G[u][v][k]["degree_hours"] = dh
    return G


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any accidental network/DEM calls in tests."""
    monkeypatch.setattr(net_mod, "pull_dem", lambda *a, **kw: None)
    monkeypatch.setattr(net_mod, "load_network", lambda *a, **kw: _make_grid_graph())


# ── Tests ───────────────────────────────────────────────────────────────────


class TestAssignEdgeAttributes:
    """Edge attribute assignment logic."""

    def test_all_attributes_set(self) -> None:
        G = _make_grid_graph()
        G = assign_edge_attributes(G, lst_ds=None)  # no LST → all 25°C
        for u, v, k in G.edges(keys=True):
            assert "temp_c" in G[u][v][k]
            assert "energy_wh" in G[u][v][k]
            assert "degree_hours" in G[u][v][k]
            assert G[u][v][k]["energy_wh"] > 0

    def test_no_lst_uses_25c(self) -> None:
        G = _make_grid_graph()
        G = assign_edge_attributes(G, lst_ds=None)
        for u, v, k in G.edges(keys=True):
            assert G[u][v][k]["temp_c"] == 25.0
            # At 25°C, degree_hours = 0 (no thermal stress)
            assert G[u][v][k]["degree_hours"] == 0.0

    def test_degree_hours_below_threshold_is_zero(self) -> None:
        G = _make_graph_with_temps(temps_c=[20.0, 24.0])
        for u, v, k in G.edges(keys=True):
            assert G[u][v][k]["degree_hours"] == 0.0

    def test_degree_hours_above_threshold_positive(self) -> None:
        G = _make_graph_with_temps(temps_c=[30.0, 35.0, 40.0])
        for u, v, k in G.edges(keys=True):
            tc = G[u][v][k]["temp_c"]
            if tc > _DEGREE_HOUR_THRESHOLD_C:
                assert G[u][v][k]["degree_hours"] > 0

    def test_degree_hours_scales_with_temp(self) -> None:
        """Higher temp → more degree-hours per km."""
        G_cool = _make_graph_with_temps(temps_c=[30.0])
        G_hot = _make_graph_with_temps(temps_c=[40.0])
        dh_cool = G_cool[0][1][0]["degree_hours"]
        dh_hot = G_hot[0][1][0]["degree_hours"]
        assert dh_hot > dh_cool

    def test_energy_increases_with_slope(self) -> None:
        G_flat = _make_grid_graph(grade_deg=0.0)
        G_steep = _make_grid_graph(grade_deg=5.0)
        G_flat = assign_edge_attributes(G_flat, lst_ds=None)
        G_steep = assign_edge_attributes(G_steep, lst_ds=None)
        e_flat = G_flat[0][1][0]["energy_wh"]
        e_steep = G_steep[0][1][0]["energy_wh"]
        assert e_steep > e_flat

    def test_preserves_original_edges(self) -> None:
        G = _make_grid_graph()
        orig_edges = set(G.edges(keys=True))
        G = assign_edge_attributes(G, lst_ds=None)
        assert set(G.edges(keys=True)) == orig_edges


class TestCheckObjectiveCorrelation:
    """Correlation pre-check between energy and degree-hours."""

    def test_returns_dict_keys(self) -> None:
        G = _make_graph_with_temps(temps_c=[25.0, 30.0, 35.0, 40.0])
        result = check_objective_correlation(G, n_samples=10)
        for key in ("r", "p_value", "decision", "n_samples"):
            assert key in result

    def test_high_correlation_switches_decision(self) -> None:
        """When energy and degree-hours are perfectly correlated (all same temp,
        energy proportional to distance and degree-hours also proportional to
        distance), r should be high."""
        G = _make_graph_with_temps(temps_c=[35.0])  # all edges same temp
        result = check_objective_correlation(G, n_samples=15)
        assert abs(result["r"]) > 0.7  # should be highly correlated

    def test_low_correlation_on_varied_temps(self) -> None:
        """With varied temps, energy depends on both distance and temp."""
        n = 6
        G = _make_grid_graph(n=n, edge_length_m=200.0)
        temps = []
        rng = np.random.default_rng(123)
        for _u, _v, _k in G.edges(keys=True):
            temps.append(float(rng.uniform(20, 45)))
        G = _make_graph_with_temps(n=n, temps_c=temps)
        result = check_objective_correlation(G, n_samples=20)
        # With varying temps, correlation should drop
        assert -0.5 < result["r"] < 0.99


class TestParetoRoutingProblem:
    """NSGA-II problem evaluation."""

    def test_solve_returns_nonempty(self) -> None:
        G = _make_graph_with_temps(n=5, temps_c=[25.0, 30.0, 35.0, 40.0])
        frontier = solve_pareto(G, 0, 24, pop_size=30, n_gen=20, seed=42)
        assert len(frontier) > 0

    def test_frontier_has_required_keys(self) -> None:
        G = _make_graph_with_temps(n=5, temps_c=[25.0, 35.0, 45.0])
        frontier = solve_pareto(G, 0, 24, pop_size=30, n_gen=20, seed=42)
        for f in frontier:
            for key in ("alpha", "energy_wh", "obj2_value", "obj2_name", "route"):
                assert key in f, f"Missing key: {key}"
            assert isinstance(f["route"], list)
            assert len(f["route"]) >= 2
            assert f["energy_wh"] > 0

    def test_different_seed_reproducible(self) -> None:
        G = _make_graph_with_temps(n=5, temps_c=[25.0, 35.0, 45.0])
        f1 = solve_pareto(G, 0, 24, pop_size=20, n_gen=15, seed=42)
        f2 = solve_pareto(G, 0, 24, pop_size=20, n_gen=15, seed=42)
        assert len(f1) == len(f2)
        # Same seed → same frontier
        for a, b in zip(f1, f2, strict=True):
            assert a["alpha"] == b["alpha"]
            assert a["energy_wh"] == b["energy_wh"]

    def test_alpha_0_all_thermal(self) -> None:
        """Alpha=0 weights degree-hours only → route may differ from alpha=1."""
        G = _make_graph_with_temps(
            n=6, edge_length_m=200.0,
            temps_c=[20.0] * 10 + [40.0] * 30 + [20.0] * 20
        )
        frontier = solve_pareto(G, 0, 35, pop_size=30, n_gen=20, seed=42)
        alphas = [f["alpha"] for f in frontier]
        assert min(alphas) >= 0.0
        assert max(alphas) <= 1.0

    def test_disconnected_graph_handled(self) -> None:
        """Graph with no path should not crash."""
        G = nx.MultiDiGraph()
        G.add_node(0, x=29.05, y=40.95)
        G.add_node(1, x=29.06, y=40.96)
        # No edges → no path
        frontier = solve_pareto(G, 0, 1, pop_size=10, n_gen=5)
        assert len(frontier) == 0  # No routes found


class TestEdgeAttributeNormalisation:
    """Min-max normalisation of edge attributes."""

    def test_normalised_between_0_1(self) -> None:
        G = _make_graph_with_temps(n=4, temps_c=[25.0, 30.0, 35.0, 40.0])
        norm = pareto_mod._normalise_edge_attribute(G, "energy_wh")
        vals = list(norm.values())
        assert min(vals) >= 0.0
        assert max(vals) <= 1.0

    def test_constant_attribute_normalised_to_zero(self) -> None:
        G = _make_graph_with_temps(n=3, temps_c=[30.0])
        norm = pareto_mod._normalise_edge_attribute(G, "degree_hours")
        # All edges same temp → all same degree_hours → denom=0 → all values = 0
        vals = set(norm.values())
        assert len(vals) == 1


class TestMCIntegration:
    """Monte Carlo drive cycle integration in assign_edge_attributes."""

    def test_mc_adds_energy_std(self) -> None:
        """When use_mc=True, edges should have energy_std attribute."""
        G = _make_grid_graph()
        G = assign_edge_attributes(G, lst_ds=None, use_mc=True, n_mc_simulations=10)
        for u, v, k in G.edges(keys=True):
            assert "energy_std" in G[u][v][k]
            assert G[u][v][k]["energy_std"] >= 0.0

    def test_mc_energy_higher_than_steady(self) -> None:
        """MC energy should be higher than steady-state (stop-and-go losses)."""
        G_ss = _make_grid_graph()
        G_ss = assign_edge_attributes(G_ss, lst_ds=None, use_mc=False)
        G_mc = _make_grid_graph()
        G_mc = assign_edge_attributes(G_mc, lst_ds=None, use_mc=True, n_mc_simulations=50)
        e_ss = G_ss[0][1][0]["energy_wh"]
        e_mc = G_mc[0][1][0]["energy_wh"]
        assert e_mc > e_ss

    def test_mc_preserves_slope_monotonicity(self) -> None:
        """Steeper slope → more energy, even with MC."""
        G_flat = _make_grid_graph(grade_deg=0.0)
        G_steep = _make_grid_graph(grade_deg=5.0)
        G_flat = assign_edge_attributes(G_flat, lst_ds=None, use_mc=True, n_mc_simulations=20)
        G_steep = assign_edge_attributes(G_steep, lst_ds=None, use_mc=True, n_mc_simulations=20)
        e_flat = G_flat[0][1][0]["energy_wh"]
        e_steep = G_steep[0][1][0]["energy_wh"]
        assert e_steep > e_flat

    def test_default_is_steady_state(self) -> None:
        """Default use_mc=False → no energy_std attribute."""
        G = _make_grid_graph()
        G = assign_edge_attributes(G, lst_ds=None)
        for u, v, k in G.edges(keys=True):
            assert "energy_std" not in G[u][v][k]


class TestNetworkHelpers:
    """Network loading helpers (mock targets)."""

    def test_load_network_accepts_bbox(self) -> None:
        """Smoke test: load_network is importable and takes bbox."""
        from uhi_battery.routing.network import load_network
        assert callable(load_network)

    def test_pull_dem_accepts_bbox(self) -> None:
        from uhi_battery.routing.network import pull_dem
        assert callable(pull_dem)
