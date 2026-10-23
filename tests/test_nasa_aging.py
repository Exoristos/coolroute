"""Tests for NASA PCoE Battery Data Set loader.

Builds synthetic ``.mat`` files via :func:`scipy.io.savemat` that mimic the
NASA nested struct format.  No real NASA files required.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest
from scipy.io import savemat

from uhi_battery.data.nasa_aging import NOMINAL_CAPACITY_AH, load_cell, load_nasa_aging

# ── Synthetic .mat builders ────────────────────────────────────────────────


def _build_nasa_cycle_struct(
    n_cycles: int = 20,
    nominal_ah: float = 2.0,
    base_temp_c: float = 24.0,
    capacity_loss_per_cycle: float = 0.005,
    discharge_every: int = 1,
    rng_seed: int = 42,
) -> np.ndarray:
    """Build a 1×N structured array mimicking NASA ``cycle`` struct.

    Parameters
    ----------
    n_cycles : int
        Total number of cycles.
    nominal_ah : float
        Starting capacity (Ah).
    base_temp_c : float
        Base temperature (°C), with small per-cycle noise.
    capacity_loss_per_cycle : float
        Capacity drop per discharge cycle (Ah).
    discharge_every : int
        Every Nth cycle is a discharge (others are 'impedance' or 'charge').
    rng_seed : int
        Seed for reproducibility.

    Returns
    -------
    np.ndarray  shape (1, n_cycles), dtype=[('type','O'), ('data','O')]
    """
    rng = np.random.default_rng(rng_seed)

    # Dtype for the data sub-struct inside each cycle
    data_dtype = np.dtype(
        [
            ("Temperature_measured", "O"),
            ("Capacity", "O"),
            ("Voltage_measured", "O"),
            ("Current_measured", "O"),
        ]
    )

    cycle_dtype = np.dtype(
        [
            ("type", "O"),
            ("ambient_temperature", "O"),
            ("time", "O"),
            ("data", "O"),
        ]
    )

    cycles = np.empty((1, n_cycles), dtype=cycle_dtype)
    discharge_counter = 0

    for i in range(n_cycles):
        ctype = "impedance"
        if discharge_every > 0 and (i % discharge_every == 0):
            ctype = "discharge"
        discharge_counter += 1 if ctype == "discharge" else 0

        temp = base_temp_c + rng.uniform(-1.0, 3.0)
        capacity = nominal_ah - discharge_counter * capacity_loss_per_cycle
        capacity = max(capacity, 0.0)

        # Build data sub-struct (1×1)
        data_item = np.empty((1, 1), dtype=data_dtype)
        data_item[0, 0] = (
            np.array([[temp]]),
            np.array([[capacity]]),
            np.array([[3.7]]),
            np.array([[2.0]]),
        )

        cycles[0, i] = (
            np.array([ctype]),
            np.array([[base_temp_c]]),
            np.array([[float(i * 3600)]]),
            data_item,
        )

    return cycles


def _write_synthetic_mat(path: str | Path, cell_name: str = "B0005", **kw: object) -> Path:
    """Write a synthetic NASA-style .mat file to *path*."""
    path = Path(path)
    cycles = _build_nasa_cycle_struct(**kw)  # type: ignore[arg-type]

    # Wrap in outer struct: data['B0005'] is 1×1 struct with field 'cycle'
    outer_dtype = np.dtype([("cycle", "O")])
    outer = np.empty((1, 1), dtype=outer_dtype)
    outer[0, 0] = (cycles,)

    savemat(str(path), {cell_name: outer})
    return path


# ── Tests ──────────────────────────────────────────────────────────────────


class TestLoadCell:
    """Unit tests for ``load_cell``."""

    def test_extracts_discharge_cycles(self) -> None:
        """Discharge cycles are correctly counted and parsed."""
        with tempfile.TemporaryDirectory() as td:
            mat_path = _write_synthetic_mat(
                Path(td) / "test_cell.mat", n_cycles=20, discharge_every=2
            )
            df = load_cell(mat_path)

        # Every 2nd of 20 cycles → 10 discharge
        assert len(df) == 10
        assert list(df.columns) == ["cycle_idx", "temperature_measured", "capacity_Ah"]

    def test_cycle_idx_is_1_based(self) -> None:
        """First discharge cycle has cycle_idx >= 1."""
        with tempfile.TemporaryDirectory() as td:
            mat_path = _write_synthetic_mat(
                Path(td) / "test_cell.mat", n_cycles=5, discharge_every=1
            )
            df = load_cell(mat_path)

        assert df["cycle_idx"].iloc[0] == 1

    def test_temperature_values_plausible(self) -> None:
        """Temperature values are within realistic range (0–50 °C)."""
        with tempfile.TemporaryDirectory() as td:
            mat_path = _write_synthetic_mat(
                Path(td) / "test_cell.mat", n_cycles=30, base_temp_c=24.0
            )
            df = load_cell(mat_path)

        assert df["temperature_measured"].between(0, 50).all()

    def test_capacity_decreases_over_cycles(self) -> None:
        """Capacity degrades monotonically with increasing cycle count."""
        with tempfile.TemporaryDirectory() as td:
            mat_path = _write_synthetic_mat(
                Path(td) / "test_cell.mat", n_cycles=30, capacity_loss_per_cycle=0.01
            )
            df = load_cell(mat_path)

        caps = df.sort_values("cycle_idx")["capacity_Ah"].values
        # Should be non-increasing (allow small float noise)
        assert all(caps[i] >= caps[i + 1] - 1e-9 for i in range(len(caps) - 1))

    def test_skips_impedance_cycles(self) -> None:
        """Impedance/charge cycles are excluded from output."""
        with tempfile.TemporaryDirectory() as td:
            # 50 cycles, only 1st is discharge, rest are impedance
            mat_path = _write_synthetic_mat(
                Path(td) / "test_cell.mat", n_cycles=50, discharge_every=50
            )
            df = load_cell(mat_path)

        assert len(df) == 1
        assert df["cycle_idx"].iloc[0] == 1


class TestLoadNasaAging:
    """Unit tests for ``load_nasa_aging`` (directory-level loader)."""

    def test_loads_multiple_cells(self) -> None:
        """Aggregates multiple .mat files into a single tidy DataFrame."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_synthetic_mat(tdp / "B0005.mat", cell_name="B0005", n_cycles=10)
            _write_synthetic_mat(tdp / "B0006.mat", cell_name="B0006", n_cycles=10)

            df = load_nasa_aging(td)

        assert set(df["cell_id"].unique()) == {"B0005", "B0006"}
        assert list(df.columns) == [
            "cell_id",
            "temp_C",
            "cycles",
            "capacity_Ah",
            "capacity_retention_pct",
            "source",
        ]
        assert (df["source"] == "NASA_PCoE").all()

    def test_retention_pct_calculation(self) -> None:
        """capacity_retention_pct = capacity_Ah / 2.0 * 100."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_synthetic_mat(
                tdp / "B0007.mat",
                cell_name="B0007",
                n_cycles=5,
                nominal_ah=NOMINAL_CAPACITY_AH,
                capacity_loss_per_cycle=0.0,
            )
            df = load_nasa_aging(td)

        # Perfect capacity → 100 % retention
        assert (df["capacity_retention_pct"] > 99.0).all()
        assert (df["capacity_retention_pct"] <= 100.1).all()

    def test_missing_dir_raises_file_not_found(self) -> None:
        """Clear error with download URL when directory is missing."""
        with pytest.raises(FileNotFoundError, match="NASA battery data"):
            load_nasa_aging("/nonexistent/nasa_dir_xyz_123")

    def test_empty_dir_raises_file_not_found(self) -> None:
        """Clear error when no B00*.mat files exist."""
        with tempfile.TemporaryDirectory() as td:
            with pytest.raises(FileNotFoundError, match="No B00"):
                load_nasa_aging(td)

    def test_skips_empty_cells(self) -> None:
        """If one .mat has no discharge cycles, others still load."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Cell #1: all impedance, no discharge
            _write_synthetic_mat(
                tdp / "B0008.mat",
                cell_name="B0008",
                n_cycles=10,
                discharge_every=0,  # no discharge cycles at all
            )
            # Cell #2: discharges present
            _write_synthetic_mat(
                tdp / "B0009.mat",
                cell_name="B0009",
                n_cycles=10,
                discharge_every=1,
            )
            df = load_nasa_aging(td)

        assert df["cell_id"].unique().tolist() == ["B0009"]


class TestNominalConstant:
    """Verify the nominal capacity constant."""

    def test_nominal_is_2_ah(self) -> None:
        assert NOMINAL_CAPACITY_AH == 2.0
