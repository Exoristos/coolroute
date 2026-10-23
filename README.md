# CoolRoute

**Urban-heat-aware routing and battery analytics for electric micromobility.**

[![CI](https://github.com/Exoristos/coolroute/actions/workflows/ci.yml/badge.svg)](https://github.com/Exoristos/coolroute/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

CoolRoute studies how urban heat affects e-scooter energy consumption and
battery ageing. It combines satellite land-surface temperature, spatial
statistics, physics-based battery models and multi-objective routing in an
end-to-end research prototype with a FastAPI service and Streamlit dashboard.

> This is a research and portfolio project. Routes and battery estimates are
> model outputs, not operational safety or navigation advice.

## Why it matters

Conventional routing optimises distance or travel time. CoolRoute asks a
different question: **can a micromobility route reduce thermal exposure and
battery stress without becoming impractically long?**

The pilot covers Kadıköy, Üsküdar and Ataşehir on Istanbul's Anatolian side.
Trip demand is simulated; the data interface is intentionally swappable so real
operator telemetry can be integrated later.

## What the pipeline does

```text
Landsat + MODIS LST ──> 30 m heat layer ──> Moran's I / Gi* hotspots
                                                      │
NASA battery ageing ──> energy + SoH models           │
                                                      ▼
OSM road network ──> edge attribution ──> Pareto routing (NSGA-II)
                                                      │
                                      FastAPI + Streamlit dashboard
```

## Selected results

| Area | Saved experiment result | Interpretation |
|---|---:|---|
| Stop-and-go energy | 9.0 Wh/km MC mean | Brings the model into the 6–15 Wh/km literature band |
| Spatial clustering | Moran's I = 0.966, p < 0.001 | Strong clustering at the 300 m analysis scale |
| Routing | 2/6 non-degenerate Pareto fronts | Some OD pairs expose a real heat–route trade-off |
| Network scale | 61,378 nodes / 157,248 edges | Expanded ~810 km² pilot graph |
| Test inventory | 254 pass, 1 skip in the saved local run | CI now re-runs the reproducible test suite |

The recorded fusion RMSE of `0.000 °C` is a **pipeline self-consistency check**
against a constructed reference, not independent ground-truth accuracy. The
project therefore does not present it as external validation.

## Technical highlights

- Landsat/MODIS land-surface-temperature fusion at 30 m output resolution
- Moran's I and Getis-Ord Gi* spatial hotspot analysis
- physics-based steady-state energy model plus Markov-chain Monte Carlo drive cycle
- Arrhenius-style State-of-Health model with warm-regime calibration
- OSMnx/NetworkX graph attribution and NSGA-II Pareto optimisation
- seven FastAPI endpoints and a four-tab Streamlit dashboard
- deterministic seeds, cached intermediate data and a pytest/ruff quality gate

## Quick start

Requirements: Python 3.11 or 3.12, [`uv`](https://docs.astral.sh/uv/) and,
for the live geospatial pipeline, a Google Earth Engine account.

```bash
git clone https://github.com/Exoristos/coolroute.git
cd coolroute
uv sync --extra dev
cp .env.example .env
```

Run the offline tests and linter:

```bash
uv run pytest tests/ -q
uv run ruff check .
```

Launch the services:

```bash
uv run uvicorn uhi_battery.api.app:app --port 8000
uv run streamlit run src/uhi_battery/dashboard/app.py
```

Representative pipeline commands:

```bash
uv run python scripts/train_models.py
uv run python scripts/compute_hotspots.py
uv run python scripts/run_pareto.py
uv run python scripts/compute_metrics.py
```

## Repository layout

```text
src/uhi_battery/
  data/          satellite fusion, battery data and trip providers
  models/        energy, drive-cycle and battery SoH models
  stats/         spatial autocorrelation and hotspot analysis
  routing/       road graph preparation and Pareto optimisation
  api/           FastAPI service
  dashboard/     Streamlit application
scripts/         reproducible pipeline entry points
tests/           unit, integration and regression tests
docs/            technical report and development notes
```

## Limitations

- Land-surface temperature is not ambient air or battery-core temperature.
- Trip demand is simulated rather than supplied by a fleet operator.
- The SoH activation energy is fixed from literature because warm-regime
  calibration data is limited.
- Energy and heat exposure remain highly correlated for several OD pairs, so
  four of six saved Pareto fronts are degenerate.
- A realistic deployment requires field telemetry, publication-lag handling,
  uncertainty intervals and out-of-area validation.

For methodology, detailed metrics and reproduction notes, see the
[technical report](docs/TECHNICAL_REPORT.md). Historical implementation notes
are kept separately in [development notes](docs/development-notes.md).

## License

MIT License. See [LICENSE](LICENSE).
