"""Offline tests for the kepler.gl page builder. No browser, no network.

Run:  .venv/bin/python -m pytest -q test_kepler_view.py
"""

from __future__ import annotations

import json
import re

import pandas as pd

import kepler_view


def _datasets(html: str) -> list[dict]:
    m = re.search(r"const DATASETS = (.*);\n", html)
    assert m, "datasets block missing"
    return json.loads(m.group(1))


def test_light_theme_pinned_version_and_token_free_basemap():
    html = kepler_view.build_kepler_html({"p": pd.DataFrame({"lon": [8.66], "lat": [50.1]})})
    assert 'const THEME = "light"' in html
    assert 'const BASEMAP = "positron"' in html
    assert f"kepler.gl@{kepler_view.KEPLER_VERSION}/umd/keplergl.min.js" in html
    assert "alpha" not in kepler_view.KEPLER_VERSION


def test_dark_theme_switches_basemap():
    html = kepler_view.build_kepler_html({"p": pd.DataFrame({"lon": [1.0], "lat": [2.0]})}, theme="dark")
    assert 'const THEME = "dark"' in html and 'const BASEMAP = "dark-matter"' in html


def test_points_keep_lon_lat_and_drop_geojson():
    df = pd.DataFrame({"lon": [1.0], "lat": [2.0], "geom_type": ["Point"], "bbox_min_lon": [1.0],
                       "geometry_geojson": ['{"type": "Point", "coordinates": [1.0, 2.0]}']})
    out, truncated = kepler_view.prepare(df)
    assert list(out.columns) == ["lon", "lat"] and not truncated


def test_lines_keep_geojson_only():
    df = pd.DataFrame({"lon": [1.0], "lat": [2.0], "geom_type": ["LineString"], "class": ["motorway"],
                       "geometry_geojson": ['{"type": "LineString", "coordinates": [[1, 2], [3, 4]]}']})
    out, _ = kepler_view.prepare(df)
    assert "_geojson" in out.columns and "lon" not in out.columns and "class" in out.columns


def test_row_cap():
    df = pd.DataFrame({"lon": range(10), "lat": range(10)})
    out, truncated = kepler_view.prepare(df, max_rows=3)
    assert len(out) == 3 and truncated
    html = kepler_view.build_kepler_html({"p": df}, max_rows=3)
    assert _datasets(html)[0]["csv"].count("\n") == 4  # header + 3 rows


def test_unmappable_tables_are_skipped():
    html = kepler_view.build_kepler_html({"stats": pd.DataFrame({"km": [1.0]}),
                                          "p": pd.DataFrame({"lon": [1.0], "lat": [2.0]})})
    assert [d["id"] for d in _datasets(html)] == ["p"]


def test_data_cannot_break_out_of_the_script_block():
    hostile = "</script><script>alert(1)</script><!--"
    html = kepler_view.build_kepler_html({"p": pd.DataFrame({"lon": [1.0], "lat": [2.0], "name": [hostile]})})
    block = re.search(r"const DATASETS = (.*);\n", html).group(1)
    assert "<" not in block
    assert hostile in _datasets(html)[0]["csv"]
