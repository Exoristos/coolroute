"""Spatial statistics (P2): Moran's I + Getis-Ord Gi* via PySAL, FDR, hotspots.

Uses the PySAL ``esda`` package (standalone, NOT ``pysal.explore.esda``).
Weights are built via ``libpysal.weights.lat2W`` (efficient for regular raster
grids).  All functions operate on flattened 1-D arrays; a mapping from linear
index → (row, col) → (lat, lon) is maintained for export.

.. note::

   Results depend on the 30 m grid resolution — the Modifiable Areal Unit
   Problem (MAUP) applies: hotspot boundaries and significance may shift at
   coarser / finer resolutions.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from esda import G_Local, Moran  # standalone package, not pysal.explore.esda
from statsmodels.stats.multitest import multipletests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAUP_NOTE: str = (
    "Results depend on the 30 m grid resolution. "
    "Hotspot boundaries and statistical significance are subject to the "
    "Modifiable Areal Unit Problem (MAUP) and may shift at other resolutions. "
    "Gi* is computed on a 10× coarsened grid (300 m) to detect UHI-scale "
    "hotspots; raw 30 m Queen contiguity is too fine for meaningful clusters."
)

_LST_MIN: float = 0.0
_LST_MAX: float = 50.0
"""Valid LST range for analysis (°C).  Outliers exist from unmasked clouds
(-14.9) and hot surfaces (61.8); these are set to NaN before analysis."""

_DEFAULT_COARSEN_FACTOR: int = 10
"""Default factor for Gi* grid coarsening (30 m → 300 m)."""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _filter_lst(data_2d: np.ndarray) -> np.ndarray:
    """Set LST values outside [0, 50] °C to NaN."""
    result = data_2d.copy()
    result[(result < _LST_MIN) | (result > _LST_MAX)] = np.nan
    return result


def _flat_to_grid(idx: int, n_cols: int) -> tuple[int, int]:
    """Convert a flattened (row-major) index to (row, col)."""
    return divmod(idx, n_cols)


def coarsen_grid(data_2d: np.ndarray, factor: int = 10) -> np.ndarray:
    """Block-mean coarsen a 2-D array.

    Reshapes ``(H, W)`` → ``(H//f, f, W//f, f)`` and takes the mean over
    the two factor axes.  Rows/columns at the bottom/right edge that don't fill
    a complete block are trimmed (``H % factor == 0`` and ``W % factor == 0``
    required, or the surplus rows/cols are silently dropped via floor division).

    Parameters
    ----------
    data_2d : np.ndarray  (H, W)
        2-D LST field.
    factor : int
        Coarsening factor (e.g. 10 reduces 30 m → 300 m).

    Returns
    -------
    np.ndarray  (H//factor, W//factor)
        Block-mean coarsened array.
    """
    h, w = data_2d.shape
    h_new = (h // factor) * factor
    w_new = (w // factor) * factor
    trimmed = data_2d[:h_new, :w_new]
    reshaped = trimmed.reshape(h_new // factor, factor, w_new // factor, factor)
    result = np.nanmean(reshaped, axis=(1, 3))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_weights(
    n_rows: int,
    n_cols: int,
    method: str = "queen",
) -> libpysal.weights.W:  # noqa: F821
    """Build spatial weights for a regular raster grid.

    Parameters
    ----------
    n_rows : int
        Number of grid rows (y / latitude).
    n_cols : int
        Number of grid columns (x / longitude).
    method : str
        ``"queen"`` (8 neighbours) or ``"rook"`` (4 neighbours).

    Returns
    -------
    libpysal.weights.W
        Sparse weights matrix, row-standardised (``transform='r'``).
    """
    import libpysal  # noqa: F811

    rook = method.lower() != "queen"
    w = libpysal.weights.lat2W(n_rows, n_cols, rook=rook)
    w.transform = "r"
    return w


def filter_and_dropna(
    data_2d: np.ndarray,
    y_coords: np.ndarray,
    x_coords: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Filter LST to 0–50 °C, drop NaN cells, return valid data + coords.

    Parameters
    ----------
    data_2d : np.ndarray  (n_rows, n_cols)
        2-D LST field (°C).
    y_coords : np.ndarray  (n_rows,)
        Latitude at each row centre.
    x_coords : np.ndarray  (n_cols,)
        Longitude at each column centre.

    Returns
    -------
    valid_flat : np.ndarray  (n_valid,)
        LST values for valid cells, flattened.
    valid_lat : np.ndarray  (n_valid,)
        Latitude of each valid cell.
    valid_lon : np.ndarray  (n_valid,)
        Longitude of each valid cell.
    valid_mask : np.ndarray of bool  (n_rows * n_cols,)
        Mask of valid (non-NaN) cells in flattened (row-major) order.
    """
    filtered = _filter_lst(data_2d)
    flat = filtered.ravel()  # row-major
    valid_mask = ~np.isnan(flat)

    # Build coordinate grids
    Y, X = np.meshgrid(x_coords, y_coords, indexing="ij")  # (n_cols, n_rows)
    lon_flat = X.ravel()  # row-major
    lat_flat = Y.ravel()

    return (
        flat[valid_mask].astype(float),
        lat_flat[valid_mask].astype(float),
        lon_flat[valid_mask].astype(float),
        valid_mask,
    )


def compute_morans_i(
    lst_da: xr.DataArray,
    w: libpysal.weights.W | None = None,  # noqa: F821
    permutations: int = 999,
    random_seed: int = 42,
) -> dict:
    """Global Moran's I on a 2-D LST field.

    Parameters
    ----------
    lst_da : xr.DataArray  (y, x)
        Single-timestep LST grid (°C).
    w : libpysal.weights.W or None
        Spatial weights.  If ``None``, built via :func:`build_weights`
        with Queen contiguity.
    permutations : int
        Monte Carlo permutations for pseudo-p inference.
    random_seed : int
        Seed for reproducible permutations.

    Returns
    -------
    dict
        ``{"I": float, "p_sim": float, "z_sim": float, "sim": np.ndarray,
           "n_valid": int, "n_total": int}``.
    """
    n_rows, n_cols = lst_da.shape
    data_2d = lst_da.values.astype(float)

    valid_flat, valid_lat, valid_lon, valid_mask = filter_and_dropna(
        data_2d, lst_da.y.values, lst_da.x.values
    )

    if len(valid_flat) < 3:
        return {
            "I": np.nan,
            "p_sim": np.nan,
            "z_sim": np.nan,
            "sim": np.array([]),
            "n_valid": len(valid_flat),
            "n_total": n_rows * n_cols,
        }

    # Build weights for valid cells only
    if w is None:
        w_full = build_weights(n_rows, n_cols, method="queen")
    else:
        w_full = w

    # Subset weights to valid cells
    w_subset = subset_weights(w_full, valid_mask)
    if w_subset is None:
        return {
            "I": np.nan,
            "p_sim": np.nan,
            "z_sim": np.nan,
            "sim": np.array([]),
            "n_valid": len(valid_flat),
            "n_total": n_rows * n_cols,
        }

    try:
        rng_state = np.random.get_state()
        np.random.seed(random_seed)
        mi = Moran(valid_flat, w_subset, transformation="r", permutations=permutations)
        np.random.set_state(rng_state)
    except Exception:
        return {
            "I": np.nan,
            "p_sim": np.nan,
            "z_sim": np.nan,
            "sim": np.array([]),
            "n_valid": len(valid_flat),
            "n_total": n_rows * n_cols,
        }

    return {
        "I": float(mi.I),
        "p_sim": float(mi.p_sim),
        "z_sim": float(mi.z_sim),
        "sim": np.asarray(mi.sim, dtype=float),
        "n_valid": len(valid_flat),
        "n_total": n_rows * n_cols,
    }


def compute_gi_star(
    lst_da: xr.DataArray,
    w: libpysal.weights.W | None = None,  # noqa: F821
    permutations: int = 999,
    random_seed: int = 42,
) -> tuple[xr.DataArray, np.ndarray]:
    """Getis-Ord Gi* local hotspot statistic on a 2-D LST field.

    Parameters
    ----------
    lst_da : xr.DataArray  (y, x)
        Single-timestep LST grid (°C).
    w : libpysal.weights.W or None
        Spatial weights.  Built via :func:`build_weights` if ``None``.
    permutations : int
        Monte Carlo permutations.
    random_seed : int
        Reproducibility seed.

    Returns
    -------
    gi_z : xr.DataArray  (y, x)
        Gi* z-scores.  NaN cells (out-of-range LST) remain NaN.
    gi_pvals : np.ndarray  (y, x)
        Gi* pseudo-p-values (not FDR-corrected).  Same NaN mask as *gi_z*.
    """
    import libpysal  # noqa: F811

    n_rows, n_cols = lst_da.shape
    data_2d = lst_da.values.astype(float)

    if w is None:
        w = build_weights(n_rows, n_cols, method="queen")

    # Flatten with NaN for invalid cells
    filtered = _filter_lst(data_2d)
    flat = filtered.ravel()
    valid_mask = ~np.isnan(flat)

    # Subset weights
    w_subset = subset_weights(w, valid_mask)
    if w_subset is None:
        nan_2d = np.full((n_rows, n_cols), np.nan)
        gi_z = xr.DataArray(
            nan_2d.copy(),
            dims=("y", "x"),
            coords={"y": lst_da.y, "x": lst_da.x},
            name="gi_z",
        )
        return gi_z, nan_2d.copy()

    valid_flat = flat[valid_mask].astype(float)

    # Set explicit diagonal weights to silence star=True warning
    libpysal.weights.fill_diagonal(w_subset, 0.5)

    try:
        rng_state = np.random.get_state()
        np.random.seed(random_seed)
        gi = G_Local(valid_flat, w_subset, star=None, permutations=permutations)
        np.random.set_state(rng_state)
        zs_valid = gi.Zs  # z-scores for valid cells only
        ps_valid = gi.p_sim  # raw p-values
    except Exception:
        nan_2d = np.full((n_rows, n_cols), np.nan)
        gi_z = xr.DataArray(
            nan_2d.copy(),
            dims=("y", "x"),
            coords={"y": lst_da.y, "x": lst_da.x},
            name="gi_z",
        )
        return gi_z, nan_2d.copy()

    # Map back to full grid
    def _map_to_2d(vals_1d: np.ndarray) -> np.ndarray:
        arr = np.full(n_rows * n_cols, np.nan)
        arr[valid_mask] = vals_1d
        return arr.reshape(n_rows, n_cols)

    zs_2d = _map_to_2d(zs_valid)
    ps_2d = _map_to_2d(ps_valid)

    gi_z = xr.DataArray(
        zs_2d,
        dims=("y", "x"),
        coords={"y": lst_da.y, "x": lst_da.x},
        name="gi_z",
        attrs={"statistic": "Getis-Ord Gi*", "MAUP_note": MAUP_NOTE},
    )
    return gi_z, ps_2d


def fdr_correct(
    pvals: np.ndarray,
    method: str = "fdr_bh",
) -> np.ndarray:
    """Apply FDR (Benjamini-Hochberg) correction to p-values.

    Parameters
    ----------
    pvals : np.ndarray
        Raw p-values.
    method : str
        Correction method passed to :func:`statsmodels.stats.multitest.multipletests`.

    Returns
    -------
    np.ndarray
        FDR-corrected p-values (same shape as *pvals*).
    """
    mask = np.isfinite(pvals)
    result = np.full_like(pvals, np.nan, dtype=float)
    if mask.sum() == 0:
        return result
    _, corrected, _, _ = multipletests(pvals[mask], method=method)
    result[mask] = corrected
    return result


def extract_hotspots(
    gi_zscores: xr.DataArray,
    pvals: np.ndarray,
    alpha: float = 0.05,
) -> geopandas.GeoDataFrame:  # noqa: F821
    """Extract significant hotspots from Gi* results as a GeoDataFrame.

    Parameters
    ----------
    gi_zscores : xr.DataArray  (y, x)
        Gi* z-scores from :func:`compute_gi_star`.
    pvals : np.ndarray  (y, x)
        FDR-corrected p-values.  Must be 2-D, same shape as *gi_zscores*.
    alpha : float
        Significance level.

    Returns
    -------
    geopandas.GeoDataFrame
        Columns: ``lon``, ``lat``, ``gi_z``, ``pval_fdr``, ``type``,
        ``geometry`` (Point).  CRS: EPSG:4326.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    if pvals.ndim != 2:
        raise ValueError(f"pvals must be 2-D (y, x), got shape {pvals.shape}")

    gz = gi_zscores.values
    significant = (np.isfinite(gz)) & (np.isfinite(pvals)) & (pvals < alpha)

    records: list[dict] = []
    y_coords = gi_zscores.y.values
    x_coords = gi_zscores.x.values

    for row in range(gi_zscores.shape[0]):
        for col in range(gi_zscores.shape[1]):
            if not significant[row, col]:
                continue
            lat = float(y_coords[row])
            lon = float(x_coords[col])
            hotspot_type = "hot" if gz[row, col] > 0 else "cold"
            records.append(
                {
                    "lon": lon,
                    "lat": lat,
                    "gi_z": float(gz[row, col]),
                    "pval_fdr": float(pvals[row, col]),
                    "type": hotspot_type,
                    "geometry": Point(lon, lat),
                }
            )

    if not records:
        return gpd.GeoDataFrame(
            columns=["lon", "lat", "gi_z", "pval_fdr", "type", "geometry"],
            crs="EPSG:4326",
        )

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
    return gdf


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def subset_weights(
    w: libpysal.weights.W,  # noqa: F821
    valid_mask: np.ndarray,
) -> libpysal.weights.W | None:  # noqa: F821
    """Subset spatial weights to only the cells where *valid_mask* is True.

    Parameters
    ----------
    w : libpysal.weights.W
        Full weights matrix (indexed 0..N-1).
    valid_mask : np.ndarray of bool  (N,)
        Which cells are valid.

    Returns
    -------
    libpysal.weights.W or None
        Subset weights, or ``None`` if fewer than 2 valid cells remain.
    """
    import libpysal  # noqa: F811

    valid_idx = np.where(valid_mask)[0]
    if len(valid_idx) < 2:
        return None

    # Map old → new indices
    old_to_new = np.full(len(valid_mask), -1, dtype=int)
    old_to_new[valid_idx] = np.arange(len(valid_idx))

    new_neighbors: dict[int, list[int]] = {}
    for old_id in valid_idx:
        new_id = int(old_to_new[old_id])
        old_neighbors = w.neighbors.get(old_id, [])
        new_neighbors[new_id] = [
            int(old_to_new[n]) for n in old_neighbors if old_to_new[n] >= 0
        ]

    w_subset = libpysal.weights.W(new_neighbors, silence_warnings=True)
    return w_subset
