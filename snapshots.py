"""Release statistics: load real Overture snapshots, compare two, and build a weekly series.

Two kinds of data live here and they are never mixed silently:

  real       rows read from stats_<release>.csv, produced by fetch_overture.py
             straight from the Overture S3 bucket. Two releases exist.
  simulated  weekly rows between and around those anchors, so the app has a
             time series to plot. Every simulated row is flagged in the
             `source` column and the app labels them.

The simulation exists because Overture ships monthly, and the brief asked for a
weekly view. It is anchored: on a release date the simulated value IS the real
value. Growth between anchors is interpolated geometrically, and one synthetic
data-quality incident is injected so the monitoring view has something to catch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data" / "overture"

# A conflation regression: two states lose part of their minor road network for
# two weeks, then recover. Entirely synthetic; this is the thing the coverage
# view is supposed to surface.
INCIDENT = {
    "name": "Conflation regression (synthetic)",
    "states": ("Bayern", "Baden-Württemberg"),
    "classes": ("tertiary", "residential", "unclassified"),
    "weeks": {"2026-08-05": 0.91, "2026-08-12": 0.96},  # multiplier applied to length_km
}

CLASS_ORDER = ["motorway", "trunk", "primary", "secondary", "tertiary",
               "unclassified", "residential", "living_street"]


@dataclass
class ReleaseSet:
    stats: pd.DataFrame          # long: release, state, class, segments, length_km, ...
    releases: list[str]          # sorted release ids, e.g. ["2026-07-22.0", "2026-08-19.0"]
    scopes: dict[str, dict | None] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return not self.stats.empty and len(self.releases) >= 1

    def comparable(self, rel_a: str, rel_b: str) -> tuple[bool, str]:
        """Two releases may be compared only if they were fetched with the same
        bounding box and road classes. Anything else compares apples to oranges."""
        sa, sb = self.scopes.get(rel_a), self.scopes.get(rel_b)
        if sa is None or sb is None:
            missing = [r for r, sc in ((rel_a, sa), (rel_b, sb)) if sc is None]
            return False, f"No scope record for {', '.join(missing)}; re-fetch with fetch_overture.py."
        if sa.get("bbox") != sb.get("bbox"):
            return False, f"Different areas: {sa.get('bbox')} vs {sb.get('bbox')}."
        if sa.get("classes") != sb.get("classes"):
            return False, (f"Different road classes: {', '.join(sa.get('classes', []))} "
                           f"vs {', '.join(sb.get('classes', []))}.")
        return True, ""

    def comparable_with(self, anchor: str) -> list[str]:
        return [r for r in self.releases if self.comparable(anchor, r)[0]]


def release_to_date(release: str) -> pd.Timestamp:
    """'2026-08-19.0' -> Timestamp('2026-08-19')."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", release)
    if not m:
        raise ValueError(f"Unrecognised release id: {release}")
    return pd.Timestamp(m.group(1))


def load_release_stats(directory: Path | str = DATA_DIR) -> ReleaseSet:
    directory = Path(directory)
    frames = []
    for path in sorted(directory.glob("stats_*.csv")):
        if path.stat().st_size == 0:
            continue  # a download still in flight
        df = pd.read_csv(path)
        if df.empty:
            continue
        frames.append(df)
    if not frames:
        return ReleaseSet(stats=pd.DataFrame(), releases=[])
    stats = pd.concat(frames, ignore_index=True)
    stats = stats[stats["state"] != "outside DE"].copy()
    stats["release_date"] = stats["release"].map(release_to_date)
    releases = sorted(stats["release"].unique())
    scopes: dict[str, dict | None] = {}
    for rel in releases:
        sp = directory / f"scope_{rel}.json"
        try:
            scopes[rel] = json.loads(sp.read_text()) if sp.exists() else None
        except json.JSONDecodeError:
            scopes[rel] = None
    return ReleaseSet(stats=stats, releases=releases, scopes=scopes)


# ---------------------------------------------------------------- comparison

def compare_releases(stats: pd.DataFrame, rel_a: str, rel_b: str,
                     dimension: str = "state") -> pd.DataFrame:
    """Delta between two releases along one dimension. Outer join, so a value
    that appears or disappears entirely still shows up as a row."""
    a = (stats[stats["release"] == rel_a].groupby(dimension, as_index=False)
         .agg(length_km_a=("length_km", "sum"), segments_a=("segments", "sum")))
    b = (stats[stats["release"] == rel_b].groupby(dimension, as_index=False)
         .agg(length_km_b=("length_km", "sum"), segments_b=("segments", "sum")))
    out = a.merge(b, on=dimension, how="outer").fillna(0.0)
    out["delta_km"] = out["length_km_b"] - out["length_km_a"]
    out["delta_segments"] = (out["segments_b"] - out["segments_a"]).astype(int)
    out["pct_change"] = np.where(
        out["length_km_a"] > 0, 100.0 * out["delta_km"] / out["length_km_a"], np.nan)
    return out.sort_values("delta_km", ascending=False).reset_index(drop=True)


def headline(stats: pd.DataFrame, release: str) -> dict[str, float]:
    sub = stats[stats["release"] == release]
    if sub.empty:
        return {}
    total_km = float(sub["length_km"].sum())
    segments = int(sub["segments"].sum())
    # Length-weighted so a million tiny residential stubs don't dominate the figure.
    named = float((sub["pct_named"] * sub["length_km"]).sum() / total_km) if total_km else 0.0
    return {"total_km": total_km, "segments": segments, "pct_named": named,
            "states": int(sub["state"].nunique()), "classes": int(sub["class"].nunique())}


# ---------------------------------------------------------------- weekly series

def _weekly_dates(anchors: list[pd.Timestamp], before: int, after: int) -> list[pd.Timestamp]:
    start = min(anchors) - pd.Timedelta(weeks=before)
    end = max(anchors) + pd.Timedelta(weeks=after)
    return list(pd.date_range(start, end, freq="7D"))


def build_weekly_series(stats: pd.DataFrame, weeks_before: int = 4, weeks_after: int = 4,
                        seed: int = 7, apply_incident: bool = True) -> pd.DataFrame:
    """Weekly length_km per state and class, anchored on the real releases.

    Returns columns: week, state, class, length_km, segments, pct_named, source, incident.
    On a release date the value equals the real release value exactly.
    """
    if stats.empty:
        return pd.DataFrame()

    releases = sorted(stats["release"].unique())
    anchors = {release_to_date(r): r for r in releases}
    anchor_dates = sorted(anchors)
    weeks = _weekly_dates(anchor_dates, weeks_before, weeks_after)
    _ = seed  # kept for API stability; the series is deterministic

    first, last = anchor_dates[0], anchor_dates[-1]
    base = stats.set_index(["release", "state", "class"])

    keys = stats[["state", "class"]].drop_duplicates().itertuples(index=False)
    rows = []
    for state, cls in keys:
        def val(rel: str, col: str) -> float | None:
            try:
                return float(base.loc[(rel, state, cls), col])
            except KeyError:
                return None

        km_first, km_last = val(releases[0], "length_km"), val(releases[-1], "length_km")
        if km_first is None and km_last is None:
            continue
        km_first = km_first if km_first is not None else km_last
        km_last = km_last if km_last is not None else km_first
        seg_first = val(releases[0], "segments") or val(releases[-1], "segments") or 0
        seg_last = val(releases[-1], "segments") or seg_first
        nm_first = val(releases[0], "pct_named")
        nm_last = val(releases[-1], "pct_named")
        nm_first = nm_first if nm_first is not None else (nm_last or 0.0)
        nm_last = nm_last if nm_last is not None else nm_first

        span_weeks = max(1, int((last - first).days / 7))
        growth = (km_last / km_first) ** (1 / span_weeks) if km_first > 0 else 1.0

        for week in weeks:
            offset = (week - first).days / 7
            is_anchor = week in anchors
            if is_anchor:
                rel = anchors[week]
                km = val(rel, "length_km")
                seg = val(rel, "segments")
                named = val(rel, "pct_named")
                if km is None:          # class absent from this release
                    km, seg, named = 0.0, 0, 0.0
                source, noise = "Overture release", 1.0
            else:
                km = km_first * (growth ** offset)
                frac = 0.0 if span_weeks == 0 else min(1.0, max(0.0, offset / span_weeks))
                seg = seg_first + (seg_last - seg_first) * frac
                named = nm_first + (nm_last - nm_first) * frac
                source = "simulated"
                # No random noise. With independent axes it reads as real volatility,
                # which it is not. The incident below is the only deliberate deviation.
                noise = 1.0

            incident = False
            key = week.strftime("%Y-%m-%d")
            if (apply_incident and not is_anchor and key in INCIDENT["weeks"]
                    and state in INCIDENT["states"] and cls in INCIDENT["classes"]):
                noise *= INCIDENT["weeks"][key]
                incident = True

            rows.append({
                "week": week, "state": state, "class": cls,
                "length_km": max(0.0, km * noise),
                "segments": int(round(max(0.0, seg * noise))),
                "pct_named": float(np.clip(named, 0, 100)),
                "source": source, "incident": incident,
            })

    df = pd.DataFrame(rows)
    df["class"] = pd.Categorical(df["class"], categories=CLASS_ORDER, ordered=True)
    return df.sort_values(["week", "state", "class"]).reset_index(drop=True)


def weekly_totals(weekly: pd.DataFrame, by: str | None = None) -> pd.DataFrame:
    """Roll the weekly series up to totals, optionally split by one dimension."""
    if weekly.empty:
        return weekly
    group = ["week"] + ([by] if by else [])
    out = (weekly.groupby(group, as_index=False, observed=True)
           .agg(length_km=("length_km", "sum"), segments=("segments", "sum"),
                incident=("incident", "any")))
    named = (weekly.assign(w=weekly["pct_named"] * weekly["length_km"])
             .groupby(group, as_index=False, observed=True)
             .agg(w=("w", "sum"), km=("length_km", "sum")))
    named["pct_named"] = np.where(named["km"] > 0, named["w"] / named["km"], np.nan)
    out = out.merge(named[group + ["pct_named"]], on=group, how="left")
    src = (weekly.groupby(group, as_index=False, observed=True)
           .agg(source=("source", lambda s: "Overture release"
                        if (s == "Overture release").all() else "simulated")))
    return out.merge(src, on=group, how="left")


def explain_anomaly(week: pd.Timestamp) -> str | None:
    """Name the injected incident only if the flagged week really is one of its
    weeks. A drop in any other week is unexplained and must be reported as such."""
    key = pd.Timestamp(week).strftime("%Y-%m-%d")
    return INCIDENT["name"] if key in INCIDENT["weeks"] else None


def detect_anomalies(totals: pd.DataFrame, threshold_pct: float = -1.0) -> pd.DataFrame:
    """Week-over-week drops beyond a threshold. The simplest monitor that works."""
    if totals.empty:
        return totals
    df = totals.sort_values("week").copy()
    df["prev_km"] = df["length_km"].shift(1)
    df["wow_pct"] = 100.0 * (df["length_km"] - df["prev_km"]) / df["prev_km"]
    return df[df["wow_pct"] < threshold_pct].dropna(subset=["wow_pct"])
