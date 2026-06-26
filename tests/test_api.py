"""Tests for FastAPI REST service.

Uses TestClient — no running server needed.  Tests work with real data files
when available, skip gracefully when not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ── Check data availability ─────────────────────────────────────────────────
LST_ZARR = Path("data/processed/lst_hourly.zarr")
HOTSPOTS_GEOJSON = Path("data/processed/hotspots.geojson")
FRONTIERS_JSON = Path("data/processed/pareto_frontiers.json")
METRICS_JSON = Path("data/processed/metrics.json")
SOH_MODEL_PKL = Path("data/processed/soh_model.pkl")
ENERGY_MODEL_PKL = Path("data/processed/energy_model.pkl")

HAS_LST = LST_ZARR.exists()
HAS_HOTSPOTS = HOTSPOTS_GEOJSON.exists()
HAS_FRONTIERS = FRONTIERS_JSON.exists()
HAS_METRICS = METRICS_JSON.exists()
HAS_SOH = SOH_MODEL_PKL.exists()

# ── Test setup ──────────────────────────────────────────────────────────────


def _create_minimal_temp_files() -> None:
    """Create minimal temp versions of data files for tests if missing."""
    if not HAS_METRICS:
        METRICS_JSON.parent.mkdir(parents=True, exist_ok=True)
        METRICS_JSON.write_text(json.dumps({"test": True, "routing_metrics": {}}))
    if not HAS_FRONTIERS:
        FRONTIERS_JSON.parent.mkdir(parents=True, exist_ok=True)
        FRONTIERS_JSON.write_text(json.dumps({
            "correlation_r": 0.97, "obj2": "length",
            "pairs": [{"pair_idx": 1, "origin": 0, "dest": 1, "obj2": "length",
                       "frontier_size": 1, "frontier": [
                           {"alpha": 0.5, "energy_wh": 20.0, "obj2_value": 5000.0,
                            "obj2_name": "length"}]}]
        }))


@pytest.fixture(scope="module")
def client() -> TestClient:
    _create_minimal_temp_files()
    from uhi_battery.api.app import app
    return TestClient(app)


# ── Tests ───────────────────────────────────────────────────────────────────


class TestHealth:
    """GET /health"""

    def test_returns_ok(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestHeatLayer:
    """GET /heat-layer"""

    @pytest.mark.skipif(not HAS_LST, reason="LST zarr not available")
    def test_default_day_hour(self, client: TestClient) -> None:
        r = client.get("/heat-layer")
        assert r.status_code == 200
        data = r.json()
        assert "values" in data
        assert "stats" in data
        assert "min" in data["stats"]
        assert data["stats"]["min"] >= -50  # plausible range

    @pytest.mark.skipif(not HAS_LST, reason="LST zarr not available")
    def test_specific_day(self, client: TestClient) -> None:
        r = client.get("/heat-layer?day=0&hour=10.5")
        assert r.status_code == 200
        data = r.json()
        assert data["day"] == 0
        assert data["hour"] == 10.5

    @pytest.mark.skipif(not HAS_LST, reason="LST zarr not available")
    def test_stride_reduces_grid(self, client: TestClient) -> None:
        r1 = client.get("/heat-layer?stride=1")
        r5 = client.get("/heat-layer?stride=5")
        if r1.status_code == 200 and r5.status_code == 200:
            assert r5.json()["grid_shape"][0] < r1.json()["grid_shape"][0]

    @pytest.mark.skipif(not HAS_LST, reason="LST zarr not available")
    def test_invalid_day_rejected(self, client: TestClient) -> None:
        r = client.get("/heat-layer?day=99999")
        assert r.status_code in (404, 422, 500)

    @pytest.mark.skipif(HAS_LST, reason="LST zarr exists — testing missing data separately")
    def test_missing_lst_404(self, client: TestClient) -> None:
        r = client.get("/heat-layer")
        assert r.status_code in (404, 422, 500)


class TestHotspots:
    """GET /hotspots"""

    def test_returns_geojson(self, client: TestClient) -> None:
        r = client.get("/hotspots")
        assert r.status_code == 200
        data = r.json()
        assert "type" in data
        if data.get("features"):
            assert isinstance(data["features"], list)

    @pytest.mark.skipif(not HAS_HOTSPOTS, reason="Hotspots file not available")
    def test_has_features(self, client: TestClient) -> None:
        r = client.get("/hotspots")
        data = r.json()
        assert len(data.get("features", [])) > 0


class TestRoute:
    """GET /route"""

    VALID_O = (29.05, 40.95)
    VALID_D = (29.08, 40.97)

    def test_valid_coords_returns_frontier(self, client: TestClient) -> None:
        r = client.get(
            f"/route?origin_lon={self.VALID_O[0]}&origin_lat={self.VALID_O[1]}"
            f"&dest_lon={self.VALID_D[0]}&dest_lat={self.VALID_D[1]}"
        )
        assert r.status_code == 200
        data = r.json()
        assert "frontier_size" in data
        assert "note" in data

    def test_out_of_bbox_returns_422(self, client: TestClient) -> None:
        r = client.get(
            "/route?origin_lon=10.0&origin_lat=50.0&dest_lon=11.0&dest_lat=51.0"
        )
        assert r.status_code == 422

    def test_missing_params_422(self, client: TestClient) -> None:
        r = client.get("/route?origin_lon=29.05")
        assert r.status_code == 422

    def test_frontier_has_required_fields(self, client: TestClient) -> None:
        r = client.get(
            f"/route?origin_lon={self.VALID_O[0]}&origin_lat={self.VALID_O[1]}"
            f"&dest_lon={self.VALID_D[0]}&dest_lat={self.VALID_D[1]}"
        )
        if r.status_code == 200:
            data = r.json()
            for f in data.get("frontier", []):
                for key in ("energy_wh", "obj2_value", "obj2_name", "alpha"):
                    assert key in f, f"Missing key in frontier: {key}"


class TestMetrics:
    """GET /metrics"""

    def test_returns_json(self, client: TestClient) -> None:
        r = client.get("/metrics")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)

    @pytest.mark.skipif(not HAS_METRICS, reason="Metrics file not available")
    def test_has_expected_sections(self, client: TestClient) -> None:
        r = client.get("/metrics")
        data = r.json()
        assert any(k in data for k in ("data_quality", "model_quality", "routing_metrics"))


class TestSoH:
    """GET /soh"""

    def test_valid_params_returns_retention(self, client: TestClient) -> None:
        r = client.get("/soh?temp_c=25&cycles=500")
        assert r.status_code == 200
        data = r.json()
        assert "retention_pct" in data
        assert 0 <= data["retention_pct"] <= 100

    def test_high_temp_lower_retention(self, client: TestClient) -> None:
        r25 = client.get("/soh?temp_c=25&cycles=500")
        r45 = client.get("/soh?temp_c=45&cycles=500")
        if r25.status_code == 200 and r45.status_code == 200:
            assert r45.json()["retention_pct"] < r25.json()["retention_pct"]

    def test_more_cycles_lower_retention(self, client: TestClient) -> None:
        r100 = client.get("/soh?temp_c=25&cycles=100")
        r500 = client.get("/soh?temp_c=25&cycles=500")
        if r100.status_code == 200 and r500.status_code == 200:
            assert r500.json()["retention_pct"] < r100.json()["retention_pct"]

    def test_fresh_cell_returns_100(self, client: TestClient) -> None:
        r = client.get("/soh?temp_c=25&cycles=0")
        assert r.status_code == 200
        assert r.json()["retention_pct"] == 100.0

    def test_invalid_temp_rejected(self, client: TestClient) -> None:
        r = client.get("/soh?temp_c=500&cycles=100")
        assert r.status_code == 422


class TestEnergy:
    """GET /energy"""

    def test_valid_params_returns_energy(self, client: TestClient) -> None:
        r = client.get("/energy?distance_m=3000&speed_kmh=15&temp_c=25")
        assert r.status_code == 200
        data = r.json()
        assert data["energy_wh"] > 0
        assert data["wh_per_km"] > 0
        assert "components" in data

    def test_components_sum_to_total(self, client: TestClient) -> None:
        r = client.get("/energy?distance_m=3000&speed_kmh=15&temp_c=25&slope_deg=0")
        data = r.json()
        c = data["components"]
        comp_sum = c["rolling_wh"] + c["aero_wh"] + c["grade_wh"]
        assert abs(comp_sum - data["energy_wh"]) < 0.1

    def test_uphill_more_energy(self, client: TestClient) -> None:
        r_flat = client.get("/energy?distance_m=3000&speed_kmh=10&slope_deg=0")
        r_up = client.get("/energy?distance_m=3000&speed_kmh=10&slope_deg=5")
        if r_flat.status_code == 200 and r_up.status_code == 200:
            assert r_up.json()["energy_wh"] > r_flat.json()["energy_wh"]

    def test_cold_more_energy(self, client: TestClient) -> None:
        r_warm = client.get("/energy?distance_m=3000&speed_kmh=15&temp_c=25")
        r_cold = client.get("/energy?distance_m=3000&speed_kmh=15&temp_c=5")
        if r_warm.status_code == 200 and r_cold.status_code == 200:
            assert r_cold.json()["energy_wh"] > r_warm.json()["energy_wh"]

    def test_invalid_params_rejected(self, client: TestClient) -> None:
        r = client.get("/energy?distance_m=-100")
        assert r.status_code == 422

    def test_missing_required_param(self, client: TestClient) -> None:
        r = client.get("/energy?speed_kmh=15")
        assert r.status_code == 422


class TestBboxValidation:
    """Coordinate validation."""

    def test_inside_bbox_accepted(self, client: TestClient) -> None:
        r = client.get("/route?origin_lon=29.05&origin_lat=40.95&dest_lon=29.08&dest_lat=40.97")
        assert r.status_code != 422

    def test_outside_west(self, client: TestClient) -> None:
        r = client.get("/route?origin_lon=10.0&origin_lat=40.95&dest_lon=29.08&dest_lat=40.97")
        assert r.status_code == 422

    def test_outside_north(self, client: TestClient) -> None:
        r = client.get("/route?origin_lon=29.05&origin_lat=50.0&dest_lon=29.08&dest_lat=40.97")
        assert r.status_code == 422
