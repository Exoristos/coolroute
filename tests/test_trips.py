"""Tests for simulated trip generator.

All OSMnx calls are monkeypatched — no network required.  Tests assert:
* Determinism (same seed → identical trips)
* Distance distribution (median 2–5 km, p90 < 10 km)
* Trip schema columns
* ``load_trips(source="ibb")`` raises NotImplementedError
* ``source="operator"`` raises NotImplementedError
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from geopandas import GeoDataFrame
from shapely.geometry import Point

from uhi_battery.data import trips as trips_mod
from uhi_battery.data.trips import load_trips

# ── Synthetic POI fixture (monkeypatch target) ─────────────────────────────


def _synthetic_pois(
    n: int = 80,
    bbox: tuple[float, float, float, float] = (29.00, 40.90, 29.13, 40.99),
    seed: int = 1,
) -> GeoDataFrame:
    """Build a GeoDataFrame of synthetic POI points within *bbox*.

    Enough density that the distance sampler can reliably hit its
    distribution targets.
    """
    rng = np.random.default_rng(seed)
    west, south, east, north = bbox
    lons = rng.uniform(west, east, n)
    lats = rng.uniform(south, north, n)
    points = [Point(x, y) for x, y in zip(lons, lats, strict=False)]
    return GeoDataFrame({"geometry": points}, geometry="geometry", crs="EPSG:4326")


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_osmnx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace osmnx helpers with synthetic data for all tests in this module."""
    bbox_for_pois = (29.00, 40.90, 29.13, 40.99)

    def _fake_pois(bbox: tuple[float, float, float, float]) -> GeoDataFrame:
        return _synthetic_pois(bbox=bbox_for_pois)

    def _fake_network(bbox: tuple[float, float, float, float]) -> object:
        return None  # not used by trip generator

    monkeypatch.setattr(trips_mod, "_fetch_pois", _fake_pois)
    monkeypatch.setattr(trips_mod, "_fetch_network", _fake_network)


# ── Tests ──────────────────────────────────────────────────────────────────


class TestDeterminism:
    """Same seed → byte-identical output."""

    def test_same_seed_same_trips(self) -> None:
        trips1 = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=100,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        trips2 = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=100,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        # Compare non-geometry columns
        cols = ["trip_id", "datetime", "distance_m", "est_speed"]
        pd.testing.assert_frame_equal(trips1[cols], trips2[cols])
        # Compare geometry WKT
        assert trips1["origin"].equals(trips2["origin"])
        assert trips1["destination"].equals(trips2["destination"])

    def test_different_seed_different_trips(self) -> None:
        trips1 = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=100,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        trips2 = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=100,
            start="2024-06-01",
            end="2024-07-01",
            seed=99,
        )
        # At least one trip should differ in origin or distance
        dists_same = (trips1["distance_m"].values == trips2["distance_m"].values).all()
        origin_same = trips1["origin"].equals(trips2["origin"])
        assert not (dists_same and origin_same), (
            "Expected different seeds to produce different trips, but they were identical"
        )


class TestDistanceDistribution:
    """R4 guarantees: median 2–5 km, p90 < 10 km."""

    def test_median_in_range(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=200,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        median_km = trips["distance_m"].median() / 1000
        assert 2.0 <= median_km <= 5.0, f"Median {median_km:.1f} km not in [2, 5] km"

    def test_p90_under_10km(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=200,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        p90_km = np.percentile(trips["distance_m"], 90) / 1000
        assert p90_km < 10.0, f"p90 {p90_km:.1f} km exceeds 10 km threshold"

    def test_no_zero_distance_trips(self) -> None:
        """Origin and destination are always distinct POIs."""
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=200,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        assert (trips["distance_m"] > 0).all(), "Found zero-distance trip(s)"


class TestTripSchema:
    """Output columns match the Trip schema."""

    REQUIRED_COLS = [
        "trip_id",
        "origin",
        "destination",
        "path",
        "datetime",
        "distance_m",
        "est_speed",
    ]

    def test_all_columns_present(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=10,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        for col in self.REQUIRED_COLS:
            assert col in trips.columns, f"Missing column: {col}"

    def test_geometry_column_is_origin(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=10,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        assert trips.geometry.name == "origin"

    def test_crs_is_4326(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=10,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        assert trips.crs is not None
        assert trips.crs.to_epsg() == 4326

    def test_path_is_linestring(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=10,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        from shapely.geometry import LineString

        assert all(isinstance(p, LineString) for p in trips["path"])

    def test_datetime_within_window(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=10,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        t0 = pd.Timestamp("2024-06-01")
        t1 = pd.Timestamp("2024-07-01")
        assert (trips["datetime"] >= t0).all()
        assert (trips["datetime"] <= t1).all()

    def test_est_speed_plausible(self) -> None:
        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=50,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        # e-scooter speeds between 5–30 km/h
        assert trips["est_speed"].between(5, 30).all()

    def test_saveable_to_parquet(self, tmp_path: Path) -> None:
        """Trips can be round-tripped through parquet; all three geometry columns survive."""
        import geopandas as gpd
        from shapely.geometry import LineString, Point

        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=10,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        pq_path = tmp_path / "trips.parquet"
        trips.to_parquet(pq_path, index=False)

        reloaded = gpd.read_parquet(pq_path)
        assert len(reloaded) == 10
        assert set(self.REQUIRED_COLS).issubset(reloaded.columns)

        # Active geometry is origin (Point)
        assert reloaded.geometry.name == "origin"
        assert all(isinstance(g, Point) for g in reloaded.geometry)

        # destination must be Point geometry
        assert all(isinstance(g, Point) for g in reloaded["destination"])

        # path must be LineString geometry
        assert all(isinstance(g, LineString) for g in reloaded["path"])


class TestLoadTripsDispatcher:
    """Provider routing behaviour."""

    def test_simulation_source_works(self) -> None:
        trips = load_trips(
            source="simulation",
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=10,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        assert len(trips) == 10
        assert "distance_m" in trips.columns

    def test_ibb_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="ibb.*e-scooter"):
            load_trips(source="ibb")

    def test_operator_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="operator.*Martı"):
            load_trips(source="operator")

    def test_unknown_source_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown trip source"):
            load_trips(source="garbage")


class TestVolumeCalibration:
    """n_trips parameter is respected."""

    def test_exact_trip_count(self) -> None:
        for n in [1, 10, 50, 200]:
            trips = trips_mod.simulate_trips(
                bbox=(29.00, 40.90, 29.13, 40.99),
                n_trips=n,
                start="2024-06-01",
                end="2024-07-01",
                seed=42,
            )
            assert len(trips) == n, f"Expected {n} trips, got {len(trips)}"


class TestSparsePOIGracefulDegradation:
    """#9: Sparse POI case — simulator skips gracefully, no crashes, no tiny trips."""

    def test_few_pois_no_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With only 3 POIs, simulate_trips returns without exception."""
        sparse = _synthetic_pois(n=3, seed=7)
        monkeypatch.setattr(trips_mod, "_fetch_pois", lambda _bbox: sparse)

        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=20,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        # Must not crash; may return fewer trips
        assert len(trips) >= 0

    def test_all_returned_trips_have_valid_distance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every returned trip has distance >= 500 m (min-distance fallback guarantee)."""
        sparse = _synthetic_pois(n=3, seed=7)
        monkeypatch.setattr(trips_mod, "_fetch_pois", lambda _bbox: sparse)

        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=20,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        if len(trips) > 0:
            assert (trips["distance_m"] >= 500).all(), (
                "All returned trips must have distance >= 500 m"
            )

    def test_may_skip_trips_with_sparse_pois(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With sparse POIs, the number of returned trips may be less than requested."""
        sparse = _synthetic_pois(n=3, seed=7)
        monkeypatch.setattr(trips_mod, "_fetch_pois", lambda _bbox: sparse)

        trips = trips_mod.simulate_trips(
            bbox=(29.00, 40.90, 29.13, 40.99),
            n_trips=20,
            start="2024-06-01",
            end="2024-07-01",
            seed=42,
        )
        # Either all 20 (if POIs happen to be far enough apart for fallback)
        # or fewer if some trips couldn't find a destination >= 500m.
        # The key: no exception, no zero-distance trips.
        assert len(trips) <= 20
        if len(trips) > 0:
            assert (trips["distance_m"] > 0).all()
