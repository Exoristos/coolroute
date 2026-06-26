"""Smoke test: prove the heavy geo/optimization stack imports cleanly."""
from __future__ import annotations


def test_geo_stack_imports():
    import geopandas  # noqa: F401
    import rasterio  # noqa: F401
    import rioxarray  # noqa: F401
    import xarray  # noqa: F401


def test_stats_imports():
    import esda  # noqa: F401  (standalone package, not pysal.explore.esda)
    import libpysal  # noqa: F401
    from esda import G_Local, Moran  # noqa: F401  (Moran's I + Getis-Ord Gi*)


def test_routing_opt_imports():
    import networkx  # noqa: F401
    import osmnx  # noqa: F401
    from pymoo.algorithms.moo.nsga2 import NSGA2  # noqa: F401


def test_config_loads():
    from uhi_battery.config import settings

    assert settings.target_resolution_m == 30
    assert len(settings.pilot_bbox) == 4
    assert settings.random_seed == 42
