"""Download Overture Maps road statistics and a map layer, one release at a time.

Per release, into data/overture/<scope>/:
  stats_<release>.csv           km, segments, mean length, name coverage by state and class
  roads_<release>.geojson.gz    motorway + trunk geometry for the map, gzip-compressed
  scope_<release>.json          exactly what was fetched; the app only compares equal scopes
  places_<release>.geojson.gz   points of interest with name and category (only with --places)

How the transfer is kept small:
  * One pass per release. Geometry is ~93% of the bytes and is read exactly once;
    stats and map layer are both derived from that single local table.
  * Row-group pruning. Only Parquet row groups whose bbox statistics overlap the
    area are fetched, and only the columns the pipeline uses.
  * State boundaries are cached in data/overture/states_de.parquet after the
    first run instead of being re-read from the divisions theme every release.
  * --estimate prints the expected transfer from Parquet footers (a few MB of
    metadata) and exits, so you know the cost before paying it.

Usage:
  python fetch_overture.py --preset berlin --estimate
  python fetch_overture.py --preset berlin
  python fetch_overture.py --preset germany --releases 2026-07-22.0 2026-08-19.0
  python fetch_overture.py --bbox 6.35 49.11 7.40 49.64
  python fetch_overture.py --preset frankfurt --releases 2026-09-23.1 --places --map-classes motorway trunk primary secondary tertiary
  python fetch_overture.py --compact     # rewrite existing uncompressed map files in place

Data: © OpenStreetMap contributors, Overture Maps Foundation. Available under the
Open Database License (ODbL). This project is not affiliated with Overture,
OpenStreetMap or TomTom; see attribution.py.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

import attribution
from scopes import DATA_ROOT, PRESETS, scope_name

BUCKET = "s3://overturemaps-us-west-2/release"
STATES_CACHE = DATA_ROOT / "states_de.parquet"

CLASSIFIED = ("motorway", "trunk", "primary", "secondary", "tertiary")
FULL_DRIVABLE = CLASSIFIED + ("unclassified", "residential", "living_street")
MAP_CLASSES = ("motorway", "trunk")
SIMPLIFY_DEG = 0.0005    # ~50 m, map geometry only; lengths use full geometry
COORD_DECIMALS = 5       # ~1 m

# Leaf columns the single pass touches. Used both by the query and by --estimate.
SEGMENT_COLUMNS = ("subtype", "class", "bbox, xmin", "bbox, ymin", "names, primary", "id", "geometry")
DIVISION_COLUMNS = ("country", "subtype", "names, primary", "geometry")
PLACE_COLUMNS = ("bbox, xmin", "bbox, ymin", "id", "names, primary", "basic_category",
                 "taxonomy, primary", "confidence", "operating_status", "addresses, list, element, locality",
                 "geometry")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect(tmp_dir: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2'; SET enable_progress_bar=false;")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp_dir}'; SET preserve_insertion_order=false;")
    con.execute("SET memory_limit='6GB'; SET threads=4;")
    return con


def segment_glob(release: str) -> str:
    return f"{BUCKET}/{release}/theme=transportation/type=segment/*"


def division_glob(release: str) -> str:
    return f"{BUCKET}/{release}/theme=divisions/type=division_area/*"


def place_glob(release: str) -> str:
    return f"{BUCKET}/{release}/theme=places/type=place/*"


# --------------------------------------------------------------------- estimate

def overlapping_bytes(meta: pd.DataFrame, bbox, columns) -> tuple[int, int, int]:
    """Bytes the pipeline will fetch, from Parquet footer statistics.

    `meta` has one row per (file, row group, leaf column) with compressed size
    and min/max. A row group is fetched if its bbox.xmin and bbox.ymin ranges
    overlap the area, which is exactly the pruning DuckDB applies.
    Returns (row groups fetched, row groups total, bytes).
    """
    x0, y0, x1, y1 = bbox
    key = ["file_name", "row_group_id"]
    lo_hi = (meta[meta["path_in_schema"].isin(["bbox, xmin", "bbox, ymin"])]
             .pivot_table(index=key, columns="path_in_schema", values=["mn", "mx"], aggfunc="first"))
    keep = lo_hi[(lo_hi[("mx", "bbox, xmin")] >= x0) & (lo_hi[("mn", "bbox, xmin")] <= x1) &
                 (lo_hi[("mx", "bbox, ymin")] >= y0) & (lo_hi[("mn", "bbox, ymin")] <= y1)].index
    sel = meta.set_index(key)
    sel = sel[sel.index.isin(keep) & sel["path_in_schema"].isin(columns)]
    total_groups = meta[key].drop_duplicates().shape[0]
    return len(keep), total_groups, int(sel["total_compressed_size"].sum())


def read_metadata(con, release: str, kind: str = "segments") -> pd.DataFrame:
    """Footer statistics for the columns the pipeline reads, cached locally.

    Only the needed leaf columns are pulled into pandas; converting every
    column of every row group is what makes an unfiltered read take minutes.
    The cache is a few MB and makes later estimates for any region instant.
    """
    glob, columns = {"segments": (segment_glob(release), SEGMENT_COLUMNS),
                     "places": (place_glob(release), PLACE_COLUMNS)}[kind]
    cache = DATA_ROOT / ".meta" / f"{kind}_{release}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    cols = ", ".join(f"'{c}'" for c in columns)
    meta = con.execute(f"""
        SELECT file_name, row_group_id, path_in_schema, total_compressed_size,
               TRY_CAST(stats_min AS DOUBLE) AS mn, TRY_CAST(stats_max AS DOUBLE) AS mx
        FROM parquet_metadata('{glob}')
        WHERE path_in_schema IN ({cols})
    """).fetchdf()
    cache.parent.mkdir(parents=True, exist_ok=True)
    meta.to_parquet(cache, index=False)
    return meta


def estimate(con, release: str, bbox) -> int:
    t0 = time.time()
    meta = read_metadata(con, release)
    groups, total, nbytes = overlapping_bytes(meta, bbox, SEGMENT_COLUMNS)
    extra = ""
    if not STATES_CACHE.exists():
        # Only row groups whose country statistics can contain 'DE' are read.
        dmeta = con.execute(f"""
            WITH m AS (SELECT * FROM parquet_metadata('{division_glob(release)}')),
            rg AS (SELECT file_name, row_group_id FROM m
                   WHERE path_in_schema = 'country' AND stats_min <= 'DE' AND stats_max >= 'DE')
            SELECT sum(total_compressed_size) FROM m SEMI JOIN rg USING (file_name, row_group_id)
            WHERE path_in_schema IN {tuple(DIVISION_COLUMNS)}
        """).fetchone()[0] or 0
        extra = f" + ~{dmeta / 1e9:.2f} GB once for state boundaries (then cached)"
    log(f"  estimate {release}: {groups:,} of {total:,} row groups, "
        f"~{nbytes / 1e9:.2f} GB{extra}  (footers read in {time.time() - t0:.0f}s)")
    return nbytes


def estimate_places(con, release: str, bbox) -> int:
    groups, total, nbytes = overlapping_bytes(read_metadata(con, release, "places"), bbox, PLACE_COLUMNS)
    log(f"  estimate places {release}: {groups:,} of {total:,} row groups, ~{nbytes / 1e9:.2f} GB")
    return nbytes


# --------------------------------------------------------------------- fetch

def load_states(con, release: str) -> None:
    """German states, from the local cache when present."""
    if STATES_CACHE.exists():
        con.execute(f"CREATE OR REPLACE TABLE states AS SELECT state, ST_GeomFromWKB(wkb) AS geom, "
                    f"xmin, xmax, ymin, ymax FROM '{STATES_CACHE}'")
        src = "cache"
    else:
        con.execute(f"""
            CREATE OR REPLACE TABLE states AS
            SELECT names.primary AS state, geometry AS geom,
                   ST_XMin(geometry) AS xmin, ST_XMax(geometry) AS xmax,
                   ST_YMin(geometry) AS ymin, ST_YMax(geometry) AS ymax
            FROM read_parquet('{division_glob(release)}', hive_partitioning=1)
            WHERE country = 'DE' AND subtype = 'region'
        """)
        STATES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY (SELECT state, ST_AsWKB(geom) AS wkb, xmin, xmax, ymin, ymax FROM states) "
                    f"TO '{STATES_CACHE}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 19)")
        src = f"divisions theme, cached to {STATES_CACHE.name}"
    n = con.execute("SELECT count(DISTINCT state) FROM states").fetchone()[0]
    log(f"  states: {n} ({src})")


def single_pass(con, release: str, bbox, classes, map_classes=MAP_CLASSES) -> None:
    """Read the release once. Everything later is local."""
    x0, y0, x1, y1 = bbox
    cls = ", ".join(f"'{c}'" for c in classes)
    mapc = ", ".join(f"'{c}'" for c in map_classes)
    t0 = time.time()
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE seg AS
        SELECT class,
               names.primary IS NOT NULL                  AS is_named,
               ST_Length_Spheroid(geometry) / 1000.0      AS length_km,
               round(ST_X(ST_Centroid(geometry)), 2)      AS gx,
               round(ST_Y(ST_Centroid(geometry)), 2)      AS gy,
               CASE WHEN class IN ({mapc}) THEN id END            AS map_id,
               CASE WHEN class IN ({mapc}) THEN names.primary END AS map_name,
               CASE WHEN class IN ({mapc})
                    THEN ST_AsGeoJSON(ST_Simplify(geometry, {SIMPLIFY_DEG})) END AS map_gj
        FROM read_parquet('{segment_glob(release)}', hive_partitioning=1)
        WHERE subtype = 'road'
          AND class IN ({cls})
          AND bbox.xmin BETWEEN {x0} AND {x1}
          AND bbox.ymin BETWEEN {y0} AND {y1}
    """)
    # Grid-cell state lookup: a few hundred thousand point-in-polygon tests, not millions.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE cell_state AS
        SELECT c.gx, c.gy, s.state
        FROM (SELECT DISTINCT gx, gy FROM seg) c
        LEFT JOIN states s
          ON c.gx BETWEEN s.xmin AND s.xmax
         AND c.gy BETWEEN s.ymin AND s.ymax
         AND ST_Within(ST_Point(c.gx, c.gy), s.geom)
        QUALIFY row_number() OVER (PARTITION BY c.gx, c.gy ORDER BY s.state) = 1
    """)
    n = con.execute("SELECT count(*) FROM seg").fetchone()[0]
    log(f"  read {n:,} segments in {(time.time() - t0) / 60:.1f} min")


def write_stats(con, release: str, out: Path) -> None:
    path = out / f"stats_{release}.csv"
    con.execute(f"""
        COPY (
            SELECT '{release}' AS release,
                   COALESCE(cs.state, 'outside DE')                 AS state,
                   s.class,
                   count(*)                                         AS segments,
                   round(sum(s.length_km), 2)                       AS length_km,
                   round(avg(s.length_km) * 1000, 1)                AS mean_segment_m,
                   round(100.0 * avg(CAST(s.is_named AS INT)), 1)   AS pct_named
            FROM seg s JOIN cell_state cs USING (gx, gy)
            GROUP BY 1, 2, 3
            ORDER BY state, length_km DESC
        ) TO '{path}' (HEADER, DELIMITER ',')
    """)
    km = con.execute(f"SELECT sum(length_km) FROM read_csv('{path}') WHERE state <> 'outside DE'").fetchone()[0] or 0
    log(f"  stats: {path.name}, {km:,.0f} km")


def round_coords(coords, n: int = COORD_DECIMALS):
    if coords and isinstance(coords[0], (int, float)):
        return [round(coords[0], n), round(coords[1], n)]
    return [round_coords(c, n) for c in coords]


def slim_feature(fid: str, cls: str, name: str | None, state: str, length_km: float,
                 is_named: bool, geometry: dict) -> dict:
    """Only what the app uses. Empty names and default flags are omitted."""
    props = {"class": cls, "state": state, "length_km": round(float(length_km), 4)}
    if name:
        props["name"] = name
    if not is_named:
        props["is_named"] = False
    return {"type": "Feature", "id": fid, "properties": props,
            "geometry": {"type": geometry["type"], "coordinates": round_coords(geometry["coordinates"])}}


def write_features_gz(path: Path, features) -> int:
    n = 0
    tmp = path.with_suffix(path.suffix + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for feat in features:
            f.write(("," if n else "") + json.dumps(feat, separators=(",", ":")) + "\n")
            n += 1
        f.write("]}\n")
    tmp.replace(path)
    return n


def write_map(con, release: str, out: Path) -> None:
    path = out / f"roads_{release}.geojson.gz"
    cur = con.execute("""
        SELECT s.map_id, s.class, s.map_name, cs.state, s.length_km, s.map_name IS NOT NULL, s.map_gj
        FROM seg s JOIN cell_state cs USING (gx, gy)
        WHERE s.map_id IS NOT NULL AND cs.state IS NOT NULL
    """)

    def rows():
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                return
            for fid, cls, name, state, km, named, gj in batch:
                yield slim_feature(fid, cls, name, state, km, named, json.loads(gj))

    n = write_features_gz(path, rows())
    log(f"  map: {path.name}, {n:,} features, {path.stat().st_size / 1e6:.1f} MB")


def place_feature(fid: str, name: str | None, category: str | None, basic: str | None,
                  confidence: float | None, status: str | None, locality: str | None,
                  lon: float, lat: float) -> dict:
    """One place as a slim GeoJSON point. Empty fields are omitted."""
    props = {"category": category, "basic_category": basic, "name": name, "locality": locality,
             "operating_status": status,
             "confidence": round(float(confidence), 3) if confidence is not None else None}
    return {"type": "Feature", "id": fid, "properties": {k: v for k, v in props.items() if v is not None},
            "geometry": {"type": "Point", "coordinates": round_coords([lon, lat])}}


def write_places(con, release: str, bbox, out: Path) -> None:
    """Points of interest inside the bbox, read in one pass with the same row-group pruning."""
    x0, y0, x1, y1 = bbox
    path = out / f"places_{release}.geojson.gz"
    t0 = time.time()
    cur = con.execute(f"""
        SELECT id, names.primary, taxonomy.primary, basic_category, confidence, operating_status,
               addresses[1].locality, ST_X(geometry), ST_Y(geometry)
        FROM read_parquet('{place_glob(release)}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {x0} AND {x1}
          AND bbox.ymin BETWEEN {y0} AND {y1}
    """)

    def rows():
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                return
            for row in batch:
                yield place_feature(*row)

    n = write_features_gz(path, rows())
    log(f"  places: {path.name}, {n:,} features, {path.stat().st_size / 1e6:.1f} MB "
        f"in {(time.time() - t0) / 60:.1f} min")


def write_scope(release: str, bbox, classes, out: Path) -> None:
    (out / f"scope_{release}.json").write_text(json.dumps({
        "release": release,
        "bbox": [round(v, 4) for v in bbox],
        "classes": sorted(classes),
        "state_attribution": "centroid snapped to 0.01 degree grid",
        "length": "ST_Length_Spheroid on full geometry",
        "map_geometry": f"simplified {SIMPLIFY_DEG} deg, {COORD_DECIMALS} decimals",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **attribution.scope_fields(),
    }, indent=2))


# --------------------------------------------------------------------- compact

def compact_file(src: Path) -> tuple[float, float]:
    """Rewrite one legacy roads_*.geojson as slim gzip, verify, then delete the original."""
    fc = json.loads(src.read_text(encoding="utf-8"))
    feats = fc["features"]
    dst = src.with_name(src.name + ".gz")
    write_features_gz(dst, (
        slim_feature(f["id"], f["properties"]["class"], f["properties"].get("name") or None,
                     f["properties"]["state"], f["properties"]["length_km"],
                     f["properties"].get("is_named", True), f["geometry"])
        for f in feats))
    back = json.loads(gzip.decompress(dst.read_bytes()))["features"]
    same_ids = [f["id"] for f in back] == [f["id"] for f in feats]
    km_a = sum(f["properties"]["length_km"] for f in feats)
    km_b = sum(f["properties"]["length_km"] for f in back)
    # Lengths are rounded to 4 decimals, so the sum may drift by at most 0.00005 km per feature.
    if not same_ids or abs(km_a - km_b) > 0.00005 * len(feats) + 1e-6:
        dst.unlink()
        raise RuntimeError(f"verification failed for {src.name}; original kept")
    before, after = src.stat().st_size / 1e6, dst.stat().st_size / 1e6
    src.unlink()
    return before, after


def compact_existing(root: Path = DATA_ROOT) -> None:
    """Move legacy top-level files into their scope folder and compress map files."""
    for scope_file in sorted(root.glob("scope_*.json")):
        meta = json.loads(scope_file.read_text())
        release = meta["release"]
        target = root / scope_name(None, tuple(meta["bbox"]))
        target.mkdir(parents=True, exist_ok=True)
        for name in (f"stats_{release}.csv", f"roads_{release}.geojson", scope_file.name):
            if (root / name).exists():
                shutil.move(str(root / name), str(target / name))
                log(f"moved {name} -> {target.name}/")
    for src in sorted(root.glob("*/roads_*.geojson")):
        before, after = compact_file(src)
        log(f"compacted {src.parent.name}/{src.name}: {before:.1f} MB -> {after:.1f} MB")
    shutil.rmtree(root / ".duckdb_tmp", ignore_errors=True)


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--releases", nargs="+", default=["2026-07-22.0", "2026-08-19.0"])
    ap.add_argument("--preset", choices=sorted(PRESETS), default="germany")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    help="custom area; overrides --preset")
    ap.add_argument("--classes", nargs="+", default=list(CLASSIFIED))
    ap.add_argument("--map-classes", nargs="+", default=list(MAP_CLASSES),
                    help="road classes written as map geometry (default: motorway trunk)")
    ap.add_argument("--places", action="store_true", help="also fetch points of interest (places theme)")
    ap.add_argument("--estimate", action="store_true", help="print expected transfer and exit")
    ap.add_argument("--compact", action="store_true", help="compress existing map files and exit")
    args = ap.parse_args()

    if args.compact:
        compact_existing()
        return 0

    bbox = tuple(args.bbox) if args.bbox else tuple(PRESETS[args.preset]["bbox"])
    name = scope_name(None if args.bbox else args.preset, bbox)
    out = DATA_ROOT / name
    classes = tuple(args.classes)
    log(f"scope '{name}'  bbox={bbox}  classes={','.join(classes)}")

    con = connect(DATA_ROOT / ".duckdb_tmp")
    try:
        if args.estimate:
            total = sum(estimate(con, r, bbox) for r in args.releases)
            if args.places:
                total += sum(estimate_places(con, r, bbox) for r in args.releases)
            log(f"total for {len(args.releases)} release(s): ~{total / 1e9:.2f} GB")
            return 0
        out.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        for release in args.releases:
            log(f"release {release}")
            load_states(con, release)
            single_pass(con, release, bbox, classes, tuple(args.map_classes))
            write_stats(con, release, out)
            write_map(con, release, out)
            write_scope(release, bbox, classes, out)
            if args.places:
                write_places(con, release, bbox, out)
            con.execute("DROP TABLE IF EXISTS seg; DROP TABLE IF EXISTS cell_state;")
        log(f"done in {(time.time() - t0) / 60:.1f} min -> {out}")
        return 0
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {str(e)[:300]}")
        return 1
    finally:
        con.close()
        shutil.rmtree(DATA_ROOT / ".duckdb_tmp", ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
