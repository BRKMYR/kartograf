# Third-party notices

Kartograf's own code is MIT licensed (see `LICENSE`). It downloads open data,
calls locally installed models and loads libraries that keep their own licences.
Nothing below is redistributed in this repository unless stated.

## Data

| Source | Used for | Licence and attribution |
|---|---|---|
| Overture Maps, transportation theme | Road segments (`fetch_overture.py`) | ODbL 1.0. Attribution: "© OpenStreetMap contributors, Overture Maps Foundation". Includes contributions from TomTom. Derived databases you publish fall under ODbL share-alike. |
| Overture Maps, divisions theme | German state boundaries, cached locally | ODbL 1.0, same attribution. Also draws on geoBoundaries, Esri Community Maps and LINZ (CC BY 4.0). |
| Overture Maps, places theme | Points of interest (`--places`) | No single licence. Sources under CDLA Permissive 2.0 (Meta, Microsoft and others), Apache 2.0 ("Copyright 2024 Foursquare Labs, Inc. All rights reserved.") and CC0 1.0 (AllThePlaces). See docs.overturemaps.org/attribution. |
| CARTO basemaps (Positron, Dark Matter) | Map background in the browser | "© CARTO, © OpenStreetMap contributors". Loaded at view time, not stored. Check CARTO's terms before commercial or high-traffic use. |
| `sample_data/` | Demo without downloads | Synthetic, created for this project, MIT. |

Overture extracts are written to `data/overture/`, which is gitignored. The eval
results in `evals/results/` contain counts and short answers derived from the
Frankfurt extract, with the attribution above.

## Models (not included)

| Model | How it is used | Licence |
|---|---|---|
| Qwen3 8B | Default model of the web app, through Ollama | Apache 2.0 |
| Llama 3.1 8B | Default model of the workbench, through Ollama | Llama 3.1 Community License, Copyright © Meta Platforms, Inc. No weights are distributed with Kartograf. If you distribute a product that includes Llama, the license asks for "Built with Llama" attribution. |
| Claude (optional) | Workbench provider through the Anthropic API | Anthropic terms of service; requires your own API key |

## Browser libraries (loaded from unpkg at view time)

| Library | Licence |
|---|---|
| kepler.gl 3.2.6 | MIT |
| React 18, ReactDOM 18 | MIT |
| Redux 4, React Redux 8 | MIT |
| styled-components 6 | MIT |
| MapLibre GL JS 3 (stylesheet) | BSD 3-Clause |
| Inter Tight, IBM Plex Mono (Google Fonts) | SIL Open Font License 1.1 |

## Python packages (`requirements.txt`)

| Package | Licence |
|---|---|
| DuckDB (with the spatial and httpfs extensions) | MIT. The spatial extension bundles GEOS (LGPL 2.1), PROJ and GDAL (MIT); DuckDB downloads it at runtime, Kartograf does not redistribute it |
| pandas | BSD 3-Clause |
| Streamlit | Apache 2.0 |
| Altair | BSD 3-Clause |
| pydeck | Apache 2.0 |
| ollama (Python client) | MIT |
| anthropic (Python SDK) | MIT |
| PyYAML | MIT |
| pytest (tests only) | MIT |

Licences are as published by each project at the time of writing. Check the
upstream project when you redistribute it.
