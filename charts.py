"""Altair chart builders for the coverage view.

Colour follows the data-viz method: hues are assigned by the job they do, in a
fixed order, never cycled. Light and dark steps are both selected values, not an
automatic flip. Categorical hues carry identity (road class), the diverging pair
carries polarity (kilometres gained vs lost), and status colours are reserved for
state and never reused as a series.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

# ---------------------------------------------------------------- palette

CATEGORICAL_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                     "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CATEGORICAL_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
                    "#d55181", "#008300", "#9085e9", "#e66767"]

INK = {"light": {"primary": "#0b0b0b", "secondary": "#52514e", "grid": "#e6e5e1",
                 "surface": "#fcfcfb", "neutral": "#f0efec"},
       "dark": {"primary": "#ffffff", "secondary": "#c3c2b7", "grid": "#2f2f2d",
                "surface": "#1a1a19", "neutral": "#383835"}}

STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

# Diverging pair for gains and losses: warm/cool poles that read as opposite.
DIVERGING = {"light": {"pos": "#2a78d6", "neg": "#e34948"},
             "dark": {"pos": "#3987e5", "neg": "#e66767"}}


CLASS_ORDER = ["motorway", "trunk", "primary", "secondary", "tertiary",
               "unclassified", "residential", "living_street"]


def class_scale(mode: str = "light") -> alt.Scale:
    """Fixed hue per road class. The domain is always the full ordered class
    list, so a release or filter without some class never repaints the rest."""
    hues = CATEGORICAL_LIGHT if mode == "light" else CATEGORICAL_DARK
    return alt.Scale(domain=CLASS_ORDER, range=hues[:len(CLASS_ORDER)])


def _base(mode: str) -> dict:
    ink = INK[mode]
    return {
        "axis": {"labelColor": ink["secondary"], "titleColor": ink["secondary"],
                 "gridColor": ink["grid"], "domainColor": ink["grid"],
                 "tickColor": ink["grid"], "labelFontSize": 11, "titleFontSize": 11,
                 "titleFontWeight": "normal"},
        "legend": {"labelColor": ink["secondary"], "titleColor": ink["secondary"],
                   "labelFontSize": 11, "titleFontSize": 11, "titleFontWeight": "normal"},
        "title": {"color": ink["primary"], "fontSize": 13, "fontWeight": 600,
                  "subtitleColor": ink["secondary"], "subtitleFontSize": 11, "anchor": "start"},
        "view": {"stroke": None},
        "background": "transparent",
    }


def padded_domain(values: pd.Series, min_span: float) -> list[float]:
    """A y-domain at least `min_span` wide, centred on the data. Stops a 1 %
    wobble from filling the whole chart height and reading as a collapse."""
    lo, hi = float(values.min()), float(values.max())
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    pad = (hi - lo) * 0.08
    return [lo - pad, hi + pad]


def themed(chart: alt.Chart, mode: str) -> alt.Chart:
    cfg = _base(mode)
    return (chart
            .configure_axis(**cfg["axis"])
            .configure_legend(**cfg["legend"])
            .configure_title(**cfg["title"])
            .configure_view(stroke=None)
            .properties(background="transparent"))


# ---------------------------------------------------------------- charts

def total_km_over_time(totals: pd.DataFrame, mode: str = "light") -> alt.Chart:
    """Single-series line: total network length per week.

    One series, so no legend box; the title names it. Release weeks are marked
    with larger points because those are the only measured values on the line.
    """
    ink = INK[mode]
    df = totals.copy()
    df["is_release"] = df["source"].eq("Overture release")

    line = alt.Chart(df).mark_line(
        strokeWidth=2, color=CATEGORICAL_LIGHT[0] if mode == "light" else CATEGORICAL_DARK[0],
    ).encode(
        x=alt.X("week:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
        y=alt.Y("length_km:Q", title="kilometres",
                scale=alt.Scale(domain=padded_domain(df["length_km"], df["length_km"].mean() * 0.04)),
                axis=alt.Axis(format=",.0f")),
    )

    points = alt.Chart(df).mark_point(filled=True, size=90, opacity=1).encode(
        x="week:T", y="length_km:Q",
        color=alt.value(CATEGORICAL_LIGHT[0] if mode == "light" else CATEGORICAL_DARK[0]),
        stroke=alt.value(ink["surface"]), strokeWidth=alt.value(2),
        tooltip=[alt.Tooltip("week:T", title="Week", format="%d %b %Y"),
                 alt.Tooltip("length_km:Q", title="km", format=",.0f"),
                 alt.Tooltip("source:N", title="Source")],
    ).transform_filter(alt.datum.is_release)

    hover = alt.Chart(df).mark_circle(size=80, opacity=0).encode(
        x="week:T", y="length_km:Q",
        tooltip=[alt.Tooltip("week:T", title="Week", format="%d %b %Y"),
                 alt.Tooltip("length_km:Q", title="km", format=",.0f"),
                 alt.Tooltip("source:N", title="Source")],
    )

    return themed((line + hover + points).properties(
        height=260,
        title=alt.TitleParams("Road network length per week",
                             subtitle="Large points are measured Overture releases; the line between them is simulated"),
    ), mode)


def km_by_class_small_multiples(weekly: pd.DataFrame, mode: str = "light") -> alt.Chart:
    """Small multiples, one panel per road class, indexed to the first week.

    Classes differ by an order of magnitude in length, so absolute values on a
    shared axis flatten the small ones and independent axes blow a few km up to
    full panel height. Percent change from the first week on one shared axis is
    honest about size: real small changes look small, a regression stands out,
    and panels are directly comparable.
    """
    present = set(weekly["class"].astype(str))
    classes = [c for c in CLASS_ORDER if c in present]
    df = (weekly.groupby(["week", "class"], as_index=False, observed=True)
          .agg(length_km=("length_km", "sum"),
               source=("source", lambda s: "Overture release"
                       if (s == "Overture release").all() else "simulated")))
    df["class"] = df["class"].astype(str)
    df = df.sort_values(["class", "week"])
    first = df.groupby("class")["length_km"].transform("first")
    df["pct_vs_first"] = 100.0 * (df["length_km"] - first) / first

    lo = min(-1.0, float(df["pct_vs_first"].min()))
    hi = max(1.0, float(df["pct_vs_first"].max()))
    ink = INK[mode]

    base = alt.Chart().encode(
        x=alt.X("week:T", title=None, axis=alt.Axis(format="%b", grid=False, labelAngle=0, tickCount=3)),
        y=alt.Y("pct_vs_first:Q", title="% vs first week",
                scale=alt.Scale(domain=[lo * 1.1, hi * 1.1]), axis=alt.Axis(format="+.1f", tickCount=5)),
    )
    # Layers take no data of their own; the facet partitions the shared data.
    zero = alt.Chart().mark_rule(color=ink["secondary"], strokeWidth=1, opacity=0.2).encode(y=alt.datum(0))
    line = base.mark_line(strokeWidth=2).encode(
        color=alt.Color("class:N", scale=class_scale(mode), legend=None),
        tooltip=[alt.Tooltip("class:N", title="Class"),
                 alt.Tooltip("week:T", title="Week", format="%d %b %Y"),
                 alt.Tooltip("length_km:Q", title="km", format=",.0f"),
                 alt.Tooltip("pct_vs_first:Q", title="vs first week", format="+.2f"),
                 alt.Tooltip("source:N", title="Source")],
    )
    chart = alt.layer(zero, line, data=df).properties(width=140, height=120).facet(
        facet=alt.Facet("class:N", title=None, sort=classes,
                        header=alt.Header(labelFontSize=11, labelColor=ink["primary"],
                                          labelFontWeight=600, labelAnchor="start")),
        columns=min(len(classes), 5),
    )
    return themed(chart.properties(
        title=alt.TitleParams("Change by road class",
                              subtitle="Percent change from the first week, shared scale; panels are directly comparable")), mode)


def delta_by_dimension(comparison: pd.DataFrame, dimension: str, mode: str = "light",
                       top_n: int = 16) -> alt.Chart:
    """Diverging bars: kilometres gained or lost between two releases."""
    ink, div = INK[mode], DIVERGING[mode]
    df = comparison.reindex(comparison["delta_km"].abs().sort_values(ascending=False).index).head(top_n)

    bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, height=14).encode(
        x=alt.X("delta_km:Q", title="kilometres changed", axis=alt.Axis(format="+,.0f")),
        y=alt.Y(f"{dimension}:N", title=None, sort=alt.EncodingSortField("delta_km", order="descending")),
        color=alt.condition(alt.datum.delta_km >= 0, alt.value(div["pos"]), alt.value(div["neg"])),
        tooltip=[alt.Tooltip(f"{dimension}:N", title=dimension.title()),
                 alt.Tooltip("length_km_a:Q", title="Release A km", format=",.0f"),
                 alt.Tooltip("length_km_b:Q", title="Release B km", format=",.0f"),
                 alt.Tooltip("delta_km:Q", title="Delta km", format="+,.1f"),
                 alt.Tooltip("pct_change:Q", title="Change %", format="+.2f")],
    )
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(
        color=ink["secondary"], strokeWidth=1, opacity=0.6).encode(x="x:Q")

    return themed((bars + zero).properties(
        height=max(180, 22 * len(df)),
        title=alt.TitleParams(f"Change by {dimension}",
                              subtitle="Blue is network gained, red is network lost")), mode)


def name_coverage_over_time(totals: pd.DataFrame, mode: str = "light") -> alt.Chart:
    """Attribute completeness as a share of network length."""
    line = alt.Chart(totals).mark_line(
        strokeWidth=2, color=CATEGORICAL_LIGHT[2] if mode == "light" else CATEGORICAL_DARK[2],
    ).encode(
        x=alt.X("week:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
        y=alt.Y("pct_named:Q", title="% of kilometres named",
                scale=alt.Scale(domain=padded_domain(totals["pct_named"], 2.0))),
        tooltip=[alt.Tooltip("week:T", title="Week", format="%d %b %Y"),
                 alt.Tooltip("pct_named:Q", title="% named", format=".2f")],
    )
    return themed(line.properties(
        height=200,
        title=alt.TitleParams("Name coverage, length-weighted",
                              subtitle="Share of network kilometres carrying a primary name")), mode)
