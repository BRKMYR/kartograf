---
title: Kartograf
emoji: 🗺️
colorFrom: gray
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Ask a city map a question. Every answer shows its SQL.
---

# Kartograf

Ask a question about Frankfurt's roads and places in English or German. An open
language model writes spatial SQL, a read-only DuckDB database runs it, and the
answer comes back with a chart, the matching places on a kepler.gl map, and every
SQL statement behind it.

This demo runs the model at a hosted inference endpoint. The project itself runs
fully on a laptop with Ollama; code, eval set and results:
[github.com/BRKMYR/kartograf](https://github.com/BRKMYR/kartograf).

- One tool, `run_sql`. Every statement passes a read-only guard and runs with
  external access disabled and a 20-second timeout.
- Rate limited per visitor and per day. Answers can be wrong; check the SQL.
- Questions are not stored.

## Data

Road and boundary data © OpenStreetMap contributors, Overture Maps Foundation,
available under the Open Database License (ODbL). The Frankfurt extract in
`data/overture/frankfurt` is a derivative of Overture release 2026-09-23.1 and is
offered under the ODbL. Places data combines sources under CDLA Permissive 2.0,
Apache 2.0 (Foursquare Labs, Inc.) and CC0 1.0 (AllThePlaces); see
docs.overturemaps.org/attribution. Basemap © CARTO, © OpenStreetMap contributors.

Independent project, not affiliated with the Overture Maps Foundation, the
OpenStreetMap Foundation, Foursquare, Meta, Alibaba or CARTO.
