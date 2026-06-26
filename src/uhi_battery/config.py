"""Central config (pydantic-settings). Single source of truth for pilot params.

Pilot corridor (D4 — to be confirmed via OSMnx): Moda–Kozyatağı, Kadıköy.
Bbox below is a conservative envelope; will be tightened in P1.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- GEE ---
    gee_service_account: str | None = None
    gee_private_key_path: str | None = None
    gee_project_id: str | None = None

    # --- Pilot corridor: Kadıköy + Üsküdar + Ataşehir (EPSG:4326) ---
    # Expanded from Moda-Kozyatağı only to full Anatolian-side coastal strip.
    # Includes Üsküdar (north), Kadıköy (centre), Ataşehir/Ümraniye (inland east),
    # Bostancı/Maltepe (south coast).  Avoids Bosphorus crossing — all OD pairs
    # stay on the Asian side for non-degenerate Pareto frontiers.
    # (west, south, east, north) = (lon_min, lat_min, lon_max, lat_max)
    pilot_bbox: tuple[float, float, float, float] = (28.95, 40.88, 29.25, 41.12)
    target_resolution_m: int = 30

    # --- Data window ---
    data_start_date: str = "2024-06-01"
    data_end_date: str = "2024-10-31"

    # --- Battery regimes (°C) — energy model breakpoints (NASA PCoE has 4/24/43) ---
    # We map operational scooter temps onto these calibration points.
    temp_regimes_c: tuple[float, ...] = (25.0, 35.0, 45.0)

    # --- Reproducibility ---
    random_seed: int = 42

    # --- API ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_cors_origins: list[str] = ["http://localhost:8501"]

    # --- Pareto guard (Oracle Karar 3) ---
    # If Pearson r between energy and heat-exposure across frontier > this, switch
    # the 2nd objective from heat-exposure to route-length.
    pareto_corr_switch_threshold: float = 0.8


settings = Settings()
