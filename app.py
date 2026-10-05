"""Geo Data Chat: ask questions about CSV and GeoJSON files in plain language.

Run:  streamlit run app.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

import attribution
import charts
import scopes
import snapshots
from data_loader import DataStore, dataframe_to_bytes, load_file, table_statistics
from llm import ChatResult, chat_anthropic, chat_ollama

APP_DIR = Path(__file__).parent
SAMPLE_DIR = APP_DIR / "sample_data"
OVERTURE_ROOT = scopes.DATA_ROOT
LOG_PATH = APP_DIR / "query_log.jsonl"

st.set_page_config(page_title="Geo Data Chat", page_icon="🗺️", layout="wide")

# ----------------------------------------------------------------------------- state

if "store" not in st.session_state:
    st.session_state.store = DataStore()
if "chat" not in st.session_state:          # what the user sees
    st.session_state.chat = []
if "history" not in st.session_state:       # what the model sees (provider format)
    st.session_state.history = []
if "history_provider" not in st.session_state:
    st.session_state.history_provider = None
if "loaded_files" not in st.session_state:
    st.session_state.loaded_files = set()

store: DataStore = st.session_state.store


def add_file(name: str, raw: bytes) -> None:
    if name in st.session_state.loaded_files:
        return
    try:
        table = load_file(name, raw, existing_names=store.tables.keys())
        store.add(table)
        st.session_state.loaded_files.add(name)
        for n in table.notes:
            st.sidebar.warning(f"{name}: {n}")
    except Exception as e:
        st.sidebar.error(f"Could not load {name}: {e}")


def reset_chat() -> None:
    st.session_state.chat = []
    st.session_state.history = []
    st.session_state.history_provider = None


def theme_mode() -> str:
    """The viewer's theme, so chart colours are selected rather than flipped."""
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


@st.cache_data(show_spinner=False)
def load_overture_stats(dir_str: str, _mtimes: tuple) -> snapshots.ReleaseSet:
    return snapshots.load_release_stats(Path(dir_str))


@st.cache_data(show_spinner="Reading road geometry...")
def load_road_paths(path_str: str, _mtime: float) -> pd.DataFrame:
    """Road GeoJSON as one row per segment with a coordinate path, for pydeck."""
    fc = json.loads(scopes.read_bytes_maybe_gzip(Path(path_str)))
    rows = []
    for feat in fc.get("features", []):
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        if geom.get("type") == "LineString":
            paths = [geom["coordinates"]]
        elif geom.get("type") == "MultiLineString":
            paths = geom["coordinates"]
        else:
            continue
        for p in paths:
            rows.append({"id": feat.get("id"), "class": props.get("class"),
                         "state": props.get("state"), "name": props.get("name") or "",
                         "length_km": props.get("length_km"), "path": p})
    return pd.DataFrame(rows)


def overture_mtimes(directory: Path) -> tuple:
    if not directory.exists():
        return ()
    return tuple(sorted((p.name, p.stat().st_mtime) for p in directory.glob("*")))


def log_turn(question: str, result: ChatResult) -> None:
    """Append one JSON line per turn. This file is the seed of the eval set."""
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "question": question,
        "answer": result.answer,
        "provider": result.provider,
        "model": result.model,
        "seconds": round(result.seconds, 2),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "queries": [{"sql": q.sql, "purpose": q.purpose, "ok": q.ok, "rows": q.rows,
                     "error": q.error, "seconds": round(q.seconds, 3)} for q in result.queries],
        "tables": sorted(store.tables.keys()),
        "rating": None,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("🗺️ Geo Data Chat")
    st.caption("CSV and GeoJSON in, questions in plain language, SQL you can audit.")

    st.subheader("Model")
    provider = st.radio("Provider", ["Claude (Anthropic API)", "Local (Ollama)"],
                        help="Local keeps every byte on this machine. Claude sends schema, sample rows and query results to the API.")
    if provider.startswith("Claude"):
        model = st.text_input("Model", value="claude-opus-5")
        effort = st.select_slider("Effort", options=["low", "medium", "high"], value="medium")
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        if not has_key:
            st.info("Set ANTHROPIC_API_KEY in the environment (or log in with the ant CLI) before asking questions.")
        ollama_host = None
    else:
        model = st.text_input("Ollama model", value="qwen3:8b", help="Must support tool calling, e.g. qwen3, llama3.1, mistral-nemo.")
        ollama_host = st.text_input("Ollama host", value="http://localhost:11434")
        effort = None

    st.subheader("Data")
    uploads = st.file_uploader("Add CSV or GeoJSON files", type=["csv", "geojson", "json", "gz"], accept_multiple_files=True)
    for up in uploads or []:
        add_file(up.name, up.getvalue())
    if st.button("Load sample data (Berlin charging sessions + districts)"):
        for p in sorted(SAMPLE_DIR.glob("*")):
            add_file(p.name, p.read_bytes())

    datasets = scopes.dataset_dirs(OVERTURE_ROOT)
    dataset_dir = None
    if datasets:
        dataset_dir = st.selectbox("Overture dataset", datasets, format_func=lambda d: scopes.label(d.name))
        files = sorted(list(dataset_dir.glob("stats_*.csv")) + list(dataset_dir.glob("roads_*.geojson*")))
        if st.button(f"Load Overture roads ({scopes.label(dataset_dir.name)})"):
            for p in files:
                if p.stat().st_size > 0:
                    add_file(f"{dataset_dir.name}_{p.name}", p.read_bytes())
        size_mb = sum(p.stat().st_size for p in dataset_dir.glob("*")) / 1e6
        st.caption(f"{len(files)} file(s), {size_mb:.1f} MB on disk")

    if store.tables:
        st.markdown("**Loaded tables**")
        for t in list(store.tables.values()):
            c1, c2 = st.columns([4, 1])
            c1.write(f"`{t.name}` · {t.feature_count} rows · {t.kind}")
            if c2.button("✕", key=f"rm_{t.name}", help="Remove table"):
                store.remove(t.name)
                st.session_state.loaded_files.discard(t.source_file)
                st.rerun()
    st.caption("Spatial extension: " + ("loaded" if store.spatial else "not available (lon/lat columns still work)"))

    if st.button("Clear chat"):
        reset_chat()
        st.rerun()

    st.divider()
    st.caption("**Data sources**")
    st.caption(attribution.DATA_ATTRIBUTION)
    st.caption(attribution.NOT_AFFILIATED)
    st.caption(attribution.SAMPLE_DATA_NOTE)

# ----------------------------------------------------------------------------- tabs

tab_chat, tab_rel, tab_stats, tab_data, tab_log = st.tabs(
    ["💬 Chat", "🛰️ Releases", "📊 Statistics", "🗂️ Data", "🧾 Query log"])

with tab_chat:
    if not store.tables:
        st.info("Load a CSV or GeoJSON file in the sidebar, or click the sample data button, then ask a question.")
    for turn in st.session_state.chat:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            for q in turn.get("queries", []):
                label = ("✅" if q["ok"] else "❌") + f" SQL · {q['purpose'] or 'query'} · {q['rows']} rows · {q['seconds']:.2f}s"
                with st.expander(label):
                    st.code(q["sql"], language="sql")
                    if q["error"]:
                        st.error(q["error"])
                    elif q["result"] is not None and not q["result"].empty:
                        st.dataframe(q["result"], width="stretch")
            if turn.get("meta"):
                st.caption(turn["meta"])

    question = st.chat_input("Ask about the data, e.g. 'Which district has the highest average energy per session?'",
                             disabled=not store.tables)
    if question:
        # Switching provider mid-conversation would send the wrong message format.
        prov_key = "anthropic" if provider.startswith("Claude") else "ollama"
        if st.session_state.history_provider not in (None, prov_key):
            reset_chat()
        st.session_state.history_provider = prov_key

        st.session_state.chat.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            status = st.status("Working...", expanded=False)
            try:
                if prov_key == "anthropic":
                    result, new_history = chat_anthropic(store, st.session_state.history, question,
                                                         model=model, effort=effort, on_status=status.update)
                else:
                    result, new_history = chat_ollama(store, st.session_state.history, question,
                                                      model=model, host=ollama_host, on_status=status.update)
                status.update(label="Done", state="complete")
            except Exception as e:
                status.update(label="Failed", state="error")
                st.error(f"{type(e).__name__}: {e}")
                st.session_state.chat.append({"role": "assistant", "content": f"Error: {e}", "queries": []})
                st.stop()

        st.session_state.history = new_history
        meta = f"{result.provider} · {result.model} · {result.seconds:.1f}s"
        if result.input_tokens:
            meta += f" · {result.input_tokens} in / {result.output_tokens} out tokens"
        st.session_state.chat.append({
            "role": "assistant", "content": result.answer, "meta": meta,
            "queries": [{"sql": q.sql, "purpose": q.purpose, "ok": q.ok, "rows": q.rows,
                         "seconds": q.seconds, "error": q.error, "result": q.result} for q in result.queries],
        })
        log_turn(question, result)
        st.rerun()

with tab_rel:
    mode = theme_mode()
    rs = (load_overture_stats(str(dataset_dir), overture_mtimes(dataset_dir))
          if dataset_dir else snapshots.ReleaseSet(stats=pd.DataFrame(), releases=[]))

    if not rs.available:
        st.info("No Overture statistics yet. Check the transfer first, then download a small region:\n\n"
                "```bash\npython fetch_overture.py --preset berlin --estimate\n"
                "python fetch_overture.py --preset berlin\n```\n\n"
                "Each region lands in its own folder under `data/overture/`. "
                "It reads the public Overture bucket over S3 and needs no credentials.")
    else:
        latest = rs.releases[-1]
        head = snapshots.headline(rs.stats, latest)
        scope = rs.scopes.get(latest) or {}
        scope_classes = scope.get("classes") or sorted(rs.stats.loc[rs.stats.release == latest, "class"].unique())
        full_drivable = {"residential", "living_street", "unclassified"} <= set(scope_classes)
        network_label = "drivable road network" if full_drivable else "classified road network"
        st.subheader(f"{scopes.label(dataset_dir.name)}, {network_label} · Overture {latest}")
        per_state = (rs.stats[rs.stats.release == latest].groupby("state")["length_km"].sum()
                     .sort_values(ascending=False))
        states_txt = ", ".join(f"{st_name} {km:,.0f} km" for st_name, km in per_state.items())
        st.caption("Road classes counted: " + ", ".join(scope_classes) + ". "
                   + ("States: " if dataset_dir.name == "germany" else
                      "The area is a rectangle, so it also clips neighbouring states: ")
                   + states_txt + ".")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Network length", f"{head['total_km']:,.0f} km")
        c2.metric("Segments", f"{head['segments']:,}")
        c3.metric("Named, by length", f"{head['pct_named']:.1f} %")
        peers = rs.comparable_with(latest)
        earlier = [r for r in peers if r < latest]
        if earlier:
            prev = snapshots.headline(rs.stats, earlier[-1])
            d = head["total_km"] - prev["total_km"]
            c4.metric(f"vs {earlier[-1]}", f"{d:+,.0f} km",
                      f"{100 * d / prev['total_km']:+.2f} %" if prev["total_km"] else None)
        else:
            c4.metric("Comparable releases", len(peers))

        excluded = [r for r in rs.releases if r not in peers]
        if excluded:
            st.caption("Not shown in the time series because their scope differs from "
                       f"{latest}: {', '.join(excluded)}. " +
                       " ".join(rs.comparable(latest, r)[1] for r in excluded))

        weekly = snapshots.build_weekly_series(rs.stats[rs.stats["release"].isin(peers)])
        totals = snapshots.weekly_totals(weekly)

        st.altair_chart(charts.total_km_over_time(totals, mode), width="stretch")

        anomalies = snapshots.detect_anomalies(totals, threshold_pct=-0.5)
        if not anomalies.empty:
            worst = anomalies.iloc[anomalies["wow_pct"].argmin()]
            cause = snapshots.explain_anomaly(worst["week"])
            msg = (f"⚠️ Week-over-week drop detected on {worst['week']:%d %b %Y}: "
                   f"{worst['wow_pct']:.2f} % ({worst['length_km'] - worst['prev_km']:,.0f} km). ")
            if cause:
                msg += (f"This matches the injected `{cause}` affecting "
                        f"{', '.join(snapshots.INCIDENT['states'])}.")
            else:
                msg += "No known cause. Investigate before trusting this release."
            st.warning(msg)
            if len(anomalies) > 1:
                st.caption(f"{len(anomalies)} weeks crossed the threshold in total.")

        st.altair_chart(charts.km_by_class_small_multiples(weekly, mode), width="stretch")
        st.altair_chart(charts.name_coverage_over_time(totals, mode), width="stretch")

        with st.expander("Weekly figures as a table"):
            tbl = totals[["week", "length_km", "segments", "pct_named", "source"]].copy()
            tbl["week"] = tbl["week"].dt.strftime("%Y-%m-%d")
            st.dataframe(tbl.round({"length_km": 1, "pct_named": 2}),
                         width="stretch", hide_index=True)
            st.download_button("Download weekly series (CSV)",
                               data=weekly.to_csv(index=False).encode(),
                               file_name="overture_weekly_series.csv", mime="text/csv")

        st.divider()
        st.subheader("Compare two releases")
        if len(rs.releases) < 2:
            st.caption("Only one release downloaded. Fetch a second one to enable the comparison.")
        else:
            cc1, cc2, cc3 = st.columns(3)
            rel_a = cc1.selectbox("Release A", rs.releases, index=0)
            rel_b = cc2.selectbox("Release B", rs.releases, index=len(rs.releases) - 1)
            dim = cc3.selectbox("Break down by", ["state", "class"], index=0)

            ok, why = rs.comparable(rel_a, rel_b)
            if rel_a == rel_b:
                st.caption("Pick two different releases.")
            elif not ok:
                st.error(f"These releases cannot be compared. {why}")
            else:
                cmp = snapshots.compare_releases(rs.stats, rel_a, rel_b, dimension=dim)
                total_delta = cmp["delta_km"].sum()
                m1, m2, m3 = st.columns(3)
                m1.metric("Net change", f"{total_delta:+,.0f} km")
                m2.metric("Gained", f"{cmp.loc[cmp.delta_km > 0, 'delta_km'].sum():+,.0f} km")
                m3.metric("Lost", f"{cmp.loc[cmp.delta_km < 0, 'delta_km'].sum():+,.0f} km")

                st.altair_chart(charts.delta_by_dimension(cmp, dim, mode), width="stretch")
                st.dataframe(
                    cmp.rename(columns={"length_km_a": f"km {rel_a}", "length_km_b": f"km {rel_b}"})
                       .round({f"km {rel_a}": 1, f"km {rel_b}": 1, "delta_km": 1, "pct_change": 2}),
                    width="stretch", hide_index=True)

            st.divider()
            st.subheader("Map: what changed on motorway and trunk")
            geo_a = scopes.roads_path(dataset_dir, rel_a)
            geo_b = scopes.roads_path(dataset_dir, rel_b)
            if not (geo_a and geo_b):
                st.caption("Road geometry for both releases is not downloaded yet.")
            elif rel_a == rel_b or not ok:
                st.caption("Pick two different, comparable releases to see a difference map.")
            else:
                pa = load_road_paths(str(geo_a), geo_a.stat().st_mtime)
                pb = load_road_paths(str(geo_b), geo_b.stat().st_mtime)
                ids_a, ids_b = set(pa["id"]), set(pb["id"])
                added = pb[pb["id"].isin(ids_b - ids_a)]
                removed = pa[pa["id"].isin(ids_a - ids_b)]
                kept = pb[pb["id"].isin(ids_a & ids_b)]

                k1, k2, k3 = st.columns(3)
                k1.metric("Unchanged segments", f"{kept['id'].nunique():,}")
                k2.metric("Added in B", f"{added['id'].nunique():,}",
                          f"{added['length_km'].sum():+,.0f} km")
                k3.metric("Removed in B", f"{removed['id'].nunique():,}",
                          f"{-removed['length_km'].sum():+,.0f} km")

                chg = kept.merge(pa[["id", "length_km"]].drop_duplicates("id"), on="id",
                                 suffixes=("", "_a"))
                st.caption(f"Net change on these classes is "
                           f"{added['length_km'].sum() - removed['length_km'].sum():+,.1f} km, "
                           f"but {added['id'].nunique() + removed['id'].nunique():,} segments were "
                           f"added or removed and {(abs(chg['length_km'] - chg['length_km_a']) > 0.001).sum():,} "
                           f"kept segments changed length. A total alone would hide this churn.")
                show = st.multiselect("Show layers", ["Unchanged", "Added", "Removed"],
                                      default=["Added", "Removed"],
                                      help="Unchanged draws every motorway and trunk segment in Germany and is slower.")
                layers = []
                if "Unchanged" in show:
                    layers.append(pdk.Layer("PathLayer", data=kept, get_path="path",
                                            get_color=[150, 150, 150, 90], width_min_pixels=1,
                                            pickable=False))
                if "Added" in show:
                    layers.append(pdk.Layer("PathLayer", data=added, get_path="path",
                                            get_color=[42, 120, 214], width_min_pixels=3,
                                            pickable=True))
                if "Removed" in show:
                    layers.append(pdk.Layer("PathLayer", data=removed, get_path="path",
                                            get_color=[227, 73, 72], width_min_pixels=3,
                                            pickable=True))
                st.pydeck_chart(pdk.Deck(
                    layers=layers,
                    initial_view_state=pdk.ViewState(latitude=51.1, longitude=10.4, zoom=4.8),
                    tooltip={"text": "{class} {name}\n{state}\n{length_km} km"},
                ), height=560)
                st.caption("Grey is present in both releases. Blue is new in B. Red is gone from B. "
                           "Colours match the charts: blue for gained, red for lost. "
                           + attribution.DATA_ATTRIBUTION + " " + attribution.BASEMAP_ATTRIBUTION)

        st.divider()
        with st.expander("Raw release statistics"):
            st.dataframe(rs.stats, width="stretch", hide_index=True)
            st.download_button("Download release statistics (CSV)",
                               data=rs.stats.to_csv(index=False).encode(),
                               file_name="overture_release_stats.csv", mime="text/csv")
        st.caption("Kilometres are geodesic lengths computed with ST_Length_Spheroid over "
                   "Overture segment geometry, attributed to a Bundesland by segment centroid. "
                   "Footways, service roads, tracks, cycleways and steps are never counted. "
                   "Each carriageway direction is its own segment and motorway ramps carry the "
                   "motorway class, so Overture motorway kilometres run about three times the "
                   "official Autobahn route length, and other classes about 1.4 to 1.5 times.")
        st.caption(f"Source: {attribution.OVERTURE_CITATION}. {attribution.DATA_ATTRIBUTION} "
                   f"{attribution.SOURCE_DETAIL} {attribution.NOT_AFFILIATED}")

with tab_stats:
    if not store.tables:
        st.info("No tables loaded.")
    for t in store.tables.values():
        st.subheader(f"{t.name}  ·  {t.source_file}")
        s = table_statistics(t)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Rows", f"{s['rows']:,}")
        c2.metric("Columns", s["columns"])
        c3.metric("Memory", f"{s['memory_mb']} MB")
        c4.metric("Kind", t.kind)
        if "total_length_km" in s:
            k1, k2, k3 = st.columns(3)
            k1.metric("Total length", f"{s['total_length_km']:,.1f} km")
            k2.metric("Line features", f"{s['line_features']:,}")
            k3.metric("Mean segment", f"{s['mean_segment_m']:,.0f} m")
        if "geometry_types" in s:
            st.write("Geometry types:", s["geometry_types"])
        if "bbox" in s:
            b = s["bbox"]
            st.write(f"Bounding box: lon {b['min_lon']:.4f} to {b['max_lon']:.4f}, lat {b['min_lat']:.4f} to {b['max_lat']:.4f}")
        left, right = st.columns(2)
        with left:
            st.markdown("**Columns**")
            st.dataframe(s["column_overview"], width="stretch", hide_index=True)
        with right:
            st.markdown("**Numeric summary**")
            if s["numeric_describe"].empty:
                st.caption("No numeric columns.")
            else:
                st.dataframe(s["numeric_describe"].round(3), width="stretch")
        if "points" in s:
            st.markdown("**Map** (representative points, up to 5,000)")
            st.map(s["points"].sample(min(len(s["points"]), 5000), random_state=0), size=20)
        st.divider()

with tab_data:
    if not store.tables:
        st.info("No tables loaded.")
    for t in store.tables.values():
        with st.expander(f"{t.name} ({t.feature_count} rows)", expanded=len(store.tables) == 1):
            show = t.df.drop(columns=["geometry_geojson"], errors="ignore")
            st.dataframe(show.head(500), width="stretch")
            st.download_button("Download as CSV", data=dataframe_to_bytes(show),
                               file_name=f"{t.name}.csv", mime="text/csv", key=f"dl_{t.name}")
    st.markdown("**Run your own SQL** (read-only)")
    manual_sql = st.text_area("SQL", value="", placeholder='SELECT district, count(*) FROM charging_sessions GROUP BY 1 ORDER BY 2 DESC', label_visibility="collapsed")
    if st.button("Run SQL", disabled=not manual_sql.strip()):
        try:
            st.dataframe(store.run_sql(manual_sql, max_rows=1000), width="stretch")
        except Exception as e:
            st.error(str(e))

with tab_log:
    st.markdown("Every question, the SQL the model ran, and the answer are appended to `query_log.jsonl`. "
                "This is the raw material for the eval set: label each row correct or not and you have a baseline.")
    if LOG_PATH.exists():
        rows = [json.loads(l) for l in LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
        if rows:
            flat = pd.DataFrame([{
                "ts": r["ts"], "provider": r["provider"], "model": r["model"], "seconds": r["seconds"],
                "queries": len(r["queries"]), "failed_queries": sum(1 for q in r["queries"] if not q["ok"]),
                "question": r["question"], "answer": r["answer"][:200],
            } for r in rows])
            c1, c2, c3 = st.columns(3)
            c1.metric("Turns logged", len(flat))
            c2.metric("Median seconds", f"{flat['seconds'].median():.1f}")
            c3.metric("Turns with a failed query", int((flat["failed_queries"] > 0).sum()))
            st.dataframe(flat, width="stretch", hide_index=True)
            st.download_button("Download log (JSONL)", data=LOG_PATH.read_bytes(), file_name="query_log.jsonl")
    else:
        st.caption("No turns logged yet.")
