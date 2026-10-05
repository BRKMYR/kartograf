# Architecture

## Data flow

```
Overture Maps release on S3 (public, no credentials)
        │  fetch_overture.py: DuckDB httpfs, row-group pruning by bbox, one pass per release
        ▼
data/overture/<scope>/  roads_*.geojson.gz · places_*.geojson.gz · stats_*.csv   (gitignored)
        │  data_loader.load_file: GeoJSON to rows with lon/lat, bbox, length_km, raw geometry
        ▼
DataStore: in-memory DuckDB, spatial extension, external access disabled
        ▲                                   │
        │ run_sql (read-only guard)         │ query results
        │                                   ▼
LLM tool loop (llm.py) ───────────► answer + every SQL statement shown to the user
  local: Ollama, llama3.1:8b default        │
  remote: Claude via Anthropic API          ▼
                              kepler_view.py: kepler.gl 3.2.6 page, data parsed in the browser
```

## Trust model

The model gets the schema, three sample rows per table and one tool, `run_sql`.

- `check_sql_is_read_only` allows a single SELECT, WITH, DESCRIBE, SHOW or SUMMARIZE
  and rejects file and URL readers. DuckDB runs with `enable_external_access = false`.
- Every statement the model ran is shown next to the answer. An answer is only as
  good as its SQL, and the SQL is always visible.
- With Ollama nothing leaves the machine. The kepler page loads JavaScript from unpkg
  and basemap tiles from CARTO, but the table data is embedded in the page and never
  uploaded.

One tool on purpose: more tools (geocode, nearest, buffer) would make a small model's
job easier, but they would hide the logic that the visible SQL makes auditable.
Spatial know-how lives in the system prompt as recipes (`ST_Distance_Sphere` in
metres) instead.

## Why these choices

| Choice | Reason |
|---|---|
| Overture via DuckDB on S3 | No SDK, no account; a Frankfurt extract is about 40 MB of transfer |
| Short table names (`places`, `roads`) | Small models mistype release-stamped names |
| kepler.gl UMD, not the Python `keplergl` package | The package pulls in Jupyter and gives no control over the UI theme |
| Latest stable kepler.gl (3.2.6), not the 3.3 alpha | The upstream example pins an alpha; a demo should not depend on one |
| `reference_sql` instead of hard-coded answers | The eval set survives a new Overture release |

## Web app

`server.py` serves `web/index.html` and three endpoints on 127.0.0.1: `/api/meta`,
`/api/layers` (roads and places as CSV for kepler) and `/api/ask`. One lock guards
the single DuckDB connection. The model answers with the same `run_sql` tool; the
server then derives a chart query (same filter, grouped by class or category) and a
map query (the matching places) from the model's last simple aggregate, runs them
through the same read-only guard, and returns them in the SQL list labelled
`kartograf`. Asking an 8B model for that second query made its first one worse.

## Swapping the model

Every model call goes through `llm.chat_ollama(store, history, question, model=..., host=...)`.

- Another Ollama model: `--model <name>` in `run_evals.py`, or the model field in the app sidebar.
- A remote Ollama server (for example a rented GPU): `--host http://<server>:11434`.
- Models served through an OpenAI-compatible endpoint (vLLM, for example Aleph Alpha's
  Kolibri-1, 78B, which does not fit in 16 GB of RAM) need a third loop next to
  `chat_ollama` and `chat_anthropic`, using the same `RUN_SQL_TOOL` schema and
  `execute_run_sql`. The eval runner and the app would only need a provider switch.
