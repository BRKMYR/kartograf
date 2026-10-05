# Geo Data Chat

Ask questions about CSV and GeoJSON files in plain language, and track how a map
dataset changes between releases. A Streamlit app with four working surfaces: a
chat that writes auditable SQL, a release-coverage view over real Overture Maps
data for Germany, per-table statistics with maps, and a query log that is the
seed of an eval set.

The model never sees the raw data. It sees the schema plus three sample rows per
table and has one tool: `run_sql`. Every query passes a read-only guard, runs in
an in-memory DuckDB with external access disabled, and is shown to the user next
to the answer. That is the whole trust model: the answer is only as good as the
SQL, and the SQL is always visible.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Option A: Claude through the Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...

# Option B: fully local, nothing leaves the machine
brew install ollama && ollama pull qwen3:8b

streamlit run app.py
```

Click "Load sample data" in the sidebar (synthetic Berlin charging sessions plus
district polygons), then ask:

- Which district has the highest average energy per session?
- How many sessions happened inside pilot-zone districts? Use the bounding boxes.
- Total revenue per operator, sorted.

## Real map data: Overture Maps

`fetch_overture.py` pulls road statistics and a motorway map layer straight from the
public Overture S3 bucket with DuckDB. No credentials, no SDK, no local database.

Check the cost first, then fetch. The estimate reads only Parquet footers:

```bash
python fetch_overture.py --preset berlin --estimate
python fetch_overture.py --preset berlin
python fetch_overture.py --preset germany      # the full country
```

Presets: `germany`, `bayern`, `nrw`, `berlin-brandenburg`, `berlin`, `saarland`, or any
`--bbox XMIN YMIN XMAX YMAX`. Each lands in its own folder, `data/overture/<scope>/`:

| File | Contents |
|---|---|
| `stats_<release>.csv` | segments, kilometres, mean segment length and name coverage by state and road class |
| `roads_<release>.geojson.gz` | motorway and trunk geometry, simplified to ~50 m, 5 decimals, gzip |
| `scope_<release>.json` | area, classes and method; releases are only compared when these match |

Shared caches in `data/overture/`: `states_de.parquet` (German state boundaries, fetched
once) and `.meta/` (Parquet footer summaries that make later estimates instant).

### Transfer and storage

Geometry is about 93% of every download and is required to compute kilometres, so the
two levers are reading it once and choosing a smaller area. Transfer per release, as
estimated from Parquet footers:

| Scope | Single pass | Earlier two-pass version |
|---|---|---|
| Germany | ~2.7 GB | ~5.3 GB |
| Bayern area | ~0.85 GB | ~1.7 GB |
| Berlin-Brandenburg area | ~0.22 GB | ~0.45 GB |
| Berlin area | ~0.06 GB | ~0.11 GB |

The first fetch also reads German state boundaries once. On disk, Germany for two releases
is 6.3 MB and the Berlin area 0.1 MB. `python fetch_overture.py --compact` converts map
files from older runs in place and verifies every ID and length before deleting the original.

The Python environment is the real footprint, about 470 MB, mostly pyarrow, pandas and
DuckDB, which Streamlit and the pipeline need.

Presets other than Germany are rectangles, so they also clip neighbouring states. The app
labels them as areas and lists kilometres per state.

Scope decisions, all recorded per release in `scope_<release>.json`:

- **Default scope is the classified network**: motorway, trunk, primary,
  secondary, tertiary. Adding residential, unclassified and living_street
  (`--classes`) adds rows but not transfer, since the class filter cannot skip
  Parquet row groups. Footways, service roads, steps, tracks and cycleways are
  never counted; they are about 86% of Overture segments and would make a
  "kilometres of road" figure meaningless.
- **Releases are only compared when their scope matches.** Same bounding box,
  same classes. The app refuses the comparison otherwise and says why.
- **Overture kilometres are not official route kilometres.** Each carriageway
  direction is its own segment and ramps carry the motorway class. For the
  2026-07-22 release that puts motorway at about 3.2 times the official
  Autobahn length and primary to tertiary at 1.4 to 1.5 times the
  Bundes-, Landes- and Kreisstraßen figures. The state ranking matches official
  statistics.
- **Kilometres are geodesic**, from `ST_Length_Spheroid` on the full geometry,
  and a segment is attributed to the state containing its centroid, snapped to a
  0.01 degree grid.

## Data sources and attribution

Road and boundary data © OpenStreetMap contributors, Overture Maps Foundation.
Available under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).
Recommended citation: Overture Maps Foundation, overturemaps.org.

- **Transportation theme** (road segments): ODbL. Includes contributions from TomTom
  and OpenStreetMap.
- **Divisions theme** (German state boundaries): ODbL. The theme also draws on
  geoBoundaries, Esri Community Maps and LINZ (CC BY 4.0).
- **Basemap** in the difference map: © CARTO, © OpenStreetMap contributors.

This is an independent project. It is not affiliated with, endorsed by or sponsored by
the Overture Maps Foundation, the OpenStreetMap Foundation or TomTom.

Downloaded extracts and the statistics derived from them stay in `data/overture/`, which
is excluded from git, and every `scope_<release>.json` records source and licence. If you
publish derived statistics or maps, keep the attribution above and check the ODbL
share-alike terms for derived databases.

The Berlin charging sessions and districts in `sample_data/` are synthetic. Operator names
are illustrative and the sessions do not describe any real operator.

## The Releases tab

Answers two questions: how much road is in this dataset, and what changed.

- **Headline figures** for the newest release, with the delta against the previous one.
- **Weekly time series** of network length. Overture ships monthly, so only the
  release weeks are measured; the weeks between are simulated from the real
  anchors and are labelled as such everywhere they appear. On a release date the
  simulated value *is* the measured value.
- **A synthetic data-quality incident** is injected into two of the simulated
  weeks, and the week-over-week monitor catches it. That is the point of the
  view: a coverage chart that cannot catch a regression is decoration.
- **Change by road class** as small multiples, indexed to the first week on one
  shared percent axis. Classes differ by an order of magnitude in length, so raw
  kilometres on a shared axis flatten the small ones, and independent axes stretch
  a change of a few kilometres to full panel height. The index keeps real small
  changes small and makes a regression stand out.
- **Simulated weeks carry no random noise.** An earlier version added a little,
  and on zoomed axes it read as real week-to-week volatility. Between releases the
  line is plain interpolation; the injected incident is the only deviation.
- **Release comparison** with diverging bars, blue for kilometres gained and red
  for kilometres lost, plus the full table.
- **A difference map** of motorway and trunk: grey for segments in both releases,
  blue for segments new in B, red for segments gone from B, matched by the stable
  Overture GERS id.

Everything on the tab is downloadable as CSV.

## Layout

```
app.py           Streamlit UI: sidebar, chat, releases, statistics, data, query log
data_loader.py   CSV / GeoJSON parsing, geodesic length, DuckDB store, read-only SQL guard
llm.py           Tool loop for Claude (Anthropic SDK) and Ollama; system prompt
snapshots.py     Release stats, version comparison, weekly series, anomaly detection
charts.py        Altair specs and the validated colour palette
fetch_overture.py Overture download over S3 via DuckDB, estimate, compaction
scopes.py        Region presets, dataset folders, gzip helpers
test_core.py     Loader, SQL guard, statistics, tool loop against a fake model
test_releases.py Release stats, weekly series, incident injection, chart specs
test_storage.py  Presets, transfer estimate, compaction with verification, gzip loading
sample_data/     Synthetic CSV + GeoJSON so the app runs without any real data
data/overture/   One folder per region plus shared caches (gitignored, reproducible)
query_log.jsonl  Appended per turn: question, SQL, answer, latency, tokens (gitignored)
```

GeoJSON is parsed in pure Python: each feature becomes a row with its properties
flattened, plus `geom_type`, a representative `lon`/`lat`, a bounding box, the
raw geometry string, and `length_km`, the geodesic length of line geometries.
Polygons return null length on purpose, because a perimeter is not a length.
Where the DuckDB spatial extension is available, `ST_*` functions also work on
`ST_GeomFromGeoJSON(geometry_geojson)`.

## Tests

```bash
.venv/bin/python -m pytest -q
```

57 tests, no network and no API key needed. They cover the SQL guard against
injection and write attempts, the geodesic length against known distances, the
tool loop recovering from a bad query, the weekly series matching its real
anchors exactly, the refusal to compare releases of different scope, and every chart spec building in light and dark mode.

## From demo to eval

`query_log.jsonl` records every turn. The path to a real evaluation:

1. Ask 20 questions with known answers. Label each logged row correct or not.
2. Bucket every failure: wrong table, wrong join, wrong aggregation, hallucinated
   column, refused when it should have answered, answered when it should have refused.
3. Change one thing (prompt, sample rows, model, effort) and re-run the 20.
4. Report the delta with latency and token cost in the same table.

That table, not the chatbot, is the deliverable.
