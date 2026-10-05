"""kepler.gl map as a self-contained HTML page, for Streamlit or a standalone file.

The page loads React 18, Redux and the kepler.gl UMD bundle from unpkg, as in
kepler.gl's own examples/umd-client at the pinned version. No Python keplergl
package: it pulls in Jupyter and gives no control over the UI theme.

Data never leaves the page: tables are embedded as CSV and parsed in the
browser with KeplerGl.processCsvData. The basemap is a CARTO style that needs
no Mapbox token.
"""

from __future__ import annotations

import json

import pandas as pd

KEPLER_VERSION = "3.2.6"
MAX_ROWS = 200_000

# CARTO styles shipped with kepler.gl 3.x; no token required.
BASEMAP = {"light": "positron", "dark": "dark-matter"}

# Columns kepler can use or that help when hovering; everything else is dropped
# to keep the embedded page small.
_DROP_ALWAYS = {"bbox_min_lon", "bbox_min_lat", "bbox_max_lon", "bbox_max_lat", "geom_type"}

_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8" />
<link href="https://unpkg.com/maplibre-gl@^3/dist/maplibre-gl.css" rel="stylesheet" />
<script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js" crossorigin></script>
<script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js" crossorigin></script>
<script src="https://unpkg.com/redux@4.2.1/dist/redux.js" crossorigin></script>
<script src="https://unpkg.com/react-redux@8.1.2/dist/react-redux.min.js" crossorigin></script>
<script src="https://unpkg.com/styled-components@6.1.8/dist/styled-components.min.js" crossorigin></script>
<script src="https://unpkg.com/kepler.gl@__VERSION__/umd/keplergl.min.js"></script>
<style>body { margin: 0; padding: 0; overflow: hidden; background: __BG__; }</style>
</head>
<body>
<div id="app"></div>
<script>
  const DATASETS = __DATASETS__;
  const THEME = "__THEME__";
  const BASEMAP = "__BASEMAP__";

  const reducers = Redux.combineReducers({keplerGl: KeplerGl.keplerGlReducer});
  const store = Redux.createStore(reducers, {},
      Redux.applyMiddleware(...KeplerGl.enhanceReduxMiddleware([])));

  function App() {
    const [size, setSize] = React.useState({width: window.innerWidth, height: window.innerHeight});
    // Runs after KeplerGl has mounted and registered "map" in the store; a
    // dispatch before that (React 18 renders asynchronously) is dropped.
    React.useEffect(function () {
      store.dispatch(KeplerGl.addDataToMap({
        datasets: DATASETS.map(function (d) {
          return {info: {id: d.id, label: d.label}, data: KeplerGl.processCsvData(d.csv)};
        }),
        options: {centerMap: true, readOnly: false},
        config: {version: "v1", config: {mapStyle: {styleType: BASEMAP}}}
      }));
    }, []);
    React.useEffect(function () {
      const onResize = function () { setSize({width: window.innerWidth, height: window.innerHeight}); };
      window.addEventListener("resize", onResize);
      return function () { window.removeEventListener("resize", onResize); };
    }, []);
    return React.createElement(KeplerGl.KeplerGl, {
      id: "map", theme: THEME, mapboxApiAccessToken: "",
      width: size.width, height: size.height
    });
  }

  ReactDOM.createRoot(document.getElementById("app")).render(
    React.createElement(ReactRedux.Provider, {store: store}, React.createElement(App)));
</script>
</body>
</html>
"""


def is_mappable(df: pd.DataFrame) -> bool:
    """True when kepler can draw the table: point columns or a GeoJSON geometry column."""
    return {"lon", "lat"} <= set(df.columns) or "geometry_geojson" in df.columns


def prepare(df: pd.DataFrame, max_rows: int = MAX_ROWS) -> tuple[pd.DataFrame, bool]:
    """Slim a table for the map. Returns (frame, truncated).

    Point tables keep lon/lat and drop the GeoJSON string, which kepler would
    otherwise draw a second time. Line and polygon tables keep the GeoJSON and
    drop lon/lat, so kepler builds one geometry layer, not an extra point layer.
    """
    only_points = "geom_type" in df.columns and (df["geom_type"].dropna() == "Point").all()
    out = df.drop(columns=[c for c in _DROP_ALWAYS if c in df.columns])
    if "geometry_geojson" in out.columns:
        if only_points and {"lon", "lat"} <= set(out.columns):
            out = out.drop(columns=["geometry_geojson"])
        else:
            out = out.drop(columns=[c for c in ("lon", "lat") if c in out.columns])
            out = out.rename(columns={"geometry_geojson": "_geojson"})
    truncated = len(out) > max_rows
    return out.head(max_rows), truncated


def build_kepler_html(datasets: dict[str, pd.DataFrame], theme: str = "light",
                      max_rows: int = MAX_ROWS) -> str:
    """One HTML page with every mappable table as a kepler dataset."""
    theme = "dark" if theme == "dark" else "light"
    payload = []
    for name, df in datasets.items():
        if not is_mappable(df):
            continue
        frame, _ = prepare(df, max_rows)
        payload.append({"id": name, "label": name, "csv": frame.to_csv(index=False)})
    # Data comes from uploaded files and query results, so it must not be able to
    # end the script block (</script>) or open an HTML comment (<!--): every "<"
    # is written as a JSON unicode escape, which parses back to the same string.
    data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return (_TEMPLATE
            .replace("__VERSION__", KEPLER_VERSION)
            .replace("__BG__", "#0e1117" if theme == "dark" else "#ffffff")
            .replace("__THEME__", theme)
            .replace("__BASEMAP__", BASEMAP[theme])
            .replace("__DATASETS__", data_js))
