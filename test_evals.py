"""Offline tests for the eval scorer and question file. No Ollama needed.

Run:  .venv/bin/python -m pytest -q test_evals.py
"""

from __future__ import annotations

import pytest

import run_evals
import scopes
from llm import ChatResult, QueryRecord


def _result(answer: str, ok: bool = True) -> ChatResult:
    return ChatResult(answer=answer, queries=[QueryRecord(sql="SELECT 1", purpose="", ok=ok, rows=1, seconds=0.1)])


@pytest.mark.parametrize("text, value", [
    ("There are 13 pharmacies.", 13.0),
    ("Es gibt 460,27 Kilometer Autobahn.", 460.27),
    ("1.542 Segmente", 1542.0),          # German thousands separator
    ("1,542 segments", 1542.0),          # English thousands separator
    ("about 0.70.", 0.70),
])
def test_numbers_in_reads_german_and_english(text, value):
    assert any(abs(v - value) < 1e-9 for v in run_evals.numbers_in(text))


def test_number_check_exact_and_tolerance():
    q = {"check": "number"}
    assert run_evals.score(q, _result("There are 13 pharmacies."), [13]).passed
    assert not run_evals.score(q, _result("There are 5193 pharmacies."), [13]).passed
    q_tol = {"check": "number", "rel_tol": 0.01}
    assert run_evals.score(q_tol, _result("460.27 km"), [460.3]).passed
    assert not run_evals.score(q_tol, _result("470 km"), [460.3]).passed


def test_set_check_ignores_case_and_underscores():
    q = {"check": "set"}
    exp = ["professional_service", "doctors_office"]
    assert run_evals.score(q, _result("Professional service and doctors office lead."), exp).passed
    r = run_evals.score(q, _result("professional_service only"), exp)
    assert not r.passed and "doctors_office" in r.detail


def test_refusal_check():
    q = {"check": "refusal"}
    assert run_evals.score(q, _result("The data does not contain accident records.", ok=False), []).passed
    assert run_evals.score(q, _result("Dazu enthält der Datensatz leider keine Angaben."), []).passed
    assert not run_evals.score(q, _result("There were 312 accidents on the A5."), []).passed


def test_tool_ok_reflects_queries():
    assert not run_evals.score({"check": "number"}, _result("13", ok=False), [13]).tool_ok


def test_question_file_is_balanced_and_complete():
    qs = run_evals.load_questions()
    assert len(qs) == 20 and len({q["id"] for q in qs}) == 20
    assert sum(q["lang"] == "de" for q in qs) == 10
    assert sum(q["check"] == "refusal" for q in qs) == 4
    for q in qs:
        assert q["check"] in {"number", "set", "refusal"}
        assert (q["check"] == "refusal") == ("reference_sql" not in q)
        assert "—" not in q["question"]


def test_frankfurt_preset_resolves():
    assert scopes.scope_name("frankfurt", scopes.PRESETS["frankfurt"]["bbox"]) == "frankfurt"
    assert "Frankfurt" in scopes.label("frankfurt")


def test_result_name_is_file_safe():
    assert run_evals.result_name("llama3.1:8b") == "llama3.1_8b"
    assert run_evals.result_name("Aleph-Alpha/Kolibri-1") == "Aleph-Alpha--Kolibri-1"


def test_latest_results_keeps_newest_run_per_model(tmp_path):
    def write(name, model, passed):
        row = {"id": "en01", "passed": passed, **({"model": model} if model else {})}
        (tmp_path / name).write_text(run_evals.json.dumps(row) + "\n", encoding="utf-8")

    write("2026-10-05_qwen3_8b.jsonl", None, False)            # older file without a model field
    write("2026-10-09_qwen3_8b.jsonl", "qwen3:8b", True)
    write("2026-10-09_Aleph-Alpha--Kolibri-1.jsonl", "Aleph-Alpha/Kolibri-1", True)
    latest = run_evals.latest_results(tmp_path)
    assert set(latest) == {"qwen3:8b", "Aleph-Alpha/Kolibri-1"}
    assert latest["qwen3:8b"][0] == "2026-10-09" and latest["qwen3:8b"][1][0]["passed"]
