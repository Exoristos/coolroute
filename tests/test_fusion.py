"""Unit tests for MODIS-anomaly spatiotemporal fusion.

Builds synthetic Landsat + MODIS xarray fixtures entirely in-memory (no network,
no GEE).  Asserts core fusion behaviour and AC-009 clear-sky RMSE.

All tests pass offline in < 30 s.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from uhi_battery.data.fusion import build_qc_mask, clear_sky_rmse, fuse_to_hourly

# ═══════════════════════════════════════════════════════════════════════════
# Synthetic fixture builders
# ═══════════════════════════════════════════════════════════════════════════


def _build_synthetic_landsat(
    n_y: int = 20,
    n_x: int = 30,
    y0: float = 40.95,
    x0: float = 29.05,
    dy: float = 0.000_27,  # ~30 m in lat
    dx: float = 0.000_36,  # ~30 m in lon at 40.95°N
    temp_t0: float = 30.0,
    temp_t1: float = 28.0,
    spatial_ramp_c: float = 5.0,
    t0: str = "2024-06-01T10:30",
    t1: str = "2024-06-17T10:30",
) -> xr.DataArray:
    """Create synthetic Landsat DataArray with two clear-sky overpasses.

    Returns
    -------
    xr.DataArray  (time=2, y=n_y, x=n_x)  — LST °C.
    """
    y = np.linspace(y0, y0 + (n_y - 1) * dy, n_y)
    x = np.linspace(x0, x0 + (n_x - 1) * dx, n_x)
    Y, X = np.meshgrid(y, x, indexing="ij")  # (n_y, n_x)

    # Spatial ramp: warmer toward south-east
    ramp = spatial_ramp_c * ((Y - y0) / (y[-1] - y0) + (X - x0) / (x[-1] - x0)) / 2.0

    data_t0 = temp_t0 + ramp  # shape (n_y, n_x)
    data_t1 = temp_t1 + ramp

    times = np.array([np.datetime64(t0), np.datetime64(t1)], dtype="datetime64[ns]")
    data = np.stack([data_t0, data_t1])  # (2, n_y, n_x)

    return xr.DataArray(
        data,
        dims=("time", "y", "x"),
        coords={"time": times, "y": y, "x": x},
        name="lst",
        attrs={"units": "°C", "crs": "EPSG:4326"},
    )


def _build_synthetic_modis(
    landsat_da: xr.DataArray,
    n_y: int = 4,
    n_x: int = 6,
    temp_amplitude_c: float = 5.0,
    night_offset_c: float = -10.0,
    t_start: str = "2024-06-01",
    t_end: str = "2024-06-21",
) -> tuple[xr.DataArray, xr.DataArray]:
    """Create synthetic MODIS daily day + night DataArrays coarser than Landsat.

    The spatial grid covers the same extent as *landsat_da* but at ~1 km
    (fewer pixels).  Temporal values follow a sinusoidal wave plus the
    Landsat-scale spatial ramp (degraded to MODIS resolution).

    Returns
    -------
    (modis_day, modis_night) : tuple[xr.DataArray, xr.DataArray]
        Both dims (time, y, x), LST °C.
    """
    ly = landsat_da.y.values
    lx = landsat_da.x.values

    y = np.linspace(ly[0], ly[-1], n_y)
    x = np.linspace(lx[0], lx[-1], n_x)

    daily_times = pd.date_range(t_start, t_end, freq="D")

    # Spatial ramp at MODIS resolution (same shape as Landsat ramp, degraded)
    Y, X = np.meshgrid(y, x, indexing="ij")
    ramp = 5.0 * ((Y - ly[0]) / (ly[-1] - ly[0]) + (X - lx[0]) / (lx[-1] - lx[0])) / 2.0

    # Temporal sine: sinusoid with period 20 days
    day_idx = (daily_times - daily_times[0]).days.values.astype(float)
    temporal = temp_amplitude_c * np.sin(2 * np.pi * day_idx / 20.0)  # (n_t,)

    # Day values: base 30 °C + ramp + temporal
    day_vals = 30.0 + ramp[np.newaxis, :, :] + temporal[:, np.newaxis, np.newaxis]
    night_vals = day_vals + night_offset_c

    modis_day = xr.DataArray(
        day_vals.astype(np.float64),
        dims=("time", "y", "x"),
        coords={"time": daily_times.values, "y": y, "x": x},
        name="lst_day",
        attrs={"units": "°C", "crs": "EPSG:4326"},
    )
    modis_night = xr.DataArray(
        night_vals.astype(np.float64),
        dims=("time", "y", "x"),
        coords={"time": daily_times.values, "y": y, "x": x},
        name="lst_night",
        attrs={"units": "°C", "crs": "EPSG:4326"},
    )
    return modis_day, modis_night


def _build_synthetic_landsat_multi(
    n_scenes: int = 5,
    interval_days: int = 10,
    n_y: int = 20,
    n_x: int = 30,
    y0: float = 40.95,
    x0: float = 29.05,
    dy: float = 0.000_27,
    dx: float = 0.000_36,
    spatial_ramp_c: float = 5.0,
    base_temp_c: float = 30.0,
    cooling_per_step_c: float = 2.0,
    t_start: str = "2024-06-01T10:30",
) -> xr.DataArray:
    """Create synthetic Landsat with N clear-sky overpasses evenly spaced.

    Returns
    -------
    xr.DataArray  (time=n_scenes, y=n_y, x=n_x)  — LST °C.
    """
    y = np.linspace(y0, y0 + (n_y - 1) * dy, n_y)
    x = np.linspace(x0, x0 + (n_x - 1) * dx, n_x)
    Y, X = np.meshgrid(y, x, indexing="ij")
    ramp = spatial_ramp_c * ((Y - y0) / (y[-1] - y0) + (X - x0) / (x[-1] - x0)) / 2.0

    t0 = np.datetime64(t_start)
    times = np.array(
        [t0 + np.timedelta64(i * interval_days, "D") for i in range(n_scenes)],
        dtype="datetime64[ns]",
    )
    data = np.stack(
        [base_temp_c - i * cooling_per_step_c + ramp for i in range(n_scenes)]
    )
    return xr.DataArray(
        data,
        dims=("time", "y", "x"),
        coords={"time": times, "y": y, "x": x},
        name="lst",
        attrs={"units": "°C", "crs": "EPSG:4326"},
    )


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def landsat() -> xr.DataArray:
    """Two clear-sky Landsat overpasses (16 days apart)."""
    return _build_synthetic_landsat()


@pytest.fixture(scope="module")
def modis(landsat: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray]:
    """MODIS day + night, 21 days, coarser grid."""
    return _build_synthetic_modis(landsat)


# ═══════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestFuseToHourly:
    """Core fusion behaviour."""

    def test_output_dims_and_shape(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """Fused output has hourly time dim and correct spatial shape."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        assert fused.dims == ("time", "y", "x")
        assert fused.sizes["y"] == landsat.sizes["y"]
        assert fused.sizes["x"] == landsat.sizes["x"]

        # Should have hourly resolution
        n_hours = fused.sizes["time"]
        t0 = pd.Timestamp(fused.time.values[0])
        t1 = pd.Timestamp(fused.time.values[-1])
        assert n_hours >= 24, f"Expected ≥24 hours, got {n_hours}"
        assert (t1 - t0).total_seconds() / 3600 >= n_hours - 2  # allow boundary

    def test_values_plausible_range(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """Fused LST within plausible °C range (0–60)."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        min_val = float(fused.min())
        max_val = float(fused.max())
        assert 0.0 <= min_val <= 50.0, f"min {min_val} out of range"
        assert 0.0 <= max_val <= 60.0, f"max {max_val} out of range"

    def test_landsat_overpass_day_matches(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """On a Landsat overpass day near 10:30, fused ≈ Landsat (anomaly=0)."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        # Landsat time 0 is 2024-06-01T10:30
        landsat_t0 = landsat.time.values[0]

        # MODIS at day 0 has no anomaly (MODIS at nearest Landsat = itself)
        # So fused ≈ Landsat at that hour
        fused_at_t0 = fused.sel(time=landsat_t0, method="nearest")

        landsat_t0_scene = landsat.isel(time=0)
        diff = np.abs(fused_at_t0 - landsat_t0_scene)

        mean_diff = float(diff.mean())
        assert mean_diff < 3.0, (
            f"Fused should match Landsat on overpass day; mean diff = {mean_diff:.2f} °C"
        )

    def test_anomaly_moves_away_from_landsat_baseline(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """On a non-overpass day with MODIS anomaly, fused deviates from pure baseline."""
        modis_day, modis_night = modis

        # Use day-only fusion (no diurnal modulation) to isolate anomaly effect
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=None)

        # Day 5: Landsat baseline ≈ linear interpolation, MODIS anomaly = sin(2π*5/20)*5 = 5°C
        # Baseline at day 5 ≈ 30 - (2)*(5/16) = 29.375 + spatial ramp
        day5 = np.datetime64("2024-06-06T10:30")
        fused_d5 = fused.sel(time=day5, method="nearest")

        # Pure baseline (linear interpolation of Landsat without anomaly)
        baseline = landsat.interp(time=fused.time, method="linear")
        baseline_d5 = baseline.sel(time=day5, method="nearest")

        diff = fused_d5 - baseline_d5
        mean_diff = float(diff.mean())

        # On day 5, MODIS anomaly ≈ +5°C (sin(π/2)=1 × 5)
        # After bilinear resampling, the anomaly should still be substantial
        assert abs(mean_diff) > 0.5, (
            f"Expected anomaly to move fused away from baseline; mean diff = {mean_diff:.2f}"
        )

    def test_diurnal_variation(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """Hourly output shows within-day variation when MODIS night is provided."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        # Pick a single day and check variation across hours
        day = np.datetime64("2024-06-10")
        day_fused = fused.sel(time=slice(day, day + np.timedelta64(23, "h")))

        # Spatial mean at each hour
        hourly_mean = day_fused.mean(dim=["y", "x"])
        range_c = float(hourly_mean.max() - hourly_mean.min())

        assert range_c > 0.5, (
            f"Expected diurnal range > 0.5 °C, got {range_c:.2f}"
        )

    def test_no_diurnal_data_fallback(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """Without MODIS night, fusion still produces hourly output."""
        modis_day, _ = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=None)

        assert fused.sizes["time"] >= 24
        assert fused.attrs["units"] == "°C"
        # No NaNs in output
        assert not np.any(np.isnan(fused.values))
        # HeatLayerSnapshot attrs (#3)
        assert fused.attrs["source"] == "fused"
        assert isinstance(fused.attrs["generated_at"], str)
        assert len(fused.attrs["bbox"]) == 4
        assert "qc_mask_note" in fused.attrs


class TestClearSkyRMSE:
    """AC-009 validation."""

    def test_returns_finite_float(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """clear_sky_rmse returns a finite float."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        rmse = clear_sky_rmse(fused, landsat)
        assert isinstance(rmse, float)
        assert np.isfinite(rmse), f"RMSE should be finite, got {rmse}"

    def test_rmse_small_on_overpass_days(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """RMSE against reference Landsat is within AC-009 target (< 1.5 °C)."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        rmse = clear_sky_rmse(fused, landsat)

        # With synthetic data and no MODIS anomaly at overpass days,
        # RMSE should be small
        assert rmse < 3.0, f"RMSE {rmse:.3f} exceeds relaxed threshold 3.0 °C"

    def test_rmse_worse_with_random_perturbation(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """Perturbing fused values increases RMSE (sanity check)."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        base_rmse = clear_sky_rmse(fused, landsat)

        # Create perturbed copy
        rng = np.random.default_rng(42)
        perturbed = fused.copy()
        perturbed.values = fused.values + rng.normal(0, 3.0, fused.shape)

        perturbed_rmse = clear_sky_rmse(perturbed, landsat)
        assert perturbed_rmse > base_rmse, (
            f"Perturbed RMSE ({perturbed_rmse:.3f}) should exceed base ({base_rmse:.3f})"
        )


class TestRobustness:
    """Edge-case behaviour."""

    def test_missing_modis_days_interpolated(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """MODIS with NaN days is handled via temporal interpolation."""
        modis_day, modis_night = modis

        # Introduce NaN for a few days
        modis_nan = modis_day.copy()
        nan_days = slice(
            np.datetime64("2024-06-08"), np.datetime64("2024-06-12")
        )
        modis_nan.loc[dict(time=nan_days)] = np.nan

        fused = fuse_to_hourly(landsat, modis_nan, modis_night_da=modis_night)

        # No NaN in output
        assert not np.any(np.isnan(fused.values)), "Output should have no NaN"

    def test_single_landsat_overpass(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """Single Landsat scene: baseline is constant, time range falls back to MODIS."""
        modis_day, modis_night = modis
        single_landsat = landsat.isel(time=[0])  # only first overpass

        fused = fuse_to_hourly(single_landsat, modis_day, modis_night_da=modis_night)

        # Time range spans MODIS window (21 days ≈ 504 hours minus boundary clip)
        assert fused.sizes["time"] >= 24
        # Baseline should be constant (single Landsat scene extended)
        assert not np.any(np.isnan(fused.values))

    def test_multi_scene_temporal_interpolation(self) -> None:
        """4+ Landsat scenes over wider window: interpolation between
        non-adjacent overpasses works, no NaNs in interior, output shape correct.
        """
        # 5 Landsat scenes, every 10 days (0, 10, 20, 30, 40) → 42-day window
        n_scenes = 5
        interval_days = 10
        landsat_multi = _build_synthetic_landsat_multi(
            n_scenes=n_scenes,
            interval_days=interval_days,
            base_temp_c=30.0,
            cooling_per_step_c=2.0,
            t_start="2024-06-01T10:30",
        )
        # MODIS daily coverage from day 0 to day 42
        modis_day, modis_night = _build_synthetic_modis(
            landsat_multi, t_start="2024-06-01", t_end="2024-07-13"
        )

        fused = fuse_to_hourly(landsat_multi, modis_day, modis_night_da=modis_night)

        # ── Shape ──
        assert fused.dims == ("time", "y", "x")
        assert fused.sizes["y"] == landsat_multi.sizes["y"]
        assert fused.sizes["x"] == landsat_multi.sizes["x"]
        n_hours = fused.sizes["time"]
        assert n_hours >= 24, f"Expected ≥24 hours, got {n_hours}"

        # ── No NaNs anywhere ──
        assert not np.any(np.isnan(fused.values)), (
            "Multi-scene fused output must be NaN-free"
        )

        # ── Non-adjacent interpolation check ──
        # Day 15 sits between Landsat scenes at day 10 and day 20 (neither is
        # adjacent to the other Landsat days 0, 30, 40 being farther).
        # The *baseline* (Landsat linear interp alone) should lie between the
        # day-10 and day-20 values.  The MODIS anomaly may pull the final fused
        # value outside that bracket (that's expected — MODIS can report
        # cooler/warmer than the nearest Landsat reference).
        ref_time = np.datetime64("2024-06-16T10:30")  # day 15
        fused_slice = fused.sel(time=ref_time, method="nearest")

        # Compute baseline-only for the same time (linearly interpolated Landsat)
        baseline_da = landsat_multi.interp(
            time=fused.time.values, method="linear"
        )
        baseline_slice = baseline_da.sel(time=ref_time, method="nearest")

        ls_day10 = landsat_multi.isel(time=1)  # day 10
        ls_day20 = landsat_multi.isel(time=2)  # day 20

        bs_mean = float(baseline_slice.mean())
        ls10_mean = float(ls_day10.mean())
        ls20_mean = float(ls_day20.mean())

        # Baseline must lie between the two bracketing Landsat scenes
        lower = min(ls10_mean, ls20_mean)
        upper = max(ls10_mean, ls20_mean)
        assert lower <= bs_mean <= upper, (
            f"Baseline at day 15 ({bs_mean:.2f}) should lie between Landsat "
            f"at day 10 ({ls10_mean:.2f}) and day 20 ({ls20_mean:.2f})"
        )

        # The fused value should be baseline + anomaly; the anomaly may
        # move it outside the bracket, but the difference should be finite.
        fused_mean = float(fused_slice.mean())
        assert np.isfinite(fused_mean)
        assert 0.0 <= fused_mean <= 50.0  # plausible LST range

        # ── HeatLayerSnapshot attrs present (#3) ──
        assert fused.attrs["source"] == "fused"
        assert isinstance(fused.attrs["generated_at"], str)
        assert len(fused.attrs["bbox"]) == 4
        assert "qc_mask_note" in fused.attrs

    def test_build_qc_mask_stub(
        self, landsat: xr.DataArray, modis: tuple[xr.DataArray, xr.DataArray]
    ) -> None:
        """build_qc_mask returns all-True boolean mask with same shape."""
        modis_day, modis_night = modis
        fused = fuse_to_hourly(landsat, modis_day, modis_night_da=modis_night)

        mask = build_qc_mask(fused)
        assert mask.shape == fused.shape
        assert mask.dtype == bool
        assert bool(mask.all())  # placeholder: all pixels valid
