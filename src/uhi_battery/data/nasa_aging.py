"""NASA PCoE Battery Data Set loader → tidy DataFrame.

Loads ``.mat`` files (MATLAB v7) from the NASA Prognostics Center of Excellence
Battery Data Set.  Parses per-cycle discharge capacity and temperature for
Arrhenius-type State-of-Health (SoH) calibration.

Nominal capacity: 2.0 Ah (LCO 18650 cells, tested at 4/24/43 °C).

.. note::

    **``dwell_time_h`` is NOT available** from the NASA PCoE dataset.
    The dataset records calendar-age degradation implicitly through cycle
    count but does not provide explicit dwell-time (calendar-aging) data.
    Downstream SoH models that require ``dwell_time_h`` should treat it as
    missing or derive a proxy (e.g. from cycle interval times if available
    from the raw time vectors, though this is not standardised across cells).

.. _NASA PCoE download:
   https://phm-datasets.s3.amazonaws.com/NASA/5.+Battery+Data+Set.zip
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.io import loadmat

# Nominal capacity for NASA PCoE Li-ion 18650 cells (Ah).
NOMINAL_CAPACITY_AH = 2.0

# Download URL displayed when .mat files are missing.
_NASA_DOWNLOAD_URL = (
    "https://phm-datasets.s3.amazonaws.com/NASA/5.+Battery+Data+Set.zip"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_str(val: object) -> str:
    """Recursively extract a string from arbitrarily nested numpy arrays.

    Handles ``np.array(['discharge'])``, ``np.array([['discharge']])``, raw
    ``bytes``, and plain ``str``.
    """
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (bytes, np.bytes_)):
        return val.decode("utf-8", errors="replace").strip()
    if isinstance(val, np.ndarray):
        if val.size == 0:
            return ""
        return _extract_str(val.flat[0])
    return str(val).strip()


def _extract_float(val: object) -> float:
    """Recursively extract a float from arbitrarily nested numpy arrays.

    Returns ``np.nan`` for empty arrays or unparseable values.
    """
    if isinstance(val, (float, int, np.floating, np.integer)):
        return float(val)
    if isinstance(val, np.ndarray):
        if val.size == 0:
            return float("nan")
        return _extract_float(val.flat[0])
    try:
        return float(val)
    except (ValueError, TypeError):
        return float("nan")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_cell(mat_path: str | Path) -> pd.DataFrame:
    """Parse a single NASA battery ``.mat`` file → per-cycle DataFrame.

    Iterates the nested ``cycle`` struct array, keeps **discharge** cycles,
    and extracts for each cycle::

        cycle_idx   — 1-based cycle number
        temperature_measured  — °C (thermocouple reading during discharge)
        capacity_Ah — measured discharge capacity (Ah)

    Parameters
    ----------
    mat_path : str | Path
        Path to a NASA ``.mat`` file (e.g. ``B0005.mat``).

    Returns
    -------
    pd.DataFrame
        Columns: ``[cycle_idx, temperature_measured, capacity_Ah]``.
    """
    mat_path = Path(mat_path)
    data = loadmat(str(mat_path))

    # Find the cell key (skip MATLAB metadata keys)
    cell_keys = [k for k in data if not k.startswith("__")]
    if not cell_keys:
        raise ValueError(f"No cell struct found in {mat_path.name}")
    cell_key = cell_keys[0]

    cell = data[cell_key]
    if not isinstance(cell, np.ndarray) or cell.size == 0:
        raise ValueError(f"Empty cell struct in {mat_path.name}")

    struct = cell[0, 0]
    try:
        cycles = struct["cycle"]
    except (KeyError, IndexError, ValueError) as exc:
        raise KeyError(
            f"No 'cycle' field in {mat_path.name} — not a NASA battery .mat file?"
        ) from exc

    n = int(cycles.shape[1]) if cycles.ndim >= 2 else 0
    records: list[dict[str, object]] = []

    for i in range(n):
        cycle = cycles[0, i]
        try:
            ctype = _extract_str(cycle["type"])
        except (KeyError, IndexError):
            continue

        if "discharge" not in ctype.lower():
            continue  # skip charge / impedance cycles

        try:
            cycle_data = cycle["data"][0, 0]
        except (KeyError, IndexError, ValueError):
            continue  # malformed cycle — no data sub-struct

        try:
            temp = _extract_float(cycle_data["Temperature_measured"])
        except (KeyError, IndexError, ValueError):
            temp = float("nan")
        try:
            capacity = _extract_float(cycle_data["Capacity"])
        except (KeyError, IndexError, ValueError):
            capacity = float("nan")

        records.append(
            {
                "cycle_idx": i + 1,  # 1-based as in NASA documentation
                "temperature_measured": temp,
                "capacity_Ah": capacity,
            }
        )

    return pd.DataFrame(records)


def load_nasa_aging(mat_dir: str | Path) -> pd.DataFrame:
    """Load all NASA battery ``.mat`` files from a directory.

    Scans for ``B00*.mat`` files (e.g. ``B0005.mat``), parses each via
    :func:`load_cell`, and returns a tidy DataFrame conforming to the
    ``SoHAgingPoint`` schema (§4 spec).

    .. note::

        ``dwell_time_h`` is **not available** in the NASA PCoE dataset.
        Downstream consumers should treat this field as missing for
        NASA-sourced rows.

    Parameters
    ----------
    mat_dir : str | Path
        Directory containing unpackaged NASA Battery Data Set ``.mat`` files.

    Returns
    -------
    pd.DataFrame
        Columns: ``[cell_id, temp_C, cycles, capacity_Ah,
        capacity_retention_pct, source]``.
    """
    mat_dir = Path(mat_dir)
    if not mat_dir.exists() or not mat_dir.is_dir():
        raise FileNotFoundError(
            f"NASA battery data directory not found: {mat_dir}\n"
            f"Download and extract from: {_NASA_DOWNLOAD_URL}\n"
            f"Place ``.mat`` files in: {mat_dir.resolve()}"
        )

    mat_files = sorted(mat_dir.glob("B00*.mat"))
    if not mat_files:
        raise FileNotFoundError(
            f"No B00*.mat files found in {mat_dir.resolve()}\n"
            f"Download from: {_NASA_DOWNLOAD_URL}"
        )

    frames: list[pd.DataFrame] = []
    for mp in mat_files:
        cell_id = mp.stem  # e.g. "B0005"
        df = load_cell(mp)
        if df.empty:
            continue
        df["cell_id"] = cell_id
        frames.append(df)

    if not frames:
        raise ValueError(f"No discharge cycles parsed from {len(mat_files)} .mat files")

    result = pd.concat(frames, ignore_index=True)

    # Rename for SoHAgingPoint schema (§4 spec)
    result = result.rename(
        columns={
            "cycle_idx": "cycles",
            "temperature_measured": "temp_C",
        }
    )

    # Compute capacity retention (% of nominal 2.0 Ah)
    result["capacity_retention_pct"] = (
        result["capacity_Ah"] / NOMINAL_CAPACITY_AH * 100.0
    )

    # Tag source; dwell_time_h NOT available from NASA PCoE (see module docstring).
    result["source"] = "NASA_PCoE"

    # Reorder columns to SoHAgingPoint spec order
    return result[
        ["cell_id", "temp_C", "cycles", "capacity_Ah", "capacity_retention_pct", "source"]
    ]
