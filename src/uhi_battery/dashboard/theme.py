"""Theme constants — temperature palette, fonts, layout tokens.

A single source of truth for the dashboard's visual identity. Cool blues →
warm reds for temperature, with neutral ink and a warm-paper background that
reads well on a laptop screen.
"""

from __future__ import annotations

# ── Temperature palette (cool → warm) ────────────────────────────────────────
# Anchored on Landsat/MODIS LST visual conventions. Used consistently across
# the folium heat layer and the plotly charts.
TEMP_STOPS: list[tuple[float, str]] = [
    (0.00, "#2c3a8c"),  # deep cool blue
    (0.20, "#2b8cbe"),  # cool blue
    (0.40, "#7bccc4"),  # teal
    (0.55, "#edf8b1"),  # pale yellow
    (0.70, "#fd8d3c"),  # orange
    (0.85, "#d7301f"),  # red
    (1.00, "#7f0a0a"),  # dark crimson
]


def temp_hex_at(t01: float) -> str:
    """Linear-interpolate the temperature palette at t∈[0,1]."""
    t01 = max(0.0, min(1.0, t01))
    stops = TEMP_STOPS
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t01 <= t1:
            f = (t01 - t0) / (t1 - t0) if t1 > t0 else 0.0
            r0, g0, b0 = int(c0[1:3], 16), int(c0[3:5], 16), int(c0[5:7], 16)
            r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
            r = round(r0 + (r1 - r0) * f)
            g = round(g0 + (g1 - g0) * f)
            b = round(b0 + (b1 - b0) * f)
            return f"#{r:02x}{g:02x}{b:02x}"
    return stops[-1][1]


# ── Semantic colors ──────────────────────────────────────────────────────────
INK = "#1a2330"  # primary text / headings
INK_SOFT = "#4a5568"  # secondary text
MUTED = "#8a93a2"  # captions, badges
PAPER = "#f7f5f1"  # warm paper background
CARD = "#ffffff"  # card surface
LINE = "#e3ddd2"  # hairline borders
ACCENT = "#c75d2c"  # warm accent (scooter / energy)
HOT = "#d7301f"
COLD = "#2b8cbe"
GOOD = "#2f855a"
WARN = "#b7791f"


# ── Plotly layout defaults ──────────────────────────────────────────────────
PLOTLY_LAYOUT = {
    "font": {"family": "Inter, Helvetica, Arial, sans-serif", "color": INK, "size": 13},
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "margin": {"l": 8, "r": 8, "t": 36, "b": 8},
    "colorway": ["#c75d2c", "#2b8cbe", "#2f855a", "#b7791f", "#7f0a0a", "#7bccc4"],
}


def plotly_layout(**overrides) -> dict:
    """Return a base Plotly layout dict with overrides applied."""
    base = {k: (v.copy() if isinstance(v, dict) else v) for k, v in PLOTLY_LAYOUT.items()}
    base.update(overrides)
    return base


# ── Injected CSS ─────────────────────────────────────────────────────────────
CSS = f"""
<style>
    /* App background — warm paper, not stark white */
    .stApp {{
        background: linear-gradient(180deg, {PAPER} 0%, #efece6 100%);
    }}
    /* Headings */
    h1, h2, h3, h4 {{
        font-family: 'Georgia', 'Times New Roman', serif !important;
        color: {INK} !important;
        letter-spacing: -0.01em;
    }}
    /* Metric cards */
    .metric-card {{
        background: {CARD};
        border: 1px solid {LINE};
        border-radius: 10px;
        padding: 1.1rem 1.2rem;
        box-shadow: 0 1px 2px rgba(26,35,48,0.04);
        height: 100%;
    }}
    .metric-card .label {{
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: {MUTED};
        font-weight: 600;
        margin-bottom: 0.3rem;
    }}
    .metric-card .value {{
        font-family: 'Georgia', serif;
        font-size: 1.85rem;
        font-weight: 700;
        color: {INK};
        line-height: 1.05;
    }}
    .metric-card .sub {{
        font-size: 0.78rem;
        color: {INK_SOFT};
        margin-top: 0.35rem;
    }}
    /* Header badge */
    .data-badge {{
        display: inline-block;
        background: {INK};
        color: {PAPER};
        padding: 0.3rem 0.75rem;
        border-radius: 999px;
        font-size: 0.74rem;
        letter-spacing: 0.05em;
        font-weight: 600;
    }}
    .data-badge.warm {{
        background: {ACCENT};
    }}
    /* Section caption */
    .caption {{
        color: {MUTED};
        font-size: 0.85rem;
        line-height: 1.5;
    }}
    /* Tabs */
    .stTabs [data-baseline=tab] {{
        padding-top: 0.4rem;
        padding-bottom: 0.4rem;
    }}
    /* Footer */
    .footer {{
        margin-top: 2.5rem;
        padding-top: 1.2rem;
        border-top: 1px solid {LINE};
        color: {MUTED};
        font-size: 0.8rem;
        line-height: 1.6;
    }}
    /* Limit note callout */
    .note-callout {{
        background: #fbf3e6;
        border-left: 3px solid {WARN};
        padding: 0.7rem 0.9rem;
        border-radius: 4px;
        font-size: 0.86rem;
        color: {INK_SOFT};
        line-height: 1.5;
    }}
</style>
"""


def metric_card(label: str, value: str, sub: str = "") -> str:
    """Return HTML for a single metric card."""
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return f"""
    <div class="metric-card">
        <div class="label">{label}</div>
        <div class="value">{value}</div>
        {sub_html}
    </div>
    """
