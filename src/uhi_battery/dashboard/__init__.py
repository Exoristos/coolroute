"""Streamlit dashboard — UHI × e-micromobility battery optimization.

Visualizes the Urban Heat Island effect on e-scooter battery energy and
State-of-Health for the Kadıköy (Istanbul) pilot, May–Oct 2025.

Run with::

    uv run streamlit run src/uhi_battery/dashboard/app.py --server.headless true
"""

from __future__ import annotations

__all__ = ["main"]
