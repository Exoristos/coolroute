"""Unit tests for P2 spatial statistics module.

All tests use synthetic in-memory data — no real zarr needed.
"""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from uhi_battery.stats.spatial import (
    _filter_lst,
    build_weights,
    coarsen_grid,
    compute_gi_star,
    compute_morans_i,
    extract_hotspots,
    fdr_correct,
    filter_and_dropna,
)

# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def grid_5x5() -> xr.DataArray:
    """5×5 LST grid with a spatial gradient (positive autocorrelation)."""
    y = np.linspace(41.0, 40.9, 5)
    x = np.linspace(29.0, 29.1, 5)
    Y, X = np.meshgrid(y, x, indexing="ij")
    data = 30.0 + 3.0 * ((Y - y[0]) / (y[-1] - y[0])) + 2.0 * ((X - x[0]) / (x[-1] - x[0]))
    return xr.DataArray(
        data, dims=("y", "x"), coords={"y": y, "x": x}, name="lst"
    )


@pytest.fixture(scope="module")
def grid_random_5x5() -> xr.DataArray:
    """5×5 LST grid with random values (no autocorrelation)."""
    rng = np.random.default_rng(42)
    data = rng.uniform(25, 35, (5, 5))
    y = np.linspace(41.0, 40.9, 5)
    x = np.linspace(29.0, 29.1, 5)
    return xr.DataArray(
        data, dims=("y", "x"), coords={"y": y, "x": x}, name="lst"
    )


@pytest.fixture(scope="module")
def grid_with_outliers() -> xr.DataArray:
    """5×5 grid with some out-of-range values (-5, 60, NaN)."""
    y = np.linspace(41.0, 40.9, 5)
    x = np.linspace(29.0, 29.1, 5)
    data = np.full((5, 5), 30.0)
    data[0, 0] = -5.0  # below 0
    data[1, 2] = 60.0  # above 50
    data[3, 3] = np.nan  # already NaN
    return xr.DataArray(
        data, dims=("y", "x"), coords={"y": y, "x": x}, name="lst"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Tests — build_weights
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildWeights:
    def test_queen_5x5_center_has_8_neighbors(self) -> None:
        """Center cell of 5×5 Queen grid has 8 neighbours."""
        w = build_weights(5, 5, method="queen")
        center_idx = 12  # row 2, col 2
        assert len(w.neighbors[center_idx]) == 8

    def test_queen_5x5_corner_has_3_neighbors(self) -> None:
        """Corner cell of 5×5 Queen grid has 3 neighbours."""
        w = build_weights(5, 5, method="queen")
        corner_idx = 0  # row 0, col 0
        assert len(w.neighbors[corner_idx]) == 3

    def test_queen_5x5_edge_has_5_neighbors(self) -> None:
        """Edge (non-corner) cell of 5×5 Queen grid has 5 neighbours."""
        w = build_weights(5, 5, method="queen")
        edge_idx = 2  # row 0, col 2
        assert len(w.neighbors[edge_idx]) == 5

    def test_rook_5x5_center_has_4_neighbors(self) -> None:
        """Center cell of 5×5 Rook grid has 4 neighbours."""
        w = build_weights(5, 5, method="rook")
        center_idx = 12
        assert len(w.neighbors[center_idx]) == 4

    def test_weights_are_row_standardised(self) -> None:
        """Weights are row-standardised (sum to 1 per row)."""
        w = build_weights(5, 5, method="queen")
        row_sums = w.sparse.sum(axis=1)
        # For non-island cells, row sum ≈ 1
        assert np.allclose(row_sums[row_sums > 0], 1.0)


# ═══════════════════════════════════════════════════════════════════════════
# Tests — LST filtering
# ═══════════════════════════════════════════════════════════════════════════


class TestLSTFiltering:
    def test_outliers_set_to_nan(self, grid_with_outliers: xr.DataArray) -> None:
        """Values <0 or >50 are set to NaN."""
        filtered = _filter_lst(grid_with_outliers.values)
        assert np.isnan(filtered[0, 0])  # was -5
        assert np.isnan(filtered[1, 2])  # was 60
        assert np.isnan(filtered[3, 3])  # was NaN
        assert filtered[4, 4] == 30.0  # valid value kept

    def test_filter_and_dropna_removes_invalid(
        self, grid_with_outliers: xr.DataArray
    ) -> None:
        """filter_and_dropna returns only valid cells."""
        valid, lat, lon, mask = filter_and_dropna(
            grid_with_outliers.values,
            grid_with_outliers.y.values,
            grid_with_outliers.x.values,
        )
        n_total = 25
        # 3 invalid cells
        assert len(valid) == n_total - 3
        assert len(lat) == n_total - 3
        assert len(lon) == n_total - 3
        assert mask.sum() == n_total - 3
        assert np.all(valid == 30.0)


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Moran's I
# ═══════════════════════════════════════════════════════════════════════════


class TestMoransI:
    def test_positive_autocorrelation(self, grid_5x5: xr.DataArray) -> None:
        """Spatial gradient produces I > 0 with p < 0.05."""
        result = compute_morans_i(grid_5x5, permutations=99, random_seed=42)
        assert result["I"] > 0.0, f"Expected I > 0, got {result['I']}"
        assert result["p_sim"] < 0.05, (
            f"Expected significant (p < 0.05), got p={result['p_sim']}"
        )
        assert np.isfinite(result["z_sim"])

    def test_random_no_autocorrelation(
        self, grid_random_5x5: xr.DataArray
    ) -> None:
        """Random data shows I ≈ 0 (p > 0.05 typically)."""
        result = compute_morans_i(grid_random_5x5, permutations=99, random_seed=42)
        # Should be close to zero (allow p > 0.05)
        assert abs(result["I"]) < 0.5, f"Expected I near 0, got {result['I']}"

    def test_returns_expected_keys(self, grid_5x5: xr.DataArray) -> None:
        """Result dict has all expected keys."""
        result = compute_morans_i(grid_5x5, permutations=99, random_seed=42)
        for key in ("I", "p_sim", "z_sim", "sim", "n_valid", "n_total"):
            assert key in result, f"Missing key: {key}"

    def test_all_nan_returns_nan(self) -> None:
        """All-NaN grid returns NaN I without crashing."""
        y = np.array([41.0, 40.95])
        x = np.array([29.0, 29.05])
        data = np.full((2, 2), np.nan)
        da = xr.DataArray(data, dims=("y", "x"), coords={"y": y, "x": x})
        result = compute_morans_i(da, permutations=99)
        assert np.isnan(result["I"])


# ═══════════════════════════════════════════════════════════════════════════
# Tests — Getis-Ord Gi*
# ═══════════════════════════════════════════════════════════════════════════


class TestGiStar:
    def test_returns_correct_shape(self, grid_5x5: xr.DataArray) -> None:
        """Gi* output matches input grid shape."""
        gi, _p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        assert gi.shape == grid_5x5.shape
        assert gi.dims == ("y", "x")

    def test_hot_spot_in_warm_region(self, grid_5x5: xr.DataArray) -> None:
        """Warm region (south-east) has positive z-scores."""
        gi, _p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        # The south-east corner (last row, last col) is warmest
        # Gi* should be positive there (hot spot)
        corner_z = float(gi.values[-1, -1])
        assert corner_z > 0, f"Expected positive Gi* at warm corner, got {corner_z:.2f}"

    def test_all_nan_returns_nan_grid(self) -> None:
        """All-NaN grid returns NaN grid."""
        y = np.array([41.0, 40.95])
        x = np.array([29.0, 29.05])
        data = np.full((2, 2), np.nan)
        da = xr.DataArray(data, dims=("y", "x"), coords={"y": y, "x": x})
        gi, p = compute_gi_star(da, permutations=99)
        assert np.all(np.isnan(gi.values))
        assert np.all(np.isnan(p))

    def test_has_maup_note_attr(self, grid_5x5: xr.DataArray) -> None:
        """Output has MAUP_note attribute."""
        gi, _p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        assert "MAUP_note" in gi.attrs

    def test_returns_pvalues_array(self, grid_5x5: xr.DataArray) -> None:
        """P-values array has same shape and finite values."""
        gi, p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        assert p.shape == gi.shape
        # At least some valid p-values should be finite
        valid_mask = np.isfinite(gi.values)
        assert np.any(np.isfinite(p[valid_mask]))


# ═══════════════════════════════════════════════════════════════════════════
# Tests — FDR correction
# ═══════════════════════════════════════════════════════════════════════════


class TestFDR:
    def test_reduces_significance(self) -> None:
        """FDR corrected p-values are ≥ original."""
        pvals = np.array([0.001, 0.01, 0.02, 0.05, 0.5])
        corrected = fdr_correct(pvals)
        assert np.all(corrected >= pvals)

    def test_nan_handling(self) -> None:
        """NaN in p-values is preserved."""
        pvals = np.array([0.001, np.nan, 0.05])
        corrected = fdr_correct(pvals)
        assert np.isnan(corrected[1])
        assert corrected[0] >= 0.001

    def test_all_nan_returns_nan(self) -> None:
        """All-NaN input returns all-NaN."""
        pvals = np.full(5, np.nan)
        corrected = fdr_correct(pvals)
        assert np.all(np.isnan(corrected))


# ═══════════════════════════════════════════════════════════════════════════
# Tests — extract_hotspots
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractHotspots:
    def test_returns_geodataframe(self, grid_5x5: xr.DataArray) -> None:
        """Returns a GeoDataFrame with expected columns."""
        gi, _p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        pvals = np.full(gi.shape, 0.01)  # all highly significant (2-D)
        gdf = extract_hotspots(gi, pvals, alpha=0.05)
        assert "type" in gdf.columns
        assert "gi_z" in gdf.columns
        assert "geometry" in gdf.columns

    def test_filters_by_alpha(self, grid_5x5: xr.DataArray) -> None:
        """alpha=0.05 retains only significant cells (p < 0.05)."""
        gi, _p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        pvals = np.full(gi.shape, 0.01)  # all significant
        gdf = extract_hotspots(gi, pvals, alpha=0.05)
        assert len(gdf) > 0  # some hotspots should exist

    def test_types_are_hot_or_cold(self, grid_5x5: xr.DataArray) -> None:
        """All rows have type 'hot' or 'cold'."""
        gi, _p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        pvals = np.full(gi.shape, 0.01)
        gdf = extract_hotspots(gi, pvals, alpha=0.05)
        assert set(gdf["type"].unique()).issubset({"hot", "cold"})

    def test_alpha_1e_10_returns_empty(self, grid_5x5: xr.DataArray) -> None:
        """Very strict alpha returns empty GeoDataFrame."""
        gi, _p = compute_gi_star(grid_5x5, permutations=99, random_seed=42)
        pvals = np.full(gi.shape, 0.01)
        gdf = extract_hotspots(gi, pvals, alpha=1e-10)
        assert len(gdf) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Tests — coarsen_grid
# ═══════════════════════════════════════════════════════════════════════════


class TestCoarsenGrid:
    def test_factor_2_reduces_shape(self) -> None:
        """10×10 → 5×5 with factor=2."""
        data = np.arange(100, dtype=float).reshape(10, 10)
        coarse = coarsen_grid(data, factor=2)
        assert coarse.shape == (5, 5)

    def test_factor_2_block_mean(self) -> None:
        """Block mean gives correct values for constant data."""
        data = np.full((10, 10), 7.0)
        coarse = coarsen_grid(data, factor=2)
        assert np.allclose(coarse, 7.0)

    def test_trims_surplus_edges(self) -> None:
        """Surplus rows/cols are silently dropped."""
        data = np.ones((11, 11))
        coarse = coarsen_grid(data, factor=2)
        assert coarse.shape == (5, 5)  # 11//2 = 5

    def test_handles_nan(self) -> None:
        """NaN values are handled via nanmean."""
        data = np.full((10, 10), 10.0)
        data[0, 0] = np.nan
        data[0, 1] = np.nan
        coarse = coarsen_grid(data, factor=2)
        # The top-left 2×2 block has 2 NaN + 2 10.0 → mean = 10.0
        assert np.isclose(coarse[0, 0], 10.0)
