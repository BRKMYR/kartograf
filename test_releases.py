"""Tests for release statistics, the weekly series, and the chart specs.

These run on fabricated release CSVs, so they do not need the Overture download.
"""

from __future__ import annotations

import pandas as pd
import pytest

import charts
import snapshots

REL_A, REL_B = "2026-07-22.0", "2026-08-19.0"
STATES = ["Bayern", "Baden-Württemberg", "Berlin"]
CLASSES = ["motorway", "primary", "tertiary", "residential"]


def _stats_frame(release: str, factor: float, drop: tuple[str, str] | None = None) -> pd.DataFrame:
    rows = []
    for si, state in enumerate(STATES):
        for ci, cls in enumerate(CLASSES):
            if drop and (state, cls) == drop:
                continue
            base = 1000.0 * (si + 1) * (ci + 1)
            rows.append({"release": release, "state": state, "class": cls,
                         "segments": int(base / 2), "length_km": round(base * factor, 2),
                         "mean_segment_m": 500.0, "pct_named": 60.0 + ci,
                         "pct_with_route": 30.0})
    return pd.DataFrame(rows)


def _write_scope(directory, release, bbox=(5.87, 47.27, 15.04, 55.06), classes=CLASSES):
    import json
    (directory / f"scope_{release}.json").write_text(json.dumps(
        {"release": release, "bbox": list(bbox), "classes": sorted(classes)}))


@pytest.fixture
def release_dir(tmp_path):
    _write_scope(tmp_path, REL_A)
    _write_scope(tmp_path, REL_B)
    _stats_frame(REL_A, 1.00).to_csv(tmp_path / f"stats_{REL_A}.csv", index=False)
    # Release B grows 2%, and Berlin loses its tertiary roads entirely.
    _stats_frame(REL_B, 1.02, drop=("Berlin", "tertiary")).to_csv(
        tmp_path / f"stats_{REL_B}.csv", index=False)
    (tmp_path / "stats_empty.csv").write_text("")  # a download still in flight
    return tmp_path


@pytest.fixture
def rs(release_dir) -> snapshots.ReleaseSet:
    return snapshots.load_release_stats(release_dir)


# ---------------------------------------------------------------- loading

def test_loads_both_releases_and_skips_empty_file(rs):
    assert rs.available
    assert rs.releases == [REL_A, REL_B]
    assert set(rs.stats["state"]) == set(STATES)


def test_release_date_parsing():
    assert snapshots.release_to_date("2026-08-19.0") == pd.Timestamp("2026-08-19")
    with pytest.raises(ValueError):
        snapshots.release_to_date("not-a-release")


def test_outside_de_rows_are_dropped(tmp_path):
    df = _stats_frame(REL_A, 1.0)
    df.loc[len(df)] = {"release": REL_A, "state": "outside DE", "class": "motorway",
                       "segments": 10, "length_km": 99.0, "mean_segment_m": 1.0,
                       "pct_named": 0.0, "pct_with_route": 0.0}
    df.to_csv(tmp_path / f"stats_{REL_A}.csv", index=False)
    assert "outside DE" not in set(snapshots.load_release_stats(tmp_path).stats["state"])


# ---------------------------------------------------------------- comparison

def test_compare_releases_by_state(rs):
    cmp = snapshots.compare_releases(rs.stats, REL_A, REL_B, "state")
    assert set(cmp["state"]) == set(STATES)
    # Bayern and Baden-Württemberg grew 2% with nothing removed.
    by = cmp.set_index("state")
    assert by.loc["Bayern", "pct_change"] == pytest.approx(2.0, abs=0.01)
    # Berlin lost a whole class, so it must be net negative despite the 2% growth.
    assert by.loc["Berlin", "delta_km"] < 0


def test_compare_releases_handles_class_present_in_only_one(rs):
    cmp = snapshots.compare_releases(rs.stats, REL_A, REL_B, "class")
    tert = cmp.set_index("class").loc["tertiary"]
    assert tert["length_km_a"] > 0 and tert["length_km_b"] > 0
    assert tert["delta_km"] < 0  # Berlin's tertiary disappeared


def test_headline_is_length_weighted(rs):
    h = snapshots.headline(rs.stats, REL_A)
    assert h["states"] == 3 and h["classes"] == 4
    assert h["total_km"] == pytest.approx(rs.stats.query("release == @REL_A")["length_km"].sum())
    # Length-weighted name coverage must sit inside the per-row range, not be a flat mean.
    assert 60.0 <= h["pct_named"] <= 63.0


# ---------------------------------------------------------------- weekly series

def test_weekly_anchors_equal_the_real_release_values(rs):
    weekly = snapshots.build_weekly_series(rs.stats)
    for rel in (REL_A, REL_B):
        date = snapshots.release_to_date(rel)
        got = (weekly[weekly["week"] == date]
               .groupby(["state", "class"], observed=True)["length_km"].sum())
        want = (rs.stats[rs.stats["release"] == rel]
                .groupby(["state", "class"], observed=True)["length_km"].sum())
        for key, value in want.items():
            assert got[key] == pytest.approx(value, abs=0.01), f"{rel} {key}"
        assert (weekly[weekly["week"] == date]["source"] == "Overture release").all()


def test_weekly_covers_requested_window_and_is_deterministic(rs):
    a = snapshots.build_weekly_series(rs.stats, weeks_before=4, weeks_after=4, seed=7)
    b = snapshots.build_weekly_series(rs.stats, weeks_before=4, weeks_after=4, seed=7)
    pd.testing.assert_frame_equal(a, b)
    weeks = sorted(a["week"].unique())
    # 4 before + anchor A + 3 between + anchor B + 4 after
    assert len(weeks) == 13
    assert weeks[0] == pd.Timestamp("2026-06-24")
    assert weeks[-1] == pd.Timestamp("2026-09-16")
    assert pd.Timestamp("2026-07-22") in weeks and pd.Timestamp("2026-08-19") in weeks
    assert all((weeks[i + 1] - weeks[i]).days == 7 for i in range(len(weeks) - 1))


def test_incident_hits_only_the_named_states_and_classes(rs):
    weekly = snapshots.build_weekly_series(rs.stats, apply_incident=True)
    flagged = weekly[weekly["incident"]]
    assert not flagged.empty
    assert set(flagged["state"]) <= set(snapshots.INCIDENT["states"])
    assert set(flagged["class"].astype(str)) <= set(snapshots.INCIDENT["classes"])
    assert set(flagged["week"].dt.strftime("%Y-%m-%d")) == set(snapshots.INCIDENT["weeks"])

    clean = snapshots.build_weekly_series(rs.stats, apply_incident=False)
    assert not clean["incident"].any()
    # Without the incident the same week is materially higher.
    wk = pd.Timestamp("2026-08-05")
    assert clean.loc[clean.week == wk, "length_km"].sum() > \
           weekly.loc[weekly.week == wk, "length_km"].sum()


def test_anomaly_detector_finds_the_incident(rs):
    totals = snapshots.weekly_totals(snapshots.build_weekly_series(rs.stats))
    found = snapshots.detect_anomalies(totals, threshold_pct=-0.5)
    assert pd.Timestamp("2026-08-05") in set(found["week"])


def test_weekly_totals_rolls_up_consistently(rs):
    weekly = snapshots.build_weekly_series(rs.stats)
    totals = snapshots.weekly_totals(weekly)
    assert len(totals) == weekly["week"].nunique()
    assert totals["length_km"].sum() == pytest.approx(weekly["length_km"].sum())
    assert totals["pct_named"].between(0, 100).all()
    by_state = snapshots.weekly_totals(weekly, by="state")
    assert set(by_state.columns) >= {"week", "state", "length_km", "pct_named"}
    assert by_state["length_km"].sum() == pytest.approx(weekly["length_km"].sum())


def test_empty_stats_do_not_crash():
    empty = pd.DataFrame()
    assert snapshots.build_weekly_series(empty).empty
    assert snapshots.weekly_totals(empty).empty
    assert snapshots.detect_anomalies(empty).empty


# ---------------------------------------------------------------- charts

@pytest.mark.parametrize("mode", ["light", "dark"])
def test_chart_specs_build_in_both_modes(rs, mode):
    weekly = snapshots.build_weekly_series(rs.stats)
    totals = snapshots.weekly_totals(weekly)
    cmp = snapshots.compare_releases(rs.stats, REL_A, REL_B, "state")

    for chart in (charts.total_km_over_time(totals, mode),
                  charts.km_by_class_small_multiples(weekly, mode),
                  charts.name_coverage_over_time(totals, mode),
                  charts.delta_by_dimension(cmp, "state", mode)):
        spec = chart.to_dict()          # raises if the spec is invalid
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")


def test_class_colours_are_fixed_not_positional():
    """A class keeps its hue whether or not other classes are present."""
    scale = charts.class_scale("light")
    assert scale.domain == charts.CLASS_ORDER == snapshots.CLASS_ORDER
    assert scale.range[:3] == charts.CATEGORICAL_LIGHT[:3]
    assert len(set(scale.range)) == len(scale.range)
    # Primary is slot 3 regardless of the data: build a chart without trunk.
    spec_scale = charts.class_scale("dark")
    assert spec_scale.range[spec_scale.domain.index("primary")] == charts.CATEGORICAL_DARK[2]


def test_small_multiples_are_indexed_on_a_shared_scale(rs):
    """Classes differ by an order of magnitude; the panels share one % axis."""
    weekly = snapshots.build_weekly_series(rs.stats)
    spec = charts.km_by_class_small_multiples(weekly, "light").to_dict()
    assert spec.get("resolve", {}).get("scale", {}).get("y") != "independent"
    layers = spec["spec"]["layer"]
    assert any(l.get("encoding", {}).get("y", {}).get("field") == "pct_vs_first" for l in layers)
    # Every layer must draw from the facet's partition, never from its own dataset,
    # or each panel shows every class.
    assert all("data" not in l for l in layers)
    assert "data" in spec or "datasets" in spec


def test_no_chart_uses_a_second_y_axis(rs):
    weekly = snapshots.build_weekly_series(rs.stats)
    totals = snapshots.weekly_totals(weekly)
    for chart in (charts.total_km_over_time(totals, "light"),
                  charts.name_coverage_over_time(totals, "light")):
        spec = chart.to_dict()
        layers = spec.get("layer", [spec])
        y_titles = {l.get("encoding", {}).get("y", {}).get("title")
                    for l in layers if isinstance(l, dict)}
        assert len([t for t in y_titles if t]) <= 1


# ---------------------------------------------------------------- scope and attribution

def test_releases_with_same_scope_are_comparable(rs):
    ok, why = rs.comparable(REL_A, REL_B)
    assert ok and why == ""
    assert rs.comparable_with(REL_B) == [REL_A, REL_B]


def test_different_bbox_blocks_comparison(release_dir):
    _write_scope(release_dir, REL_B, bbox=(6.35, 49.11, 7.40, 49.64))
    rs = snapshots.load_release_stats(release_dir)
    ok, why = rs.comparable(REL_A, REL_B)
    assert not ok and "Different areas" in why
    assert rs.comparable_with(REL_B) == [REL_B]


def test_different_classes_block_comparison(release_dir):
    _write_scope(release_dir, REL_B, classes=CLASSES + ["residential_extra"])
    ok, why = snapshots.load_release_stats(release_dir).comparable(REL_A, REL_B)
    assert not ok and "Different road classes" in why


def test_missing_scope_record_is_not_comparable(release_dir):
    (release_dir / f"scope_{REL_A}.json").unlink()
    ok, why = snapshots.load_release_stats(release_dir).comparable(REL_A, REL_B)
    assert not ok and REL_A in why


def test_anomaly_is_attributed_only_in_incident_weeks():
    assert snapshots.explain_anomaly(pd.Timestamp("2026-08-05")) == snapshots.INCIDENT["name"]
    assert snapshots.explain_anomaly(pd.Timestamp("2026-08-19")) is None
    assert snapshots.explain_anomaly(pd.Timestamp("2026-07-01")) is None
