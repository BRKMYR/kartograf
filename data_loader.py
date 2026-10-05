"""Load CSV and GeoJSON files into pandas and DuckDB.

GeoJSON handling is pure Python on purpose: no GDAL, no shapely. Each feature
becomes one row with its properties flattened into columns plus derived
geometry columns (type, representative point, bounding box, GeoJSON string).
That is enough for statistics, maps, and most analytical questions. If the
DuckDB spatial extension is available it is loaded so the model can also use
ST_* functions on the geometry column.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from typing import Any, Iterable

import duckdb
import pandas as pd

GEO_COLUMNS = [
    "geom_type",
    "lon",
    "lat",
    "length_km",
    "bbox_min_lon",
    "bbox_min_lat",
    "bbox_max_lon",
    "bbox_max_lat",
    "geometry_geojson",
]

EARTH_RADIUS_KM = 6371.0088  # mean radius, IUGG


@dataclass
class LoadedTable:
    name: str
    source_file: str
    kind: str  # "csv" or "geojson"
    df: pd.DataFrame
    feature_count: int
    geom_types: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def is_geo(self) -> bool:
        return self.kind == "geojson"


def sanitize_table_name(filename: str, existing: Iterable[str] = ()) -> str:
    """Turn a filename into a safe SQL identifier, unique against existing."""
    base = re.sub(r"\.(csv|geojson|json)$", "", filename, flags=re.IGNORECASE)
    base = re.sub(r"[^0-9a-zA-Z_]+", "_", base).strip("_").lower() or "table"
    if base[0].isdigit():
        base = "t_" + base
    name, i = base, 2
    taken = set(existing)
    while name in taken:
        name = f"{base}_{i}"
        i += 1
    return name


def _iter_coords(coords: Any):
    """Yield (lon, lat) pairs from any nested GeoJSON coordinate array."""
    if not coords:
        return
    if isinstance(coords[0], (int, float)):
        yield float(coords[0]), float(coords[1])
        return
    for c in coords:
        yield from _iter_coords(c)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lon, lat) points in kilometres."""
    from math import asin, cos, radians, sin, sqrt

    lon1, lat1 = radians(a[0]), radians(a[1])
    lon2, lat2 = radians(b[0]), radians(b[1])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(min(1.0, h)))


def _path_length_km(path: Any) -> float:
    """Length of one coordinate path: [[lon, lat], [lon, lat], ...]."""
    pts = [(float(p[0]), float(p[1])) for p in path if isinstance(p, (list, tuple)) and len(p) >= 2]
    return sum(_haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def geometry_length_km(geom: dict | None) -> float | None:
    """Geodesic length of a line geometry. None for anything that is not a line.

    Polygons deliberately return None: a perimeter is not a length, and
    summing perimeters into a "kilometres" figure would be wrong.
    """
    if not geom or "type" not in geom:
        return None
    gtype, coords = geom["type"], geom.get("coordinates")
    if gtype == "LineString":
        return _path_length_km(coords or [])
    if gtype == "MultiLineString":
        return sum(_path_length_km(p) for p in (coords or []))
    if gtype == "GeometryCollection":
        parts = [geometry_length_km(g) for g in geom.get("geometries", [])]
        vals = [p for p in parts if p is not None]
        return sum(vals) if vals else None
    return None


def geometry_summary(geom: dict | None) -> dict[str, Any]:
    """Derive type, representative point and bbox from a GeoJSON geometry."""
    out: dict[str, Any] = {c: None for c in GEO_COLUMNS}
    if not geom or "type" not in geom:
        return out
    gtype = geom["type"]
    out["geom_type"] = gtype
    out["geometry_geojson"] = json.dumps(geom, separators=(",", ":"))
    out["length_km"] = geometry_length_km(geom)
    if gtype == "GeometryCollection":
        pts = [p for g in geom.get("geometries", []) for p in _iter_coords(g.get("coordinates"))]
    else:
        pts = list(_iter_coords(geom.get("coordinates")))
    if not pts:
        return out
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    out["bbox_min_lon"], out["bbox_max_lon"] = min(lons), max(lons)
    out["bbox_min_lat"], out["bbox_max_lat"] = min(lats), max(lats)
    if gtype == "Point":
        out["lon"], out["lat"] = pts[0]
    else:
        # Mean of vertices is a cheap, dependency-free representative point.
        out["lon"] = sum(lons) / len(lons)
        out["lat"] = sum(lats) / len(lats)
    return out


def geojson_to_dataframe(data: dict) -> tuple[pd.DataFrame, dict[str, int], list[str]]:
    notes: list[str] = []
    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif data.get("type") == "Feature":
        features = [data]
    elif "type" in data and "coordinates" in data:
        features = [{"type": "Feature", "properties": {}, "geometry": data}]
    else:
        raise ValueError("Not a GeoJSON FeatureCollection, Feature or Geometry")

    rows: list[dict[str, Any]] = []
    geom_types: dict[str, int] = {}
    collisions: set[str] = set()
    for i, f in enumerate(features):
        props = f.get("properties") or {}
        row: dict[str, Any] = {"feature_id": f.get("id", i)}
        for k, v in props.items():
            key = re.sub(r"[^0-9a-zA-Z_]+", "_", str(k)) or "prop"
            # Nested objects are kept as JSON strings so nothing is silently lost.
            row[key] = json.dumps(v) if isinstance(v, (dict, list)) else v
        gs = geometry_summary(f.get("geometry"))
        # Source properties win. A derived column that would overwrite one is
        # stored under a geom_ prefix instead, and the collision is reported.
        for k, v in gs.items():
            if k in row and k != "feature_id":
                collisions.add(k)
                row[f"geom_{k}"] = v
            else:
                row[k] = v
        gt = gs["geom_type"] or "null"
        geom_types[gt] = geom_types.get(gt, 0) + 1
        rows.append(row)

    df = pd.DataFrame(rows)
    # fetch_overture.slim_feature writes is_named only when it is False, to keep
    # map files small. Restore the default, or "is_named = TRUE" matches nothing.
    if "is_named" in df.columns:
        df["is_named"] = df["is_named"].astype("boolean").fillna(True)
    if "geom_type" not in df.columns:
        for c in GEO_COLUMNS:
            df[c] = None
    if df.empty:
        notes.append("GeoJSON contained no features.")
    for k in sorted(collisions):
        notes.append(f"Property '{k}' kept as provided; the value computed from the geometry "
                     f"is in 'geom_{k}'. They can differ, for example when geometry was simplified.")
    return df, geom_types, notes


def read_csv_bytes(raw: bytes) -> pd.DataFrame:
    text = raw.decode("utf-8-sig", errors="replace")
    # Sniff the delimiter on the first line; pandas' sniffer is fragile on wide files.
    first = text.split("\n", 1)[0]
    delim = max([",", ";", "\t", "|"], key=first.count)
    df = pd.read_csv(StringIO(text), sep=delim, na_values=["NA", "N/A", "null", "NULL", ""], low_memory=False)
    df.columns = [re.sub(r"[^0-9a-zA-Z_]+", "_", str(c)).strip("_") or f"col_{i}" for i, c in enumerate(df.columns)]
    for c in df.columns:
        if pd.api.types.is_string_dtype(df[c]) and re.search(r"date|time|timestamp", c, re.IGNORECASE):
            parsed = pd.to_datetime(df[c], errors="coerce")
            if parsed.notna().mean() > 0.8:
                df[c] = parsed
    return df


def load_file(filename: str, raw: bytes, existing_names: Iterable[str] = ()) -> LoadedTable:
    if filename.lower().endswith(".gz"):
        import gzip
        raw = gzip.decompress(raw)
        filename = filename[:-3]
    lower = filename.lower()
    name = sanitize_table_name(filename, existing_names)
    if lower.endswith(".csv"):
        df = read_csv_bytes(raw)
        return LoadedTable(name=name, source_file=filename, kind="csv", df=df, feature_count=len(df))
    if lower.endswith((".geojson", ".json")):
        data = json.loads(raw.decode("utf-8-sig"))
        df, geom_types, notes = geojson_to_dataframe(data)
        return LoadedTable(
            name=name, source_file=filename, kind="geojson", df=df,
            feature_count=len(df), geom_types=geom_types, notes=notes,
        )
    raise ValueError(f"Unsupported file type: {filename}")


class DataStore:
    """In-memory DuckDB holding every loaded table. Read-only for the model."""

    def __init__(self) -> None:
        self.con = duckdb.connect(database=":memory:")
        self.tables: dict[str, LoadedTable] = {}
        self.spatial = False
        try:
            self.con.execute("INSTALL spatial; LOAD spatial;")
            self.spatial = True
        except Exception:
            self.spatial = False
        # The model must never reach the filesystem or the network through SQL.
        try:
            self.con.execute("SET enable_external_access = false;")
        except Exception:
            pass

    def add(self, table: LoadedTable) -> None:
        self.con.register(f"_src_{table.name}", table.df)
        self.con.execute(f'CREATE OR REPLACE TABLE "{table.name}" AS SELECT * FROM "_src_{table.name}"')
        self.con.unregister(f"_src_{table.name}")
        self.tables[table.name] = table

    def remove(self, name: str) -> None:
        self.con.execute(f'DROP TABLE IF EXISTS "{name}"')
        self.tables.pop(name, None)

    def schema_description(self, sample_rows: int = 3) -> str:
        """Compact schema text for the system prompt: columns, types, samples."""
        parts: list[str] = []
        for t in self.tables.values():
            cols = self.con.execute(f'DESCRIBE "{t.name}"').fetchall()
            col_lines = ", ".join(f"{c[0]} {c[1]}" for c in cols)
            head = self.con.execute(f'SELECT * FROM "{t.name}" LIMIT {sample_rows}').fetchdf()
            if "geometry_geojson" in head.columns:
                head = head.drop(columns=["geometry_geojson"])
            sample = head.to_csv(index=False).strip()
            geo = ""
            if t.notes:
                geo += " Notes: " + " ".join(t.notes)
            if t.is_geo:
                geo = (f" GeoJSON source; geometry types {t.geom_types}. "
                       "Columns lon/lat are a representative point, bbox_* the bounding box, "
                       "geometry_geojson the raw geometry string, and length_km the geodesic "
                       "length of line geometries (NULL for points and polygons) - sum it to "
                       "get total kilometres.")
            parts.append(
                f'Table "{t.name}" ({t.feature_count} rows, from {t.source_file}).{geo}\n'
                f"Columns: {col_lines}\nSample rows (CSV):\n{sample}"
            )
        spatial = ("DuckDB spatial extension is loaded: ST_GeomFromGeoJSON(geometry_geojson), "
                   "ST_Area, ST_Within, ST_Point and ST_Distance_Sphere (metres) etc. are available."
                   if self.spatial else "DuckDB spatial extension is NOT available; use lon/lat and bbox columns.")
        return "\n\n".join(parts) + "\n\n" + spatial

    def run_sql(self, sql: str, max_rows: int = 200) -> pd.DataFrame:
        check_sql_is_read_only(sql)
        return self.con.execute(sql).fetchdf().head(max_rows)


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|install|load|"
    r"pragma|set|reset|call|truncate|vacuum|checkpoint|grant|revoke|begin|commit|rollback)\b",
    re.IGNORECASE,
)


def check_sql_is_read_only(sql: str) -> None:
    """Allow a single SELECT / WITH statement and nothing else."""
    stripped = re.sub(r"--[^\n]*", " ", sql)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.DOTALL).strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("Empty SQL.")
    if ";" in stripped:
        raise ValueError("Only one statement is allowed.")
    if not re.match(r"^(select|with|describe|show|summarize)\b", stripped, re.IGNORECASE):
        raise ValueError("Only SELECT, WITH, DESCRIBE, SHOW or SUMMARIZE queries are allowed.")
    if re.search(r"\b(read_csv|read_json|read_parquet|read_text|read_blob|glob)\w*\s*\(", stripped, re.IGNORECASE):
        raise ValueError("File-reading functions are not allowed.")
    # Keywords inside string literals are data, not commands: WHERE name = 'Call Center'.
    without_literals = re.sub(r"'(?:[^']|'')*'", "''", stripped)
    m = _FORBIDDEN.search(without_literals)
    if m:
        raise ValueError(f"Statement contains a forbidden keyword: {m.group(0)}")


def table_statistics(table: LoadedTable) -> dict[str, Any]:
    """Dependency-free statistics used by the Statistics tab."""
    df = table.df
    numeric = df.select_dtypes(include="number")
    stats: dict[str, Any] = {
        "rows": int(len(df)),
        "columns": int(df.shape[1]),
        "memory_mb": round(float(df.memory_usage(deep=True).sum()) / 1e6, 2),
        "column_overview": pd.DataFrame({
            "column": df.columns,
            "dtype": [str(t) for t in df.dtypes],
            "non_null": [int(df[c].notna().sum()) for c in df.columns],
            "null_pct": [round(float(df[c].isna().mean() * 100), 1) for c in df.columns],
            "unique": [int(df[c].nunique(dropna=True)) for c in df.columns],
        }),
        "numeric_describe": numeric.describe().T if not numeric.empty else pd.DataFrame(),
    }
    if "length_km" in df.columns and df["length_km"].notna().any():
        lengths = pd.to_numeric(df["length_km"], errors="coerce").dropna()
        stats["total_length_km"] = float(lengths.sum())
        stats["line_features"] = int(len(lengths))
        stats["mean_segment_m"] = float(lengths.mean() * 1000)
    if table.is_geo and "lon" in df.columns:
        pts = df.dropna(subset=["lon", "lat"])
        stats["geometry_types"] = table.geom_types
        if not pts.empty:
            stats["bbox"] = {
                "min_lon": float(df["bbox_min_lon"].min()),
                "min_lat": float(df["bbox_min_lat"].min()),
                "max_lon": float(df["bbox_max_lon"].max()),
                "max_lat": float(df["bbox_max_lat"].max()),
            }
            stats["points"] = pts[["lon", "lat"]]
    else:
        lon_col = next((c for c in df.columns if c.lower() in ("lon", "lng", "longitude", "x")), None)
        lat_col = next((c for c in df.columns if c.lower() in ("lat", "latitude", "y")), None)
        if lon_col and lat_col:
            pts = df[[lon_col, lat_col]].apply(pd.to_numeric, errors="coerce").dropna()
            pts.columns = ["lon", "lat"]
            if not pts.empty:
                stats["points"] = pts
    return stats


def dataframe_to_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()
