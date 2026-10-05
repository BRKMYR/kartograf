"""Tests for the low-footprint data layout: presets, estimates, compaction, gzip loading.

No network. The estimate logic runs against a fabricated Parquet footer table.
"""

from __future__ import annotations

import gzip
import json

import pandas as pd
import pytest

import fetch_overture as fo
import scopes
from data_loader import load_file


# ---------------------------------------------------------------- presets and folders

def test_scope_names():
    assert scopes.scope_name("berlin", scopes.PRESETS["berlin"]["bbox"]) == "berlin"
    # A bbox equal to a preset gets the preset name even without --preset.
    assert scopes.scope_name(None, scopes.PRESETS["germany"]["bbox"]) == "germany"
    custom = scopes.scope_name(None, (6.35, 49.1, 7.4, 49.64001))
    assert custom.startswith("bbox_") and "/" not in custom
    assert scopes.label("nrw") == "Nordrhein-Westfalen area"
    assert scopes.label("germany") == "Germany"
    assert scopes.label(custom).startswith("Custom area")


def test_dataset_dirs_skip_empty_archive_and_hidden(tmp_path):
    for name, content in (("germany", "x"), ("berlin", "x"), ("archive", "x"), (".meta", "x"), ("empty", "")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "stats_2026-08-19.0.csv").write_text(content)
    names = [d.name for d in scopes.dataset_dirs(tmp_path)]
    assert names == ["germany", "berlin"]


def test_roads_path_prefers_gzip(tmp_path):
    (tmp_path / "roads_r.geojson").write_text("{}")
    assert scopes.roads_path(tmp_path, "r").name == "roads_r.geojson"
    (tmp_path / "roads_r.geojson.gz").write_bytes(gzip.compress(b"{}"))
    assert scopes.roads_path(tmp_path, "r").name == "roads_r.geojson.gz"
    assert scopes.roads_path(tmp_path, "missing") is None
    assert scopes.read_bytes_maybe_gzip(tmp_path / "roads_r.geojson.gz") == b"{}"
    assert scopes.read_bytes_maybe_gzip(tmp_path / "roads_r.geojson") == b"{}"


# ---------------------------------------------------------------- estimate

def _footer(rows):
    """rows: (file, row_group, xmin_lo, xmin_hi, ymin_lo, ymin_hi, geometry_bytes)"""
    out = []
    for f, rg, x_lo, x_hi, y_lo, y_hi, geom in rows:
        out += [
            {"file_name": f, "row_group_id": rg, "path_in_schema": "bbox, xmin", "total_compressed_size": 10, "mn": x_lo, "mx": x_hi},
            {"file_name": f, "row_group_id": rg, "path_in_schema": "bbox, ymin", "total_compressed_size": 10, "mn": y_lo, "mx": y_hi},
            {"file_name": f, "row_group_id": rg, "path_in_schema": "geometry", "total_compressed_size": geom, "mn": None, "mx": None},
            {"file_name": f, "row_group_id": rg, "path_in_schema": "routes", "total_compressed_size": 999, "mn": None, "mx": None},
        ]
    return pd.DataFrame(out)


def test_overlapping_bytes_counts_only_overlapping_groups_and_needed_columns():
    meta = _footer([
        ("a", 0, 13.0, 13.5, 52.3, 52.6, 1000),   # inside Berlin box
        ("a", 1, 13.6, 14.0, 52.5, 52.9, 2000),   # overlaps the edge
        ("b", 0, 6.0, 7.0, 49.0, 49.5, 5000),     # Saarland, outside
        ("b", 1, 20.0, 21.0, 52.0, 53.0, 7000),   # Poland, outside
    ])
    groups, total, nbytes = fo.overlapping_bytes(meta, scopes.PRESETS["berlin"]["bbox"], fo.SEGMENT_COLUMNS)
    assert (groups, total) == (2, 4)
    # geometry + two bbox columns for two groups; the unused routes column is not counted
    assert nbytes == (1000 + 10 + 10) + (2000 + 10 + 10)


# ---------------------------------------------------------------- slim features and compaction

def test_round_coords_nested():
    assert fo.round_coords([13.123456789, 52.987654321]) == [13.12346, 52.98765]
    assert fo.round_coords([[[1.000001, 2.000009]]]) == [[[1.0, 2.00001]]]


def test_slim_feature_drops_empty_and_default_props():
    geom = {"type": "LineString", "coordinates": [[13.1234567, 52.1], [13.2, 52.2]]}
    f = fo.slim_feature("id1", "motorway", "", "Berlin", 1.234567, True, geom)
    assert f["properties"] == {"class": "motorway", "state": "Berlin", "length_km": 1.2346}
    g = fo.slim_feature("id2", "trunk", "B96", "Berlin", 2.0, False, geom)
    assert g["properties"]["name"] == "B96" and g["properties"]["is_named"] is False
    assert f["geometry"]["coordinates"][0] == [13.12346, 52.1]


def _legacy_file(path, n=3):
    feats = [{"type": "Feature", "id": f"id{i}",
              "properties": {"class": "motorway", "name": "" if i else "A100", "state": "Berlin",
                             "length_km": 1.5 + i, "is_named": i == 0, "release": "r"},
              "geometry": {"type": "LineString", "coordinates": [[13.1234567, 52.1], [13.2, 52.2 + i]]}}
             for i in range(n)]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return feats


def test_compact_existing_moves_verifies_and_compresses(tmp_path):
    root = tmp_path
    rel = "2026-08-19.0"
    feats = _legacy_file(root / f"roads_{rel}.geojson", n=50)
    (root / f"stats_{rel}.csv").write_text("release,state\n")
    (root / f"scope_{rel}.json").write_text(json.dumps(
        {"release": rel, "bbox": list(scopes.PRESETS["berlin"]["bbox"]), "classes": ["motorway"]}))

    fo.compact_existing(root)

    target = root / "berlin"
    assert not (root / f"roads_{rel}.geojson").exists()
    assert not (target / f"roads_{rel}.geojson").exists()          # original removed after verify
    gz = target / f"roads_{rel}.geojson.gz"
    back = json.loads(gzip.decompress(gz.read_bytes()))["features"]
    assert [f["id"] for f in back] == [f["id"] for f in feats]
    assert sum(f["properties"]["length_km"] for f in back) == pytest.approx(
        sum(f["properties"]["length_km"] for f in feats))
    assert (target / f"stats_{rel}.csv").exists() and (target / f"scope_{rel}.json").exists()


def test_compact_file_keeps_original_when_verification_fails(tmp_path, monkeypatch):
    src = tmp_path / "roads_x.geojson"
    _legacy_file(src)
    real = fo.write_features_gz

    def lossy(path, features):
        return real(path, list(features)[:-1])     # silently drops a feature

    monkeypatch.setattr(fo, "write_features_gz", lossy)
    with pytest.raises(RuntimeError):
        fo.compact_file(src)
    assert src.exists() and not (tmp_path / "roads_x.geojson.gz").exists()


def test_loader_reads_gzip_geojson_and_keeps_provider_length(tmp_path):
    src = tmp_path / "roads_r.geojson"
    _legacy_file(src)
    fo.compact_file(src)
    gz = tmp_path / "roads_r.geojson.gz"
    t = load_file("berlin_roads_r.geojson.gz", gz.read_bytes())
    assert t.kind == "geojson" and t.feature_count == 3
    assert t.name == "berlin_roads_r"
    assert list(t.df["length_km"]) == [1.5, 2.5, 3.5]
    assert "geom_length_km" in t.df.columns


def test_scope_record_carries_licence_and_attribution(tmp_path):
    fo.write_scope("2026-08-19.0", scopes.PRESETS["berlin"]["bbox"], ("motorway",), tmp_path)
    rec = json.loads((tmp_path / "scope_2026-08-19.0.json").read_text())
    assert rec["license"] == "ODbL-1.0"
    assert "OpenStreetMap contributors" in rec["attribution"]
    assert rec["source"] == "Overture Maps Foundation, overturemaps.org"
    assert rec["bbox"] == list(scopes.PRESETS["berlin"]["bbox"])
