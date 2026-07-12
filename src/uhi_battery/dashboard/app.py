"""Streamlit dashboard — UHI × e-micromobility battery optimization.

Run::

    uv run streamlit run src/uhi_battery/dashboard/app.py --server.headless true

Layout
------
- Header: title + one-line description + data-window badge.
- Sidebar: day slider, hour slider, hotspot toggle.
- Tabs: Heat Map · Battery Impact · Routing · Metrics Summary.
- Footer: data sources + limitations.

Design choices
---------------
- Heat map: folium with an ImageOverlay (downsampled LST) + GeoJson hotspot
  polygons. Real-map context (OSM tiles) grounds the temperature field.
- Charts: plotly (consistent typography, temperature palette).
- All data is local (zarr / geojson / json / pkl). No API calls.
"""

from __future__ import annotations

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

from uhi_battery.config import settings
from uhi_battery.dashboard.data_loader import (
    daily_temperature_curve,
    load_hotspots,
    load_metrics,
    load_pareto_frontiers,
    load_soh_model,
    lst_timeseries,
    reconstruct_lst_field,
)
from uhi_battery.dashboard.theme import (
    ACCENT,
    COLD,
    CSS,
    HOT,
    INK,
    MUTED,
    PAPER,
    TEMP_STOPS,
    metric_card,
    plotly_layout,
    temp_hex_at,
)
from uhi_battery.models.energy import compute_trip_energy
from uhi_battery.models.soh import predict_soh

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="UHI × E-Micromobility Battery Optimization",
    page_icon="🌡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(CSS, unsafe_allow_html=True)


# ── Header ───────────────────────────────────────────────────────────────────
def _header() -> None:
    st.markdown(
        """
        <div style="display:flex; align-items:flex-end; gap:1.2rem; flex-wrap:wrap;
                    margin-bottom:0.4rem;">
            <h1 style="margin:0; font-size:2.1rem;">
                Urban Heat Island × E-Micromobility Battery Optimization
            </h1>
            <span class="data-badge warm">2025 May–Oct · Kadıköy, Istanbul</span>
        </div>
        <p class="caption" style="max-width:980px;">
            How urban heat affects e-scooter energy use and battery State of Health.
            Satellite land-surface temperature (LST) identifies heat hotspots; a
            physics energy model and an Arrhenius SoH model quantify the cost;
            Pareto routing trades energy against thermal exposure.
        </p>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<hr style="border:none; border-top:1px solid #e3ddd2; margin:0.6rem 0 1.2rem;">',
        unsafe_allow_html=True,
    )


# ── Sidebar ─────────────────────────────────────────────────────────────────
def _sidebar() -> dict:
    st.sidebar.markdown("### Controls")

    times = lst_timeseries()
    n_days = len(times)
    if n_days == 0:
        st.sidebar.warning("LST zarr not found — map controls disabled.")
        st.sidebar.info(
            "Expected `data/processed/lst_hourly.zarr`. Re-run P1 fusion to generate it."
        )
        return {"day_idx": 0, "hour": 14.0, "show_hotspots": True, "n_days": 0, "times": times}

    day_idx = st.sidebar.slider(
        "Day",
        min_value=1,
        max_value=n_days,
        value=n_days // 2,
        step=1,
        help=f"Index into the {n_days}-day LST series (2025-05-04 → 2025-10-27).",
    )
    selected_date = times[day_idx - 1].strftime("%Y-%m-%d") if n_days else "—"
    st.sidebar.caption(f"Selected date: **{selected_date}**")

    hour = st.sidebar.slider(
        "Hour of day",
        min_value=0,
        max_value=24,
        value=14,
        step=1,
        help="Peak LST is around 14:00 (diurnal cosine model).",
    )

    show_hotspots = st.sidebar.checkbox("Overlay UHI hotspots", value=True)

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        f"""
        <div class="caption">
            <strong>Pilot bbox</strong><br>
            {settings.pilot_bbox[0]:.2f}, {settings.pilot_bbox[1]:.2f} →
            {settings.pilot_bbox[2]:.2f}, {settings.pilot_bbox[3]:.2f}<br>
            Resolution: {settings.target_resolution_m} m
        </div>
        """,
        unsafe_allow_html=True,
    )

    return {
        "day_idx": day_idx - 1,
        "hour": float(hour),
        "show_hotspots": show_hotspots,
        "n_days": n_days,
        "times": times,
        "selected_date": selected_date,
    }


# ── Tab 1: Heat Map ─────────────────────────────────────────────────────────
def _heatmap_to_rgba(arr: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """Map a 2-D LST array to an RGBA uint8 image using TEMP_STOPS."""
    finite = np.isfinite(arr)
    norm = np.zeros_like(arr, dtype=float)
    norm[finite] = (arr[finite] - vmin) / max(vmax - vmin, 1e-6)
    norm = np.clip(norm, 0.0, 1.0)

    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    flat = norm.ravel()
    # Vectorized palette lookup via precomputed LUT.
    lut_n = 256
    lut = np.array([temp_hex_at(i / (lut_n - 1)) for i in range(lut_n)])
    lut_rgb = np.array(
        [[int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)] for c in lut],
        dtype=np.uint8,
    )
    idx = np.clip((flat * (lut_n - 1)).round().astype(int), 0, lut_n - 1)
    rgb = lut_rgb[idx].reshape(h, w, 3)
    rgba[..., :3] = rgb
    rgba[..., 3] = np.where(finite, 215, 0).astype(np.uint8)  # semi-transparent
    return rgba


def _build_folium_map(
    arr: np.ndarray,
    bbox: tuple[float, float, float, float],
    vmin: float,
    vmax: float,
    show_hotspots: bool,
) -> folium.Map:
    lon_min, lat_min, lon_max, lat_max = bbox
    center = [(lat_min + lat_max) / 2, (lon_min + lon_max) / 2]

    m = folium.Map(location=center, zoom_start=13, tiles="CartoDB positron", control_scale=True)

    rgba = _heatmap_to_rgba(arr, vmin, vmax)
    bounds = [[lat_min, lon_min], [lat_max, lon_max]]

    img = folium.raster_layers.ImageOverlay(
        image=rgba,  # ndarray; folium handles serialization
        bounds=bounds,
        opacity=0.85,
        name="LST (°C)",
        mercator_project=True,
        pixelated=True,
    )
    img.add_to(m)

    # Colorbar legend (HTML overlay).
    stops_css = ", ".join(f"{c} {t * 100:.0f}%" for t, c in TEMP_STOPS)
    legend_html = f"""
    <div style="position: fixed; bottom: 24px; left: 24px; z-index: 9999;
                background: {PAPER}; padding: 10px 12px; border-radius: 8px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.2); font-family: Inter, Arial;
                font-size: 12px; color: {INK};">
      <div style="font-weight:600; margin-bottom:6px;">Land Surface Temp (°C)</div>
      <div style="width:180px; height:12px; border-radius:3px;
                  background: linear-gradient(90deg, {stops_css});"></div>
      <div style="display:flex; justify-content:space-between; margin-top:4px;">
        <span>{vmin:.1f}</span><span>{vmax:.1f}</span>
      </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    # Hotspot polygons.
    if show_hotspots:
        geo = load_hotspots()
        if geo is not None:

            def _style_fn(feature):
                t = feature["properties"].get("type", "cold")
                color = HOT if t == "hot" else COLD
                return {
                    "fillColor": color,
                    "color": color,
                    "weight": 0.5,
                    "fillOpacity": 0.35,
                }

            folium.GeoJson(
                geo,
                name="UHI hotspots",
                style_function=_style_fn,
                tooltip=folium.GeoJsonTooltip(
                    fields=["type", "gi_z", "pval_fdr"],
                    aliases=["Type", "Gi z", "p (FDR)"],
                    localize=True,
                ),
            ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    return m


def _tab_heat_map(ctrl: dict) -> None:
    st.subheader("Heat Map")
    st.markdown(
        f"""
        <p class="caption">
            Reconstructed land-surface temperature for
            <strong>{ctrl.get("selected_date", "—")}</strong> at
            <strong>{ctrl["hour"]:.0f}:00</strong> local time, from the
            MODIS-anomaly + Landsat fusion with a cosine diurnal model.
            Red polygons mark significant heat hotspots (Gi*); blue marks cold spots.
        </p>
        """,
        unsafe_allow_html=True,
    )

    if ctrl["n_days"] == 0:
        st.info("LST data unavailable. See the sidebar note.")
        return

    result = reconstruct_lst_field(ctrl["day_idx"], ctrl["hour"])
    if result is None:
        st.warning("Could not reconstruct the LST field.")
        return
    arr, bbox, vmin, vmax = result

    col_map, col_curve = st.columns([3, 2], gap="large")

    with col_map:
        fmap = _build_folium_map(arr, bbox, vmin, vmax, ctrl["show_hotspots"])
        st_folium(fmap, width=None, height=520, returned_objects=[])

    with col_curve:
        st.markdown("#### Daily temperature curve")
        st.markdown(
            """
            <p class="caption">
                Spatial-mean LST across hours of the selected day. The diurnal
                cosine peaks at ~14:00. Drag the hour slider to scan the day.
            </p>
            """,
            unsafe_allow_html=True,
        )
        df = daily_temperature_curve(ctrl["day_idx"])
        if not df.empty:
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=df["hour"],
                    y=df["lst_c"],
                    mode="lines+markers",
                    line=dict(color=ACCENT, width=2.5),
                    marker=dict(size=4),
                    hovertemplate="Hour %{x:.0f}<br>%{y:.1f} °C<extra></extra>",
                )
            )
            # current-hour marker
            fig.add_vline(x=ctrl["hour"], line=dict(color=INK, dash="dot", width=1.5))
            fig.update_layout(
                **plotly_layout(
                    xaxis=dict(title="Hour of day", dtick=2, gridcolor="#e8e3d9"),
                    yaxis=dict(title="Mean LST (°C)", gridcolor="#e8e3d9"),
                    height=300,
                )
            )
            st.plotly_chart(fig, use_container_width=True)

        # Hotspot counts
        geo = load_hotspots()
        if geo is not None:
            feats = geo.get("features", [])
            n_hot = sum(1 for f in feats if f["properties"].get("type") == "hot")
            n_cold = sum(1 for f in feats if f["properties"].get("type") == "cold")
            c1, c2 = st.columns(2)
            c1.markdown(
                metric_card("Hot cells", f"{n_hot}", "Gi* significant, p<0.05 (FDR)"),
                unsafe_allow_html=True,
            )
            c2.markdown(
                metric_card("Cold cells", f"{n_cold}", "Gi* significant, p<0.05 (FDR)"),
                unsafe_allow_html=True,
            )


# ── Tab 2: Battery Impact ──────────────────────────────────────────────────
def _soh_gauge(retention: float, temp_c: float, n_cycles: int) -> go.Figure:
    color = "#2f855a" if retention >= 90 else ACCENT if retention >= 80 else "#d7301f"
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=retention,
            number={"suffix": "%", "font": {"size": 44, "family": "Georgia"}},
            title={"text": "Capacity retention", "font": {"size": 13, "color": MUTED}},
            gauge={
                "axis": {
                    "range": [0, 100],
                    "tickwidth": 1,
                    "tickcolor": MUTED,
                    "tickfont": {"size": 10},
                },
                "bar": {"color": color, "thickness": 0.32},
                "bgcolor": "#efece6",
                "borderwidth": 0,
                "steps": [
                    {"range": [0, 80], "color": "#f3ece4"},
                    {"range": [80, 100], "color": "#e7e0d4"},
                ],
                "threshold": {
                    "line": {"color": INK, "width": 2},
                    "thickness": 0.85,
                    "value": 80,
                },
            },
        )
    )
    fig.update_layout(**plotly_layout(height=260, margin={"l": 20, "r": 20, "t": 30, "b": 10}))
    return fig


def _energy_decomposition_chart(
    roll_wh: float, aero_wh: float, grade_wh: float, total_wh: float, distance_m: float
) -> go.Figure:
    components = [
        ("Rolling", roll_wh, "#7bccc4"),
        ("Aerodynamic", aero_wh, "#2b8cbe"),
        ("Grade", grade_wh, "#c75d2c"),
    ]
    fig = go.Figure()
    for name, val, color in components:
        fig.add_trace(
            go.Bar(
                x=[name],
                y=[val],
                name=name,
                marker_color=color,
                width=0.5,
                hovertemplate=f"{name}: %{{y:.2f}} Wh<extra></extra>",
            )
        )
    fig.update_layout(
        **plotly_layout(
            barmode="stack",
            yaxis=dict(title="Energy (Wh)", gridcolor="#e8e3d9"),
            showlegend=False,
            height=300,
            title=dict(
                text=f"Trip energy decomposition · {total_wh:.2f} Wh "
                f"({total_wh / (distance_m / 1000):.2f} Wh/km)",
                font={"size": 13, "color": INK},
            ),
        )
    )
    return fig


def _tab_battery(ctrl: dict) -> None:  # noqa: ARG001
    st.subheader("Battery Impact")

    bundle = load_soh_model()
    metrics = load_metrics()
    if bundle is None:
        st.warning("SoH model not found (`data/processed/soh_model.pkl`).")
        return
    model = bundle["model"]

    st.markdown(
        f"""
        <p class="caption">
            Arrhenius capacity-fade model calibrated on NASA PCoE LCO 18650 data.
            Activation energy <strong>Ea = {model["Ea"] / 1000:.0f} kJ/mol</strong>
            (literature-fixed for LCO chemistry); only the pre-exponential factor
            <em>A</em> is fit. Model excludes cold regimes (&lt; 15 °C) where
            lithium plating dominates.
        </p>
        """,
        unsafe_allow_html=True,
    )

    # ── SoH calculator ──
    st.markdown("#### State of Health calculator")
    c_left, c_right = st.columns([1, 2], gap="large")

    with c_left:
        temp_c = st.slider("Operating temperature (°C)", 5, 45, 35, step=1)
        n_cycles = st.slider("Equivalent full cycles", 0, 1000, 500, step=10)
        retention = predict_soh(model, temp_c=float(temp_c), n_cycles=float(n_cycles))
        st.plotly_chart(_soh_gauge(retention, temp_c, n_cycles), use_container_width=True)
        st.caption(
            f"At **{temp_c} °C** over **{n_cycles}** cycles → "
            f"**{retention:.1f}%** retention. "
            "Cold-temperature predictions underestimate degradation "
            "(lithium plating not modeled)."
        )

    with c_right:
        # SoH vs temperature curve at fixed cycle count
        temps = np.linspace(5, 45, 41)
        fig = go.Figure()
        for cyc in (100, 300, 500, 1000):
            ret = [predict_soh(model, float(t), float(cyc)) for t in temps]
            fig.add_trace(
                go.Scatter(
                    x=temps,
                    y=ret,
                    mode="lines",
                    name=f"{cyc} cycles",
                    line=dict(width=2.2),
                    hovertemplate="%{x:.0f} °C → %{y:.1f}%<extra>" + f"{cyc} cycles</extra>",
                )
            )
        fig.add_vline(x=temp_c, line=dict(color=INK, dash="dot", width=1.2))
        fig.update_layout(
            **plotly_layout(
                xaxis=dict(title="Temperature (°C)", gridcolor="#e8e3d9"),
                yaxis=dict(title="Retention (%)", gridcolor="#e8e3d9", range=[0, 100]),
                legend=dict(orientation="h", y=-0.18, font={"size": 11}),
                height=320,
                title=dict(
                    text="Retention vs temperature (Arrhenius)", font={"size": 13, "color": INK}
                ),
            )
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Energy calculator ──
    st.markdown("---")
    st.markdown("#### Trip energy calculator")
    st.markdown(
        """
        <p class="caption">
            Physics-based energy model: rolling + aerodynamic + grade forces,
            divided by a temperature-derated drivetrain efficiency η(T).
            Steady-state cruising — under-predicts real-world start-stop losses.
        </p>
        """,
        unsafe_allow_html=True,
    )

    ec1, ec2, ec3, ec4 = st.columns(4)
    distance_m = ec1.number_input("Distance (m)", 100, 20000, 3000, step=100)
    speed_kmh = ec2.number_input("Speed (km/h)", 5, 35, 15, step=1)
    e_temp_c = ec3.number_input("Ambient temp (°C)", 0, 50, 25, step=1)
    slope_deg = ec4.number_input("Avg slope (°)", -10.0, 10.0, 0.0, step=0.5)

    # Decompose: compute each force component separately.
    _MASS_KG, _G, _CRR = 100.0, 9.81, 0.008
    _RHO, _CDA = 1.225, 0.45
    v_ms = speed_kmh / 3.6
    f_roll = _MASS_KG * _G * _CRR
    f_aero = 0.5 * _RHO * _CDA * v_ms**2
    f_grade = _MASS_KG * _G * np.sin(np.radians(slope_deg))
    total_wh = compute_trip_energy(distance_m, speed_kmh, e_temp_c, slope_deg)
    roll_wh = f_roll * distance_m / 3600.0
    aero_wh = f_aero * distance_m / 3600.0
    grade_wh = f_grade * distance_m / 3600.0
    # η = total_work / energy — recover derated efficiency
    raw_wh = roll_wh + aero_wh + grade_wh
    eta = raw_wh / total_wh if total_wh > 0 else 0.85

    ec1, ec2, ec3 = st.columns([2, 1, 1], gap="large")
    with ec1:
        st.plotly_chart(
            _energy_decomposition_chart(roll_wh, aero_wh, grade_wh, total_wh, distance_m),
            use_container_width=True,
        )
    with ec2:
        st.markdown(
            metric_card(
                "Trip energy", f"{total_wh:.2f} Wh", f"{total_wh / (distance_m / 1000):.2f} Wh/km"
            ),
            unsafe_allow_html=True,
        )
        st.markdown(
            metric_card("Drivetrain η", f"{eta * 100:.1f} %", f"at {e_temp_c} °C (T-derated)"),
            unsafe_allow_html=True,
        )
    with ec3:
        st.markdown(
            metric_card("Total work", f"{raw_wh:.2f} Wh", "mechanical, pre-η"),
            unsafe_allow_html=True,
        )
        # Reference from metrics
        if metrics and "energy_decomposition" in metrics:
            ref = metrics["energy_decomposition"]
            st.markdown(
                metric_card("Reference", f"{ref['wh_per_km']:.2f} Wh/km", "3 km @ 15 km/h, 25 °C"),
                unsafe_allow_html=True,
            )


# ── Tab 3: Routing ─────────────────────────────────────────────────────────
def _tab_routing(ctrl: dict) -> None:  # noqa: ARG001
    st.subheader("Routing — Pareto frontiers")
    pareto = load_pareto_frontiers()
    if pareto is None:
        st.warning("Pareto frontiers not found (`data/processed/pareto_frontiers.json`).")
        return

    corr = pareto.get("correlation_r", float("nan"))
    obj2 = pareto.get("obj2", "length")
    pairs = pareto.get("pairs", [])

    st.markdown(
        f"""
        <p class="caption">
            Pareto-optimal routes for {len(pairs)} origin–destination pairs,
            trading trip energy (Wh) against route {obj2} (m). The 2nd objective
            switched from thermal exposure to length because energy and heat
            exposure are nearly collinear across the network
            (<strong>Pearson r = {corr:.3f}</strong>).
        </p>
        """,
        unsafe_allow_html=True,
    )

    # Honest finding callout.
    st.markdown(
        """
        <div class="note-callout">
            <strong>Finding — degenerate frontiers.</strong>
            Every frontier collapses to a single point (size 1). With energy and
            thermal exposure this strongly correlated, and with limited spatial
            heterogeneity in the pilot corridor, there is no exploitable
            trade-off: the energy-minimal route is also the thermally-minimal one.
            This is an honest result, not a bug — documented in the metrics.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Build a DataFrame of frontier points.
    rows = []
    for p in pairs:
        for r in p.get("frontier", []):
            rows.append(
                {
                    "pair": f"OD {p['pair_idx']}",
                    "energy_wh": r["energy_wh"],
                    "obj2_value": r["obj2_value"],
                    "route_len": len(r.get("route", [])),
                }
            )
    df = pd.DataFrame(rows)

    # Scatter: energy vs length, one trace per OD pair.
    fig = go.Figure()
    for pair_label in df["pair"].unique():
        sub = df[df["pair"] == pair_label]
        fig.add_trace(
            go.Scatter(
                x=sub["obj2_value"],
                y=sub["energy_wh"],
                mode="markers",
                marker=dict(size=16, line=dict(color=INK, width=1.5)),
                name=pair_label,
                hovertemplate=(
                    f"<b>{pair_label}</b><br>"
                    "Length: %{x:.0f} m<br>"
                    "Energy: %{y:.2f} Wh<br>"
                    f"Route nodes: {int(sub['route_len'].iloc[0])}"
                    "<extra></extra>"
                ),
                text=[f"{int(n)} nodes" for n in sub["route_len"]],
            )
        )
    fig.update_layout(
        **plotly_layout(
            xaxis=dict(title=f"Route {obj2} (m)", gridcolor="#e8e3d9"),
            yaxis=dict(title="Trip energy (Wh)", gridcolor="#e8e3d9"),
            legend=dict(orientation="h", y=-0.18, font={"size": 11}),
            height=420,
            title=dict(text="Pareto frontier — energy vs length", font={"size": 14, "color": INK}),
        )
    )
    st.plotly_chart(fig, use_container_width=True)

    # Per-pair table
    st.markdown("#### Per-OD-pair summary")
    show_df = df.copy()
    show_df["energy_wh"] = show_df["energy_wh"].round(2)
    show_df["obj2_value"] = show_df["obj2_value"].round(1)
    show_df.columns = ["OD pair", "Energy (Wh)", f"{obj2} (m)", "Route nodes"]
    st.dataframe(show_df, use_container_width=True, hide_index=True)

    # SoH impact
    metrics = load_metrics()
    if metrics and "routing_metrics" in metrics:
        soh = metrics["routing_metrics"].get("soh_impact", {})
        c1, c2, c3 = st.columns(3)
        c1.markdown(
            metric_card(
                "Baseline thermal load",
                f"{soh.get('baseline_dh', 0):.0f}",
                "degree-hours (reference route)",
            ),
            unsafe_allow_html=True,
        )
        c2.markdown(
            metric_card(
                "Pareto thermal load",
                f"{soh.get('pareto_dh', 0):.1f}",
                "degree-hours (optimal route)",
            ),
            unsafe_allow_html=True,
        )
        c3.markdown(
            metric_card(
                "SoH difference",
                f"{soh.get('soh_diff_pct', 0):.2f} %",
                "≈ 0 — no thermal routing benefit",
            ),
            unsafe_allow_html=True,
        )


# ── Tab 4: Metrics Summary ──────────────────────────────────────────────────
def _tab_metrics(ctrl: dict) -> None:  # noqa: ARG001
    st.subheader("Metrics Summary")
    metrics = load_metrics()
    bundle = load_soh_model()
    if metrics is None:
        st.warning("Metrics not found (`data/processed/metrics.json`).")
        return

    model = bundle["model"] if bundle else {}
    routing = metrics.get("routing_metrics", {})
    soh_q = metrics.get("model_quality", {}).get("soh", {})
    energy_q = metrics.get("model_quality", {}).get("energy", {})
    fusion_q = metrics.get("model_quality", {}).get("fusion", {})
    data_q = metrics.get("data_quality", {})
    dom = metrics.get("dominance_test", {})

    # ── Headline cards ──
    st.markdown("#### Headline")
    n_hot = 0
    geo = load_hotspots()
    if geo:
        n_hot = sum(1 for f in geo.get("features", []) if f["properties"].get("type") == "hot")

    soh_45_500 = predict_soh(model, 45.0, 500.0) if model else float("nan")

    cards = [
        ("Pearson r", f"{routing.get('correlation_r', 0):.3f}", "energy ↔ heat exposure"),
        (
            "Energy saving",
            f"{routing.get('energy_saving_pct', {}).get('mean', 0):.1f} %",
            "Pareto vs baseline (≈0 — degenerate)",
        ),
        ("SoH @ 45 °C, 500 cyc", f"{soh_45_500:.1f} %", "Arrhenius prediction"),
        ("Ea (fixed)", f"{soh_q.get('ea_kj_per_mol', 45):.0f} kJ/mol", "LCO 18650 literature"),
        ("Hot cells", f"{n_hot}", "Gi* significant (FDR<0.05)"),
        (
            "Data coverage",
            f"{data_q.get('lst', {}).get('days', 0)} days",
            f"{data_q.get('lst', {}).get('date_range', '—')}",
        ),
    ]
    cols = st.columns(len(cards))
    for col, (label, value, sub) in zip(cols, cards, strict=False):
        col.markdown(metric_card(label, value, sub), unsafe_allow_html=True)

    # ── Model quality ──
    st.markdown("#### Model quality")
    mq1, mq2, mq3 = st.columns(3)
    mq1.markdown(
        metric_card(
            "SoH R² (warm)",
            f"{soh_q.get('r_sq_warm_regimes', 0):.2f}",
            f"LOCO RMSE {soh_q.get('loco_mean_rmse', 0):.4f} / cycle",
        ),
        unsafe_allow_html=True,
    )
    mq2.markdown(
        metric_card(
            "Energy reference",
            f"{energy_q.get('reference_wh_per_km', 0):.2f} Wh/km",
            "below lit. [8–15] — steady-state physics",
        ),
        unsafe_allow_html=True,
    )
    mq3.markdown(
        metric_card(
            "Fusion clear-sky RMSE",
            f"{fusion_q.get('clear_sky_rmse_c', 0):.2f} °C",
            "between-overpass ~1–3 °C",
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
        <p class="caption" style="margin-top:0.6rem;">
            <strong>SoH approach:</strong> {soh_q.get("approach", "—")}.<br>
            <strong>Energy approach:</strong> {energy_q.get("approach", "—")}.<br>
            <strong>Fusion method:</strong> {fusion_q.get("method", "—")}.<br>
            <strong>Dominance test:</strong> {"passed" if dom.get("passed") else "failed"} ·
            {dom.get("n_violations", 0)} violation(s) across
            {len(routing.get("frontier_sizes", []))} OD pairs.
        </p>
        """,
        unsafe_allow_html=True,
    )

    # ── Limitations ──
    st.markdown("#### Limitations (honest)")
    st.markdown(
        f"""
        <div class="note-callout">
            <ul style="margin:0; padding-left:1.1rem;">
              <li><strong>Degenerate Pareto frontiers</strong> — energy and thermal
                  objectives are collinear (r = {routing.get("correlation_r", 0):.3f});
                  no exploitable trade-off in the pilot corridor.</li>
              <li><strong>Ea fixed from literature</strong> (45 kJ/mol, LCO 18650);
                  only the pre-exponential <em>A</em> was calibrated from NASA PCoE.</li>
              <li><strong>Cold regime excluded</strong> — Arrhenius form does not
                  capture lithium plating (&lt; 15 °C); cold RMSE reported separately
                  ({soh_q.get("cold_rmse", 0):.4f}).</li>
              <li><strong>No calendar aging</strong> — NASA PCoE lacks dwell-time
                  data; SoH is cycle-only.</li>
              <li><strong>Energy model under-predicts</strong> real-world Wh/km
                  (no start-stop / acceleration losses) — documented, not a bug.</li>
              <li><strong>QC pending</strong> — all LST pixels assumed valid until
                  real GEE quality flags are integrated.</li>
            </ul>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── Footer ──────────────────────────────────────────────────────────────────
def _footer() -> None:
    st.markdown(
        """
        <div class="footer">
            <strong>Data sources.</strong>
            NASA PCoE battery aging dataset · Landsat &amp; MODIS LST via Google
            Earth Engine · OpenStreetMap (OSMnx) street network · SRTM 30 m DEM.<br>
            <strong>Pilot.</strong> Moda–Kozyatağı corridor, Kadıköy, Istanbul
            (2025-05-04 → 2025-10-27, 177 days, 30 m grid).<br>
            <strong>Limitations.</strong> Lab-calibrated models, single-pilot
            corridor, degenerate routing frontiers, no field telemetry validation.
            See the Metrics tab for the full honest accounting.
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    """Entry point for `streamlit run`."""
    _header()
    ctrl = _sidebar()

    tab_heat, tab_battery, tab_routing, tab_metrics = st.tabs(
        ["🌡️ Heat Map", "🔋 Battery Impact", "🛣️ Routing", "📊 Metrics Summary"]
    )

    with tab_heat:
        _tab_heat_map(ctrl)
    with tab_battery:
        _tab_battery(ctrl)
    with tab_routing:
        _tab_routing(ctrl)
    with tab_metrics:
        _tab_metrics(ctrl)

    _footer()


if __name__ == "__main__":
    main()
