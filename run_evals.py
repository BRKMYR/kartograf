"""Score local models on the Frankfurt eval set.

Each question runs as a fresh conversation through the same tool loop the app
uses (llm.chat_ollama with one read-only run_sql tool). Expected answers are
computed from each question's reference_sql against the same data.

Usage:
  python run_evals.py --model llama3.1:8b --model qwen3:8b
  python run_evals.py --model llama3.1:8b --only en01 de03

Writes evals/results/<date>_<model>.jsonl per model and evals/results/SUMMARY.md.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

import scopes
from data_loader import DataStore, load_file
from llm import ChatResult, chat_ollama

ROOT = Path(__file__).parent
QUESTIONS = ROOT / "evals" / "questions.yaml"
RESULTS = ROOT / "evals" / "results"
DATASET = scopes.DATA_ROOT / "frankfurt"

# A refusal says the data cannot answer. Kept deliberately simple and visible:
# the JSONL holds every answer, so a person can re-label any borderline case.
REFUSAL_MARKERS = (
    "cannot", "can't", "can not", "unable", "not available", "no data", "not contain",
    "doesn't contain", "does not include", "doesn't include", "not included", "no information",
    "not possible", "isn't possible", "no column", "not in the", "don't have", "do not have",
    "nicht", "keine", "kein ", "leider",
)


@dataclass
class Score:
    passed: bool
    tool_ok: bool
    detail: str


# --------------------------------------------------------------------- data

def load_store(directory: Path = DATASET) -> DataStore:
    files = scopes.latest_release_files(directory)
    if "places" not in files:
        sys.exit(f"No places extract in {directory}. Run:\n"
                 "  python fetch_overture.py --preset frankfurt --releases 2026-09-23.1 --places "
                 "--map-classes motorway trunk primary secondary tertiary")
    store = DataStore()
    for name, path in files.items():
        suffix = ".csv" if path.suffix == ".csv" else ".geojson.gz"
        store.add(load_file(f"{name}{suffix}", path.read_bytes(), store.tables.keys()))
    return store


def load_questions(path: Path = QUESTIONS) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def expected_values(store: DataStore, q: dict) -> list:
    if q["check"] == "refusal":
        return []
    df = store.run_sql(q["reference_sql"], max_rows=100)
    return df.iloc[:, 0].tolist()


# --------------------------------------------------------------------- scoring

_NUMBER = re.compile(r"\d[\d.,]*")


def numbers_in(text: str) -> list[float]:
    """Every reading of every number token, so '1.542' (German) and '1,542'
    (English) both yield 1542, and '460,27' yields 460.27."""
    out: list[float] = []
    for tok in _NUMBER.findall(text):
        tok = tok.rstrip(".,")
        for cand in (tok.replace(",", ""), tok.replace(".", "").replace(",", "."), tok.replace(",", ".")):
            try:
                out.append(float(cand))
            except ValueError:
                pass
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(s).lower()).strip()


def score(q: dict, result: ChatResult, expected: list) -> Score:
    tool_ok = any(r.ok for r in result.queries)
    answer = result.answer or ""
    check = q["check"]

    if check == "refusal":
        low = answer.lower()
        refused = any(m in low for m in REFUSAL_MARKERS)
        return Score(refused, tool_ok, "refused" if refused else "answered instead of refusing")

    if check == "number":
        target = float(expected[0])
        rel, abs_ = q.get("rel_tol", 0.0), q.get("abs_tol", 0.0)
        hit = any(math.isclose(v, target, rel_tol=rel, abs_tol=max(abs_, 1e-9)) for v in numbers_in(answer))
        return Score(hit, tool_ok, f"expected {target:g}")

    if check == "set":
        missing = [v for v in expected if _norm(v) not in _norm(answer)]
        return Score(not missing, tool_ok, "all named" if not missing else f"missing {missing}")

    raise ValueError(f"unknown check {check!r}")


# --------------------------------------------------------------------- run

def run_model(store: DataStore, questions: list[dict], model: str, host: str | None) -> list[dict]:
    rows = []
    for q in questions:
        expected = expected_values(store, q)
        t0 = time.perf_counter()
        try:
            result, _ = chat_ollama(store, [], q["question"], model=model, host=host)
        except Exception as e:  # a crashed turn is a failed turn, not a failed run
            result = ChatResult(answer=f"(error: {type(e).__name__}: {e})", queries=[], provider="ollama",
                                model=model, seconds=time.perf_counter() - t0)
        s = score(q, result, expected)
        rows.append({
            "id": q["id"], "lang": q["lang"], "check": q["check"], "question": q["question"],
            "expected": expected, "answer": result.answer, "passed": s.passed, "tool_ok": s.tool_ok,
            "detail": s.detail, "seconds": round(result.seconds, 2),
            "queries": [{"sql": r.sql, "ok": r.ok, "error": r.error} for r in result.queries],
        })
        print(f"  {q['id']} {'PASS' if s.passed else 'fail'}  {result.seconds:5.1f}s  {s.detail}", flush=True)
    return rows


def summarize(per_model: dict[str, list[dict]]) -> str:
    def pct(rows, key="passed"):
        return f"{sum(r[key] for r in rows)}/{len(rows)}" if rows else "-"

    lines = ["# Eval results", "",
             f"Run {time.strftime('%Y-%m-%d %H:%M')}, Frankfurt extract, "
             f"{len(next(iter(per_model.values())))} questions. Generated by `run_evals.py`.", "",
             "| Model | Correct | English | Deutsch | Numbers | Sets | Refusals | Valid SQL (answerable) | Median s |",
             "|---|---|---|---|---|---|---|---|---|"]
    for model, rows in per_model.items():
        by = lambda k, v: [r for r in rows if r[k] == v]
        answerable = [r for r in rows if r["check"] != "refusal"]
        lines.append(
            f"| {model} | {pct(rows)} | {pct(by('lang', 'en'))} | {pct(by('lang', 'de'))} | "
            f"{pct(by('check', 'number'))} | {pct(by('check', 'set'))} | {pct(by('check', 'refusal'))} | "
            f"{pct(answerable, 'tool_ok')} | {statistics.median(r['seconds'] for r in rows):.1f} |")
    lines += ["", "## Failures", ""]
    for model, rows in per_model.items():
        fails = [r for r in rows if not r["passed"]]
        lines.append(f"**{model}**: {len(fails)} failed" + (":" if fails else ""))
        for r in fails:
            lines.append(f"- `{r['id']}` {r['detail']}. Answer: {r['answer'][:160].replace(chr(10), ' ')}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", required=True, help="Ollama model; repeat to compare")
    ap.add_argument("--only", nargs="+", help="question ids to run")
    ap.add_argument("--host", default=None, help="Ollama host, default http://localhost:11434")
    args = ap.parse_args()

    store = load_store()
    questions = load_questions()
    if args.only:
        questions = [q for q in questions if q["id"] in set(args.only)]
    RESULTS.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y-%m-%d")  # run start, so one run never spans two dates
    per_model = {}
    for model in args.model:
        print(f"{model}: {len(questions)} questions", flush=True)
        rows = run_model(store, questions, model, args.host)
        per_model[model] = rows
        out = RESULTS / f"{stamp}_{model.replace(':', '_')}.jsonl"
        out.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")

    summary = summarize(per_model)
    (RESULTS / "SUMMARY.md").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
