"""Kartograf web app: a kepler.gl map with an agent search bar, on localhost.

Run:  python server.py            then open http://localhost:8766

Same trust model as the Streamlit app: the local model gets one read-only
run_sql tool over DuckDB, and every statement it ran comes back with the answer.
On top, the agent is asked for a chart-ready breakdown of totals, and query
results with coordinates are drawn on the map.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pandas as pd

import kepler_view
from data_loader import DataStore
from llm import chat_ollama, chat_openai_compatible
from run_evals import DATASET, load_store

WEB = Path(__file__).parent / "web"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/kartograf-theme.js": ("kartograf-theme.js", "text/javascript; charset=utf-8")}
# Provider and limits come from the environment, so the same code runs on a laptop
# (Ollama, no limits) and as a public demo (hosted open model, rate limits).
#   KARTOGRAF_PROVIDER   ollama (default) | openai  (any OpenAI-compatible endpoint)
#   KARTOGRAF_BASE_URL   e.g. https://router.huggingface.co/v1
#   KARTOGRAF_API_KEY    key for that endpoint, never sent to the browser
#   KARTOGRAF_MODELS     comma-separated, first is the default
#   KARTOGRAF_EXTRA_BODY JSON merged into each request (provider options)
#   KARTOGRAF_PUBLIC     1 = per-visitor rate limit and a daily cap
#   KARTOGRAF_RATE       questions per visitor per 10 minutes (default 6)
#   KARTOGRAF_DAILY      questions per day across all visitors (default 300)
PROVIDER = os.environ.get("KARTOGRAF_PROVIDER", "ollama")
BASE_URL = os.environ.get("KARTOGRAF_BASE_URL", "http://localhost:11434/v1")
API_KEY = os.environ.get("KARTOGRAF_API_KEY", "")
EXTRA_BODY = json.loads(os.environ.get("KARTOGRAF_EXTRA_BODY", "{}"))
MODELS = tuple(m.strip() for m in os.environ.get("KARTOGRAF_MODELS", "qwen3:8b,llama3.1:8b").split(",") if m.strip())
PUBLIC = os.environ.get("KARTOGRAF_PUBLIC", "0") == "1"
RATE_PER_10_MIN = int(os.environ.get("KARTOGRAF_RATE", "6"))
DAILY_CAP = int(os.environ.get("KARTOGRAF_DAILY", "300"))
MAX_QUESTION_CHARS = 300 if PUBLIC else 500
MAP_ROWS = 5_000
CHART_MAX_BARS = 12

DATA_CONTEXT = """Data context: every table covers the Frankfurt am Main area and nothing else.
"Frankfurt" in a question means the whole table: do not filter by name, state or
locality to find Frankfurt. The roads table holds classified roads only (motorway,
trunk, primary, secondary, tertiary), not residential streets.

What the data does not contain: construction sites or roadworks, traffic, accidents,
speed limits, opening hours, prices, rents, population or events. The places table
lists businesses and points of interest; a construction company is not a
construction site. When a question asks for something the tables do not record,
say that the data does not contain it, name what it does contain, and do not
report a count from a loosely related category."""

# Simple single-table aggregates the server can break down for the chart:
# SELECT <aggregate> FROM roads|places [WHERE ...]  (no joins, CTEs or GROUP BY).
_SIMPLE = re.compile(r"^\s*select\s+(?P<expr>.+?)\s+from\s+(?P<table>roads|places)\b(?P<rest>.*?)\s*;?\s*$",
                     re.IGNORECASE | re.DOTALL)
_COMPLEX = re.compile(r"\b(join|group\s+by|union|with|having|limit|over)\b|\(\s*select", re.IGNORECASE)
_AGG = re.compile(r"\b(count|sum|avg|min|max)\s*\(", re.IGNORECASE)
BREAKDOWN_COLUMN = {"roads": "class", "places": "category"}


# --------------------------------------------------------------------- results to UI

def derived_queries(sql: str) -> dict[str, str]:
    """Chart and map queries derived from a simple aggregate the model wrote.

    'SELECT count(*) FROM places WHERE <filter>' becomes a breakdown of the same
    filter by category, and a list of the matching places for the map. The
    model's filter is reused verbatim, so the chart explains the model's number.
    """
    m = _SIMPLE.match(sql)
    if not m or _COMPLEX.search(sql) or not _AGG.search(m["expr"]):
        return {}
    table, rest = m["table"].lower(), m["rest"].strip().rstrip(";")
    if rest and not rest.lower().startswith("where"):
        return {}
    col = BREAKDOWN_COLUMN[table]
    expr = re.sub(r'\s+as\s+"?\w+"?\s*$', "", m["expr"].strip(), flags=re.IGNORECASE)  # model's own alias
    unit = "km" if "length_km" in expr.lower() else "count" if expr.lower().startswith("count") else "value"
    out = {"breakdown": f"SELECT {col}, {expr} AS {unit} FROM {table} {rest} "
                        f"GROUP BY {col} ORDER BY {unit} DESC LIMIT {CHART_MAX_BARS}"}
    if table == "places" and rest:
        out["map"] = f"SELECT name, category, lon, lat FROM places {rest} LIMIT 500"
    return out


def chart_source(chart: dict | None, sqls: list[str]) -> dict | None:
    """Tag a bar chart with the table its label column belongs to, if clickable."""
    if not chart or chart["type"] != "bar":
        return chart
    for sql in reversed(sqls):
        m = re.search(r"\bfrom\s+(roads|places)\b", sql, re.IGNORECASE)
        if m and (m[1].lower(), chart["label"]) in HIGHLIGHT_COLUMNS:
            return {**chart, "table": m[1].lower()}
    return chart


def pick_chart(results: list[pd.DataFrame]) -> dict | None:
    """Bar chart from the last result with a label column and a numeric column;
    otherwise a single-number card. None when nothing is chartable."""
    for df in reversed(results):
        if df is None or not 2 <= len(df) <= 60:
            continue
        num = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and c not in ("lon", "lat")]
        lab = [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])
               and c not in ("geometry_geojson", "_geojson")]
        if num and lab:
            top = df[[lab[0], num[0]]].dropna().sort_values(num[0], ascending=False).head(CHART_MAX_BARS)
            return {"type": "bar", "label": lab[0], "value": num[0],
                    "rows": [{"label": str(r[0]), "value": float(r[1])} for r in top.itertuples(index=False)]}
    for df in reversed(results):
        if df is not None and df.shape == (1, 1) and pd.api.types.is_numeric_dtype(df.iloc[:, 0]):
            return {"type": "number", "label": str(df.columns[0]), "value": float(df.iloc[0, 0])}
    return None


def map_layer(store: DataStore, sqls: list[str]) -> dict | None:
    """Re-run the last query whose result has coordinates, with a higher row cap."""
    for sql in reversed(sqls):
        try:
            df = store.run_sql(sql, max_rows=MAP_ROWS)
        except Exception:
            continue
        if not df.empty and kepler_view.is_mappable(df):
            frame, _ = kepler_view.prepare(df, MAP_ROWS)
            return {"id": "answer", "label": "Answer", "csv": frame.to_csv(index=False),
                    "columns": list(frame.columns), "rows": len(frame)}
    return None


# Chart bars the user can click: (table, column) pairs only, value bound as a parameter.
HIGHLIGHT_COLUMNS = {("roads", "class"), ("places", "category"), ("places", "basic_category")}


def highlight_layer(store: DataStore, table: str, column: str, value: str) -> dict | None:
    """Rows of one chart bar, for the map. Whitelisted table and column; the value
    is a bound parameter, never spliced into SQL."""
    if (table, column) not in HIGHLIGHT_COLUMNS:
        return None
    df = store.con.execute(f'SELECT * FROM "{table}" WHERE "{column}" = ? LIMIT {MAP_ROWS}', [value]).fetchdf()
    if df.empty or not kepler_view.is_mappable(df):
        return None
    frame, _ = kepler_view.prepare(df, MAP_ROWS)
    return {"id": "highlight", "label": f"{column} = {value}", "csv": frame.to_csv(index=False),
            "columns": list(frame.columns), "rows": len(frame)}


def base_layers(store: DataStore) -> list[dict]:
    out = []
    for name in ("roads", "places"):
        if name in store.tables:
            frame, _ = kepler_view.prepare(store.tables[name].df)
            out.append({"id": name, "label": name.capitalize(), "csv": frame.to_csv(index=False),
                        "columns": list(frame.columns), "rows": len(frame)})
    return out


class Limiter:
    """Per-visitor sliding window plus a global daily cap. In memory: a restart resets it."""

    def __init__(self, per_window: int, window_s: float, daily: int) -> None:
        self.per_window, self.window_s, self.daily = per_window, window_s, daily
        self.hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.day, self.count = datetime.date.today(), 0
        self.lock = threading.Lock()

    def check(self, visitor: str, now: float | None = None) -> str | None:
        """None if allowed (and counted), else a message for the visitor."""
        now = time.time() if now is None else now
        with self.lock:
            if datetime.date.today() != self.day:
                self.day, self.count = datetime.date.today(), 0
            if self.count >= self.daily:
                return "The demo has reached today's question limit. It resets at midnight UTC."
            q = self.hits[visitor]
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.per_window:
                return f"That is {self.per_window} questions in 10 minutes. Please wait a few minutes."
            q.append(now)
            self.count += 1
            return None


LIMITER = Limiter(RATE_PER_10_MIN, 600, DAILY_CAP)


def run_model(store: DataStore, question: str, model: str):
    if PROVIDER == "openai":
        return chat_openai_compatible(store, [], question, model=model, base_url=BASE_URL, api_key=API_KEY,
                                      extra_instructions=DATA_CONTEXT, extra_body=EXTRA_BODY)
    return chat_ollama(store, [], question, model=model, extra_instructions=DATA_CONTEXT,
                       think=False if model.startswith("qwen3") else None)


def ask(store: DataStore, question: str, model: str) -> dict:
    result, _ = run_model(store, question, model)
    queries = [{"sql": q.sql, "purpose": q.purpose, "ok": q.ok, "rows": q.rows, "error": q.error,
                "seconds": round(q.seconds, 2), "by": "model"} for q in result.queries]
    frames = [q.result for q in result.queries if q.ok]
    map_sqls = [q.sql for q in result.queries if q.ok]

    # Derive a breakdown and a map query from the last simple aggregate, if any.
    for q in reversed([q for q in result.queries if q.ok]):
        derived = derived_queries(q.sql)
        if not derived:
            continue
        for kind, sql in derived.items():
            t0 = time.perf_counter()
            try:
                df = store.run_sql(sql, max_rows=MAP_ROWS if kind == "map" else CHART_MAX_BARS)
            except Exception as e:
                queries.append({"sql": sql, "purpose": f"{kind} added by Kartograf", "ok": False,
                                "rows": 0, "error": str(e), "seconds": 0, "by": "kartograf"})
                continue
            queries.append({"sql": sql, "purpose": f"{kind} added by Kartograf", "ok": True, "rows": len(df),
                            "error": None, "seconds": round(time.perf_counter() - t0, 2), "by": "kartograf"})
            if kind == "breakdown" and len(df) >= 2:
                frames.append(df)
            if kind == "map":
                map_sqls.append(sql)
        break

    return {
        "answer": result.answer, "model": result.model, "seconds": round(result.seconds, 1),
        "queries": queries,
        "chart": chart_source(pick_chart(frames), [q["sql"] for q in queries if q["ok"]]),
        "map": map_layer(store, map_sqls),
    }


# --------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    store: DataStore
    lock = threading.Lock()          # one DuckDB connection, one question at a time
    layers_json: bytes = b"[]"
    meta: dict = {}

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode())

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send(HTTPStatus.OK, (WEB / name).read_bytes(), ctype)
        if path == "/api/layers":
            return self._send(HTTPStatus.OK, self.layers_json)
        if path == "/api/meta":
            return self._json(self.meta)
        if path == "/api/highlight":
            q = parse_qs(urlsplit(self.path).query)
            args = [q.get(k, [""])[0][:200] for k in ("table", "column", "value")]
            with self.lock:
                layer = highlight_layer(self.store, *args)
            return self._json(layer) if layer else self._json({"error": "nothing to highlight"}, HTTPStatus.NOT_FOUND)
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/ask":
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            question = str(body.get("question", "")).strip()[:MAX_QUESTION_CHARS]
            model = body.get("model") if body.get("model") in MODELS else MODELS[0]
        except (ValueError, TypeError):
            return self._json({"error": "bad request"}, HTTPStatus.BAD_REQUEST)
        if not question:
            return self._json({"error": "empty question"}, HTTPStatus.BAD_REQUEST)
        if PUBLIC:
            # Behind the hosting proxy the visitor is the first X-Forwarded-For entry.
            visitor = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
            refusal = LIMITER.check(visitor)
            if refusal:
                return self._json({"error": refusal}, HTTPStatus.TOO_MANY_REQUESTS)
        try:
            with self.lock:
                return self._json(ask(self.store, question, model))
        except Exception as e:
            detail = "the model service did not answer, please try again" if PUBLIC else f"{type(e).__name__}: {e}"
            return self._json({"error": detail}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, fmt, *args) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.command} {self.path.split('?')[0]}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")))
    ap.add_argument("--host", default=os.environ.get("KARTOGRAF_HOST", "127.0.0.1"),
                    help="0.0.0.0 only inside a container or behind a proxy")
    args = ap.parse_args()

    store = load_store()
    Handler.store = store
    Handler.layers_json = json.dumps(base_layers(store), ensure_ascii=False).encode()
    release = sorted(p.name[len("stats_"):-len(".csv")] for p in DATASET.glob("stats_*.csv"))[-1]
    Handler.meta = {"area": "Frankfurt am Main", "release": release, "models": list(MODELS),
                    "tables": {n: t.feature_count for n, t in store.tables.items()},
                    "hosted": PROVIDER == "openai", "public": PUBLIC,
                    "limits": {"per_10_min": RATE_PER_10_MIN, "daily": DAILY_CAP} if PUBLIC else None}
    # Default is localhost only: the API runs model-written SQL against local data.
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Kartograf on http://localhost:{args.port}  ({release}, models: {', '.join(MODELS)})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
