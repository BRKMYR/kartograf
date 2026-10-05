"""Deterministic tests: loader, SQL guard, statistics, and the tool loop with a fake model.

Run:  .venv/bin/python -m pytest -q
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import llm
from data_loader import DataStore, check_sql_is_read_only, load_file, sanitize_table_name, table_statistics

SAMPLE = Path(__file__).parent / "sample_data"


@pytest.fixture
def store() -> DataStore:
    s = DataStore()
    for p in sorted(SAMPLE.glob("*")):
        s.add(load_file(p.name, p.read_bytes(), s.tables.keys()))
    return s


# ----------------------------------------------------------------------------- loading

def test_sample_files_load(store):
    assert set(store.tables) == {"charging_sessions", "berlin_districts"}
    assert store.tables["charging_sessions"].feature_count == 400
    geo = store.tables["berlin_districts"]
    assert geo.geom_types == {"Polygon": 6, "Point": 12}
    assert {"lon", "lat", "bbox_min_lon", "geometry_geojson"} <= set(geo.df.columns)


def test_geojson_variants():
    point = {"type": "Feature", "properties": {"n": 1}, "geometry": {"type": "Point", "coordinates": [10.0, 50.0]}}
    t = load_file("one.geojson", json.dumps(point).encode())
    assert t.feature_count == 1 and t.df.loc[0, "lon"] == 10.0 and t.df.loc[0, "lat"] == 50.0

    bare = {"type": "LineString", "coordinates": [[0, 0], [2, 2]]}
    t = load_file("line.json", json.dumps(bare).encode())
    assert t.df.loc[0, "geom_type"] == "LineString"
    assert t.df.loc[0, "lon"] == 1.0 and t.df.loc[0, "bbox_max_lat"] == 2.0

    nested = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"tags": {"a": 1}, "weird key!": "x"}, "geometry": None}]}
    t = load_file("nested.geojson", json.dumps(nested).encode())
    assert t.df.loc[0, "tags"] == '{"a": 1}' and "weird_key_" in t.df.columns
    assert t.geom_types == {"null": 1}


def test_csv_delimiter_and_dates():
    raw = b"id;date;value\n1;2026-01-02;3,5\n2;2026-01-03;4\n"
    t = load_file("semi.csv", raw)
    assert list(t.df.columns) == ["id", "date", "value"]
    assert str(t.df["date"].dtype).startswith("datetime64")


def test_table_names_are_safe_and_unique():
    assert sanitize_table_name("My Data (2026).csv") == "my_data_2026"
    assert sanitize_table_name("2024.geojson") == "t_2024"
    assert sanitize_table_name("a.csv", existing=["a"]) == "a_2"


# ----------------------------------------------------------------------------- guard

@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM charging_sessions",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "select * from charging_sessions -- ; drop table charging_sessions",
    "SELECT * FROM charging_sessions WHERE operator = 'Call Center'",
    "SELECT * FROM t WHERE note = 'it''s a set'",
    "DESCRIBE charging_sessions",
])
def test_guard_allows_read_only(sql):
    check_sql_is_read_only(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE charging_sessions",
    "SELECT 1; DROP TABLE charging_sessions",
    "COPY charging_sessions TO '/tmp/x.csv'",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "INSTALL httpfs",
    "SET enable_external_access = true",
    "",
    "/* DROP */ DELETE FROM charging_sessions",
])
def test_guard_blocks_everything_else(sql):
    with pytest.raises(ValueError):
        check_sql_is_read_only(sql)


def test_store_refuses_writes_even_if_guard_is_bypassed(store):
    # Belt and braces: the guard runs inside run_sql too.
    with pytest.raises(ValueError):
        store.run_sql("DELETE FROM charging_sessions")
    assert store.run_sql("SELECT count(*) AS n FROM charging_sessions").iloc[0, 0] == 400


# ----------------------------------------------------------------------------- statistics

def test_statistics_geo_and_csv(store):
    geo = table_statistics(store.tables["berlin_districts"])
    assert geo["rows"] == 18 and "bbox" in geo and len(geo["points"]) == 18
    assert geo["bbox"]["min_lon"] < geo["bbox"]["max_lon"]
    csv = table_statistics(store.tables["charging_sessions"])
    assert "points" in csv and len(csv["points"]) == 400   # lon/lat columns detected
    assert "energy_kwh" in csv["numeric_describe"].index


def test_schema_description_mentions_tables_and_spatial_status(store):
    text = store.schema_description()
    assert 'Table "charging_sessions"' in text and 'Table "berlin_districts"' in text
    assert "geometry_geojson" not in text.split("Sample rows")[1].split("\n")[1]  # samples omit raw geometry
    assert "spatial extension" in text.lower()


# ----------------------------------------------------------------------------- tool loop with a fake Claude

class _FakeUsage:
    input_tokens = 100
    output_tokens = 20


class _FakeResponse:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()
        self.model = "fake-claude"


def _block(**kw):
    return SimpleNamespace(**kw)


class _FakeMessages:
    """First call asks for a bad query, second fixes it, third answers."""

    def __init__(self):
        self.calls = []
        self.scripted = [
            _FakeResponse([_block(type="tool_use", id="t1", name="run_sql",
                                  input={"sql": "SELECT count(*) FROM nope", "purpose": "count"})], "tool_use"),
            _FakeResponse([_block(type="tool_use", id="t2", name="run_sql",
                                  input={"sql": "SELECT count(*) AS n FROM charging_sessions", "purpose": "count"})], "tool_use"),
            _FakeResponse([_block(type="text", text="There are 400 sessions.")], "end_turn"),
        ]

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.scripted.pop(0)


def test_anthropic_loop_retries_bad_sql_and_returns_answer(store, monkeypatch):
    fake_messages = _FakeMessages()
    fake_client = SimpleNamespace(beta=SimpleNamespace(messages=fake_messages))
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: fake_client)

    result, history = llm.chat_anthropic(store, [], "How many sessions?")

    assert result.answer == "There are 400 sessions."
    assert [q.ok for q in result.queries] == [False, True]
    assert result.queries[1].rows == 1
    # The failed query went back to the model flagged as an error.
    err_msg = history[2]["content"][0]
    assert err_msg["type"] == "tool_result" and err_msg["is_error"] is True
    # Request shape: read-only tool, adaptive thinking, fallbacks on, cached system prompt.
    req = fake_messages.calls[0]
    assert req["tools"][0]["name"] == "run_sql" and req["tools"][0]["strict"] is True
    assert req["thinking"] == {"type": "adaptive"} and req["fallbacks"] == "default"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "charging_sessions" in req["system"][0]["text"]
    assert result.input_tokens == 300 and result.output_tokens == 60


def test_anthropic_loop_stops_after_max_rounds(store, monkeypatch):
    class Endless:
        def create(self, **kwargs):
            return _FakeResponse([_block(type="tool_use", id="x", name="run_sql",
                                         input={"sql": "SELECT 1", "purpose": "loop"})], "tool_use")
    monkeypatch.setattr("anthropic.Anthropic",
                        lambda *a, **k: SimpleNamespace(beta=SimpleNamespace(messages=Endless())))
    result, _ = llm.chat_anthropic(store, [], "loop forever")
    assert "too many query rounds" in result.answer
    assert len(result.queries) == llm.MAX_TOOL_ROUNDS + 1


def test_result_truncation_note(store):
    records = []
    text, is_err = llm.execute_run_sql(store, {"sql": "SELECT * FROM charging_sessions", "purpose": "dump"}, records)
    assert not is_err and records[0].rows == llm.MAX_RESULT_ROWS
    assert "truncated" in text


def test_source_property_is_never_overwritten_by_derived_column():
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"length_km": 5.0, "lon": "not-a-number"},
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [0, 1]]}}]}
    t = load_file("roads.geojson", json.dumps(fc).encode())
    row = t.df.iloc[0]
    assert row["length_km"] == 5.0                                  # provider value kept
    assert abs(row["geom_length_km"] - 111.2) < 0.1                 # computed value alongside
    assert row["lon"] == "not-a-number" and row["geom_lon"] == 0.0
    assert any("length_km" in n for n in t.notes)
    s = DataStore(); s.add(t)
    assert "geom_length_km" in s.schema_description()


def test_is_named_default_is_restored():
    """Compact road files omit is_named when it is True; the loader restores it."""
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "a", "properties": {"class": "motorway", "name": "A 5"},
         "geometry": {"type": "LineString", "coordinates": [[8.6, 50.1], [8.61, 50.1]]}},
        {"type": "Feature", "id": "b", "properties": {"class": "motorway", "is_named": False},
         "geometry": {"type": "LineString", "coordinates": [[8.6, 50.1], [8.62, 50.1]]}},
    ]}
    t = load_file("roads.geojson", json.dumps(fc).encode())
    assert t.df.set_index("feature_id")["is_named"].to_dict() == {"a": True, "b": False}
