"""Offline tests for the web app's chart and map derivation. No Ollama, no network.

Run:  .venv/bin/python -m pytest -q test_server.py
"""

from __future__ import annotations

import pandas as pd

import server


def test_breakdown_reuses_the_model_filter():
    d = server.derived_queries("SELECT count(*) AS n FROM places WHERE category = 'cafe';")
    assert d["breakdown"] == ("SELECT category, count(*) AS count FROM places WHERE category = 'cafe' "
                              "GROUP BY category ORDER BY count DESC LIMIT 12")
    assert d["map"] == "SELECT name, category, lon, lat FROM places WHERE category = 'cafe' LIMIT 500"


def test_road_totals_break_down_by_class_in_km():
    d = server.derived_queries("SELECT SUM(length_km) AS total_road_km FROM roads")
    assert d == {"breakdown": "SELECT class, SUM(length_km) AS km FROM roads  GROUP BY class ORDER BY km DESC LIMIT 12"}


def test_complex_or_non_aggregate_queries_are_left_alone():
    for sql in ["SELECT class FROM roads GROUP BY 1 ORDER BY sum(length_km) DESC LIMIT 1",
                "WITH x AS (SELECT 1) SELECT count(*) FROM places",
                "SELECT count(*) FROM places p JOIN roads r ON true",
                "SELECT name FROM places WHERE category = 'museum'",
                "SELECT count(*) FROM (SELECT * FROM places)"]:
        assert server.derived_queries(sql) == {}, sql


def test_pick_chart_prefers_a_breakdown_over_a_single_number():
    total = pd.DataFrame({"sum": [1770.85]})
    by_class = pd.DataFrame({"class": ["motorway", "trunk"], "km": [460.3, 206.6]})
    chart = server.pick_chart([total, by_class])
    assert chart["type"] == "bar" and chart["label"] == "class" and chart["value"] == "km"
    assert [r["label"] for r in chart["rows"]] == ["motorway", "trunk"]


def test_pick_chart_falls_back_to_a_number_card():
    chart = server.pick_chart([pd.DataFrame({"count_star()": [13]})])
    assert chart == {"type": "number", "label": "count_star()", "value": 13.0}


def test_pick_chart_ignores_coordinates_and_caps_bars():
    df = pd.DataFrame({"name": [f"p{i}" for i in range(30)], "lon": [8.6] * 30, "lat": [50.1] * 30,
                       "n": list(range(30))})
    chart = server.pick_chart([df])
    assert chart["value"] == "n" and len(chart["rows"]) == server.CHART_MAX_BARS
    assert chart["rows"][0]["value"] == 29


def test_highlight_only_whitelisted_columns_and_bound_values():
    from data_loader import DataStore
    store = DataStore()
    store.con.execute("CREATE TABLE roads AS SELECT * FROM (VALUES ('motorway', 8.6, 50.1), ('trunk', 8.7, 50.2)) t(class, lon, lat)")
    layer = server.highlight_layer(store, "roads", "class", "motorway")
    assert layer["rows"] == 1 and layer["id"] == "highlight"
    assert server.highlight_layer(store, "roads", "lon", "8.6") is None            # not whitelisted
    assert server.highlight_layer(store, "roads", "class", "x' OR '1'='1") is None  # bound, matches nothing


def test_bar_charts_are_tagged_with_their_table():
    chart = {"type": "bar", "label": "class", "value": "km", "rows": []}
    tagged = server.chart_source(chart, ["SELECT class, SUM(length_km) AS km FROM roads GROUP BY class"])
    assert tagged["table"] == "roads"
    assert "table" not in server.chart_source({**chart, "label": "name"}, ["SELECT name FROM roads"])
