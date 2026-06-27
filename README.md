# CoolRoute — UHI-Aware E-Micromobility Battery Optimization

**Quantifying how urban heat islands degrade e-scooter battery performance — from satellite
LST fusion through Pareto-optimal routing to a live dashboard.**

---

## Overview

This project models the non-linear impact of the Urban Heat Island (UHI) effect on electric
micromobility battery behaviour at two time scales: **instantaneous energy consumption**
(Wh/km) and **long-term State-of-Health (SoH) degradation**. It fuses multi-source satellite
thermal data into a high-resolution heat layer, applies spatial statistics to identify
persistent hotspots, calibrates dual battery models, and delivers Pareto-optimal routes that
trade off energy use against thermal exposure.

The **pilot area** is the Kadikoy–Uskudar–Atasehir region on Istanbul's Anatolian side —
~810 km^2 spanning coastal, urban-core, and inland zones with pronounced temperature gradients.
All analysis operates at **30 m spatial resolution** over a 5-month
summer/transition window (May–October 2025).

**Data sources** include Landsat 8/9 and MODIS land surface temperature (LST) via Google Earth
Engine, NASA PCoE Li-ion 18650 aging datasets, SRTM 30m digital elevation, and OSMnx road
network graphs. Trip demand is simulated pending future integration of operator telemetry
through a swappable `TripDataProvider` abstraction.

The project is a personal/portfolio work combining reproducible research analysis with a
working prototype (FastAPI + Streamlit dashboard).

---

## Architecture

```
                         ┌──────────────────────────────┐
                         │ Markov Chain + Monte Carlo   │
                         │  Drive Cycle Sim (P10)       │
                         │  Stops: 4-state chain        │
                         │  Regen: 30%, Aux: 12W        │
                         └──────────────┬───────────────┘
                                        │  applies stop-and-go delta
                                        v
Satellite LST (GEE)     NASA Aging Data      Road Network (OSMnx)
        |                     |                      |
        v                     v                      v
   Spatiotemporal       SoH Arrhenius Fit      Graph Edge Attribution
   Fusion (30m)              |                      |
        |                     |                      |
        v                     v                      v
   Hourly Heat Layer ──► Dual Battery Models ──► Pareto Routing (NSGA-II)
        |                     |                      |
        v                     v                      v
   Spatial Stats        Energy + SoH Predict     Pareto Frontiers
   (Moran's I, Gi*)                                  |
        |                                            v
        v                                     Metrics + Sensitivity
   Hotspot Clusters                                   |
        |                                             v
        └──────────────┬─────────────────>  FastAPI (7 endpoints)
                       |                            |
                       v                            v
                  Streamlit Dashboard (4 tabs)
```

### Modules

| # | Module | Description |
|---|--------|-------------|
| 1 | `data/gee_lst.py` | Pull Landsat and MODIS LST scenes from Google Earth Engine |
| 2 | `data/fusion.py` | Fuse Landsat (30m, sparse) with MODIS (1km, daily) into a 30m hourly LST layer |
| 3 | `data/nasa_aging.py` | Load and parse NASA PCoE Li-ion 18650 cycling data (5 temperature regimes) |
| 4 | `data/trips.py` | Simulate OD-pair e-scooter trips along the pilot corridor road network |
| 5 | `stats/spatial.py` | Global Moran's I and local Getis-Ord Gi* hotspot detection with permutation tests |
| 6 | `models/energy.py` | Physics-based energy model: rolling resistance + aerodynamic drag + grade + temperature-dependent efficiency |
| 6b | `models/drive_cycle.py` | **NEW** — Markov chain + Monte Carlo stop-and-go simulation (4 states: cruise, accel, decel, stop) |
| 7 | `models/soh.py` | Arrhenius-type exponential capacity-fade model, LOCO-validated, warm-regime calibration |
| 8 | `routing/` | OSMnx network loading + NSGA-II multi-objective optimization (energy vs heat exposure / length) |
| 9 | `metrics.py` | Energy saving %, SoH improvement %, sensitivity analysis, dominance test |

**Service layer:** `api/app.py` (FastAPI) exposes the heat layer, hotspots, routes, metrics, SoH, and energy endpoints. `dashboard/` provides a 4-tab Streamlit UI.

---

## Key Results

### LST Fusion

| Metric | Value |
|--------|-------|
| Coverage | 177 days, ~1114 x 891 grid cells (expanded area) |
| Spatial resolution | 30 m |
| RMSE (vs reference) | 0.000 degC |
| Method | MODIS-anomaly fallback onto Landsat baseline |
| LST range (peak day) | 19.9 – 51.7 degC (surface) |

### Spatial Statistics

| Metric | Value |
|--------|-------|
| Hotspots detected | 393 hot cells (+ 609 cold) |
| Analysis resolution | 300 m (aggregated) |
| Moran's I (global) | 0.966 (p < 0.001) |
| Method | Queen contiguity weights, 999 permutations |

### Drive Cycle Model (NEW)

| Metric | Value |
|--------|-------|
| Method | 4-state Markov chain (CRUISE, ACCEL, DECEL, STOP) |
| MC simulations | 86 bins × 30 sims (edge-binned for performance) |
| Wh/km (steady-state) | 4.1 Wh/km |
| **Wh/km (MC mean)** | **9.0 Wh/km** (+118% from stop-and-go) |
| Literature band | 6–15 Wh/km ✓ |
| Mean stops | 9.8 stops/km |
| Key physics | Regen 0.30, v_cutoff 5 km/h, aux 12W, η_accel=0.75 |

### Battery Models

| Model | Key Result |
|-------|-----------|
| Energy (steady-state) | 4.1 Wh/km (physics model, temperature-dependent efficiency) |
| Energy (drive cycle) | 9.0 Wh/km (MC mean with stop-and-go, inline with literature) |
| SoH degradation | Ea = 45 kJ/mol, R^2 = 0.67, exponential decay `100 * exp(-k * N)` |
| SoH validation | LOCO (leave-one-cell-out), 3 warm-regime calibration points |

### Pareto Routing (Expanded Area)

| Metric | Value |
|--------|-------|
| Algorithm | NSGA-II (pymoo), alpha-weighted scalarization |
| Road network | 61,378 nodes, 157,248 edges (OSMnx) |
| Correlation guard | Pearson r = 0.95 (energy vs heat exposure) |
| **Non-degenerate frontiers** | **2/6 OD pairs** (↑ from 0/3) |
| Best frontier | Pair D: Üsküdar → Ataşehir, size=4, 97 vs 255 node |
| Approach | Binned MC (86 bins, 8 min vs 9.5 hr full) |
| OD pairs | 6 coastal→inland pairs, all on Anatolian side |

### API & Dashboard

| Component | Detail |
|-----------|--------|
| API endpoints | 7 (`/health`, `/heat-layer`, `/hotspots`, `/route`, `/metrics`, `/soh`, `/energy`) |
| Dashboard tabs | 4 (Heat Map, Battery, Routing, Metrics) |
| Test suite | ~254 tests (254 pass, 1 skip) |

---

## Tech Stack

| Category | Packages |
|----------|----------|
| **Language** | Python 3.12 |
| **Package manager** | uv |
| **Geospatial** | geopandas, shapely, pyproj, rasterio, rioxarray, xarray, zarr |
| **Spatial stats** | esda, libpysal (PySAL ecosystem) |
| **Routing / graph** | osmnx, networkx |
| **Optimization** | pymoo (NSGA-II) |
| **Machine learning** | scikit-learn, scipy, numpy, pandas |
| **Earth Engine** | earthengine-api |
| **API** | FastAPI, uvicorn, pydantic, pydantic-settings |
| **Dashboard** | Streamlit, folium, streamlit-folium, plotly, matplotlib |
| **Testing** | pytest, pytest-cov |
| **Linting** | ruff |

---

## Installation

```bash
# Clone the repository
git clone https://github.com/Exoristos/coolroute.git
cd coolroute

# Create virtual environment and install dependencies
uv sync

# Set up environment variables
cp .env.example .env
# Edit .env and set GEE_PROJECT_ID (e.g., "my-gee-project")
```

### External Data Setup

**Google Earth Engine:** Register at [earthengine.google.com](https://earthengine.google.com),
then authenticate:

```bash
uv run earthengine authenticate
```

**NASA PCoE battery aging data:** Download the dataset from
`phm-datasets.s3.amazonaws.com` and place the `.mat` files under
`data/raw/nasa_battery/`. The pipeline expects 26 files across 5 temperature regimes
(5, 12, 24, 40, 44 degC).

---

## Usage

All commands are run from the repository root with `uv run`.

### Data Pipeline

```bash
# Pull Landsat + MODIS LST from GEE (requires GEE authentication)
uv run python scripts/pull_lst_real.py

# Fuse Landsat and MODIS into 30m hourly LST layer
uv run python scripts/fuse_lst.py

# Pull DEM + single-day LST for expanded pilot area (P10)
uv run python scripts/pull_expanded_data.py

# Run sanity check on fusion output
uv run python scripts/sanity_check.py
```

### Analysis & Modeling

```bash
# Train energy and SoH models (~1.5 s offline)
uv run python scripts/train_models.py

# Compute spatial hotspots (Moran's I + Getis-Ord Gi*)
uv run python scripts/compute_hotspots.py

# Run Pareto routing (requires internet for OSMnx graph download)
uv run python scripts/run_pareto.py

# Compute energy savings, SoH improvement, and sensitivity metrics
uv run python scripts/compute_metrics.py
```

### Services

```bash
# Start the FastAPI server
uv run uvicorn uhi_battery.api.app:app --port 8000
# OpenAPI docs at http://localhost:8000/docs

# Launch the Streamlit dashboard
uv run streamlit run src/uhi_battery/dashboard/app.py
```

### Tests

```bash
# Run the full test suite
uv run pytest tests/ -q

# With coverage report
uv run pytest tests/ --cov=uhi_battery --cov-report=term-missing
```

---

## Project Structure

```
isi_harita/
├── src/uhi_battery/
│   ├── config.py                 # Central settings (bbox, resolution, seed)
│   ├── data/
│   │   ├── fusion.py             # Landsat-MODIS spatiotemporal fusion
│   │   ├── gee_lst.py            # GEE LST retrieval
│   │   ├── nasa_aging.py         # NASA PCoE aging dataset loader
│   │   └── trips.py              # Trip simulation
│   ├── stats/
│   │   └── spatial.py            # Moran's I, Getis-Ord Gi*, hotspot extraction
│   ├── models/
│   │   ├── energy.py             # Physics-based energy consumption (Wh/km)
│   │   ├── drive_cycle.py       # Markov chain + Monte Carlo stop-and-go (NEW)
│   │   └── soh.py                # Arrhenius SoH degradation model
│   ├── routing/
│   │   ├── network.py            # OSMnx network download + DEM attribution
│   │   └── pareto.py             # NSGA-II multi-objective route optimization
│   ├── metrics.py                # Energy saving, SoH improvement, sensitivity
│   ├── api/
│   │   └── app.py                # FastAPI application (7 endpoints)
│   └── dashboard/
│       ├── app.py                # Streamlit dashboard entry point
│       ├── data_loader.py        # Shared data loading utilities
│       └── theme.py              # Dashboard styling
├── scripts/
│   ├── pull_lst_real.py          # GEE LST pull driver
│   ├── pull_expanded_data.py     # DEM + single-day LST for expanded bbox (NEW)
│   ├── fuse_lst.py               # Fusion driver
│   ├── sanity_check.py           # Fusion quality report
│   ├── train_models.py           # Model training driver
│   ├── compute_hotspots.py       # Spatial analysis driver
│   ├── run_pareto.py             # Pareto routing driver (6 OD pairs, MC)
│   ├── run_pareto_fast.py        # Fast Pareto with pre-computed binned MC (NEW)
│   └── compute_metrics.py        # Metrics + validation report driver
├── tests/                        # pytest suite (~254 tests)
├── data/
│   ├── raw/                      # NASA .mat files, GeoTIFFs, SRTM DEM
│   └── processed/                # .zarr, .nc, .geojson, .pkl, .json outputs
├── interview/                    # Full specification document
├── pyproject.toml                # Project metadata and dependencies
├── .env.example                  # Environment variable template
└── README.md
```

---

## Limitations

- **LST vs air temperature:** Land surface temperature (LST) is not ambient air temperature
  — it can reach 50+ degC on asphalt while air is 30–35 degC. Battery temperature is more
  influenced by air temperature and operational heat. ERA5-Land t2m integration is planned
  as a next step.
- **Pareto frontier degeneracy:** Energy consumption and heat exposure remain collinear
  (r = 0.95) even in the expanded area. The pipeline correctly detects this; 2/6 OD pairs
  produce non-degenerate frontiers. Truly diverse frontiers require stronger thermal
  gradients (e.g., coastal↔mountain) or uncorrelated objectives.
- **SoH calibration:** The Arrhenius activation energy (Ea = 45 kJ/mol) is fixed from
  literature due to a limited number of warm-regime data points (3). Stanford Calendar
  Aging dataset (232 cells, 4 temp regimes) can close this gap.
- **Cold-regime exclusion:** Lithium plating dominates degradation below 15 degC. The current
  SoH model is calibrated on warm (>=15 degC) data only and does not generalize to winter
  conditions.
- **No field telemetry:** Trip demand is fully simulated. Real operator telemetry (e.g., Marti
  e-scooters) can be integrated through the `TripDataProvider` abstraction but is not yet
  available. The Dublin E-Mobility Energy Dataset provides a partial validation path.
- **Energy model under-prediction (fixed):** The original steady-state model (4.1 Wh/km)
  under-predicted real-world consumption (8–15 Wh/km) by 2x. The Monte Carlo drive cycle
  model (P10) closes this gap to 9.0 Wh/km by simulating stop-and-go dynamics.
- **Sensitivity analysis:** Current sensitivity runs perturb LST uniformly by +/-2 degC and
  re-evaluate metrics without re-loading the road network. Route-level perturbation effects
  require a full network reload for accurate change-rate estimation.

---

## License

MIT — this is a personal portfolio project. See `pyproject.toml` for authorship details.
