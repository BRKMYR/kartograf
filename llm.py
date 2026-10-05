"""Model adapters: the same tool loop on Claude (Anthropic SDK) or a local Ollama model.

The model never sees the raw data. It sees the schema plus a few sample rows,
and it has exactly one tool: run_sql. Every query goes through the read-only
guard in data_loader before DuckDB executes it. The loop is capped so a
confused model cannot spin forever.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from data_loader import DataStore

MAX_TOOL_ROUNDS = 6
MAX_RESULT_ROWS = 50

SYSTEM_PROMPT = """You are a data analyst working on tables loaded into DuckDB.
Answer the user's question about the data. Use the run_sql tool to compute
anything numeric; never estimate numbers from the sample rows.

Rules:
- DuckDB SQL dialect. Quote identifiers with double quotes when needed.
- Only SELECT / WITH queries. The connection is read-only.
- Prefer one well-aggregated query over many small ones.
- Results are truncated to {max_rows} rows; aggregate rather than dump rows.
- Point columns lon/lat are WGS84 degrees. For distances without the spatial
  extension, use the haversine formula in SQL.
- If a query fails, read the error, fix the SQL and call run_sql again. Do not
  explain the error to the user instead of answering.
- If the question cannot be answered from these tables, say so plainly.

Spatial recipes (lon/lat are WGS84 degrees; distances must be in metres):
- Distance in metres between a row and a point:
  ST_Distance_Sphere(ST_Point(lon, lat), ST_Point(<lon>, <lat>))
- Rows within 1 km of a point:
  WHERE ST_Distance_Sphere(ST_Point(lon, lat), ST_Point(8.6625, 50.1070)) <= 1000
- Never use ST_Distance on lon/lat (that returns degrees). Never use ST_MakePoint.
- Category matching: compare with = on the exact category value; look up values
  with SELECT DISTINCT category ... WHERE category ILIKE '%term%' when unsure.
- Final answer: state the number(s), name the table(s) used, and mention any
  assumption. Keep it short. Do not repeat the SQL in the answer; the UI shows it.

Schema:
{schema}
"""

RUN_SQL_TOOL = {
    "name": "run_sql",
    "description": "Run one read-only DuckDB SQL query against the loaded tables and return the result rows as CSV.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SELECT or WITH query in DuckDB dialect."},
            "purpose": {"type": "string", "description": "One line on what this query is for."},
        },
        "required": ["sql", "purpose"],
        "additionalProperties": False,
    },
}


@dataclass
class QueryRecord:
    sql: str
    purpose: str
    ok: bool
    rows: int
    seconds: float
    error: str | None = None
    result: pd.DataFrame | None = None


@dataclass
class ChatResult:
    answer: str
    queries: list[QueryRecord] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None


def build_system_prompt(store: DataStore, extra: str = "") -> str:
    prompt = SYSTEM_PROMPT.format(max_rows=MAX_RESULT_ROWS, schema=store.schema_description())
    return prompt + ("\n" + extra.strip() + "\n" if extra.strip() else "")


def execute_run_sql(store: DataStore, args: dict[str, Any], records: list[QueryRecord]) -> tuple[str, bool]:
    """Run the tool, log it, and return (text for the model, is_error)."""
    sql = str(args.get("sql", ""))
    purpose = str(args.get("purpose", ""))
    t0 = time.perf_counter()
    try:
        df = store.run_sql(sql, max_rows=MAX_RESULT_ROWS)
        rec = QueryRecord(sql=sql, purpose=purpose, ok=True, rows=len(df),
                          seconds=time.perf_counter() - t0, result=df)
        records.append(rec)
        text = df.to_csv(index=False)
        if len(df) >= MAX_RESULT_ROWS:
            text += f"\n(truncated to {MAX_RESULT_ROWS} rows)"
        return text or "(no rows)", False
    except Exception as e:  # SQL errors go back to the model so it can fix the query
        records.append(QueryRecord(sql=sql, purpose=purpose, ok=False, rows=0,
                                   seconds=time.perf_counter() - t0, error=str(e)))
        return f"SQL error: {e}", True


# --------------------------------------------------------------------------- Anthropic

def chat_anthropic(
    store: DataStore,
    history: list[dict[str, Any]],
    user_message: str,
    model: str = "claude-opus-5",
    effort: str = "medium",
    on_status: Callable[[str], None] | None = None,
) -> tuple[ChatResult, list[dict[str, Any]]]:
    """One user turn with a manual tool loop. Returns the result and updated history.

    History holds Anthropic-format messages including tool_use / tool_result
    blocks, so later turns can refer to earlier queries.
    """
    import anthropic

    client = anthropic.Anthropic()
    messages = list(history) + [{"role": "user", "content": user_message}]
    records: list[QueryRecord] = []
    t0 = time.perf_counter()
    in_tok = out_tok = 0
    response = None

    for _ in range(MAX_TOOL_ROUNDS + 1):
        if on_status:
            on_status("Thinking...")
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=[{"type": "text", "text": build_system_prompt(store),
                     "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=[RUN_SQL_TOOL | {"strict": True}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        in_tok += response.usage.input_tokens or 0
        out_tok += response.usage.output_tokens or 0
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            answer = "The model declined this request."
            break
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            answer = "".join(b.text for b in response.content if b.type == "text").strip()
            break

        results = []
        for tu in tool_uses:
            if on_status:
                on_status(f"Running SQL: {tu.input.get('purpose', '')}")
            text, is_err = execute_run_sql(store, tu.input, records)
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": text, "is_error": is_err})
        messages.append({"role": "user", "content": results})
    else:
        answer = "Stopped after too many query rounds without a final answer."

    result = ChatResult(
        answer=answer or "(empty answer)", queries=records, provider="anthropic",
        model=response.model if response else model, seconds=time.perf_counter() - t0,
        input_tokens=in_tok, output_tokens=out_tok,
        stop_reason=response.stop_reason if response else None,
    )
    return result, messages


# --------------------------------------------------------------------------- Ollama

def _ollama_tool_spec() -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": RUN_SQL_TOOL["name"],
        "description": RUN_SQL_TOOL["description"],
        "parameters": RUN_SQL_TOOL["input_schema"],
    }}


def chat_ollama(
    store: DataStore,
    history: list[dict[str, Any]],
    user_message: str,
    model: str = "llama3.1:8b",
    host: str | None = None,
    on_status: Callable[[str], None] | None = None,
    extra_instructions: str = "",
    think: bool | None = None,
) -> tuple[ChatResult, list[dict[str, Any]]]:
    """Same loop against a local Ollama model that supports tool calling.

    extra_instructions is appended to the system prompt (the web app uses it to
    ask for a chart-ready breakdown); evals leave it empty. think=False turns off
    the reasoning phase of models that have one (qwen3), which is much faster.
    """
    import ollama

    client = ollama.Client(host=host) if host else ollama.Client()
    messages = [{"role": "system", "content": build_system_prompt(store, extra_instructions)}] + list(history) \
        + [{"role": "user", "content": user_message}]
    records: list[QueryRecord] = []
    t0 = time.perf_counter()
    in_tok = out_tok = 0
    response = None

    for _ in range(MAX_TOOL_ROUNDS + 1):
        if on_status:
            on_status("Thinking (local model)...")
        extra = {} if think is None else {"think": think}
        response = client.chat(model=model, messages=messages, tools=[_ollama_tool_spec()], **extra)
        msg = response.message
        in_tok += getattr(response, "prompt_eval_count", 0) or 0
        out_tok += getattr(response, "eval_count", 0) or 0
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": msg.tool_calls or []})
        if not msg.tool_calls:
            answer = (msg.content or "").strip()
            break
        for call in msg.tool_calls:
            args = call.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"sql": args, "purpose": ""}
            if on_status:
                on_status(f"Running SQL: {args.get('purpose', '')}")
            text, _ = execute_run_sql(store, args, records)
            messages.append({"role": "tool", "content": text, "tool_name": call.function.name})
    else:
        answer = "Stopped after too many query rounds without a final answer."

    result = ChatResult(
        answer=answer or "(empty answer)", queries=records, provider="ollama", model=model,
        seconds=time.perf_counter() - t0, input_tokens=in_tok or None, output_tokens=out_tok or None,
    )
    # Drop the system message; it is rebuilt each turn from the current schema.
    return result, messages[1:]


# --------------------------------------------------------------------------- OpenAI-compatible

def chat_openai_compatible(
    store: DataStore,
    history: list[dict[str, Any]],
    user_message: str,
    model: str,
    base_url: str,
    api_key: str = "",
    on_status: Callable[[str], None] | None = None,
    extra_instructions: str = "",
    extra_body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[ChatResult, list[dict[str, Any]]]:
    """Same loop against any OpenAI-compatible chat completions endpoint.

    Covers hosted open models (Hugging Face Inference Providers, Groq, vLLM) and
    Ollama's own /v1 endpoint, with the standard library only. extra_body is
    merged into each request, for provider options such as disabling reasoning.
    """
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = [{"role": "system", "content": build_system_prompt(store, extra_instructions)}] \
        + list(history) + [{"role": "user", "content": user_message}]
    records: list[QueryRecord] = []
    t0 = time.perf_counter()
    in_tok = out_tok = 0
    answer = ""

    for _ in range(MAX_TOOL_ROUNDS + 1):
        if on_status:
            on_status("Thinking (hosted model)...")
        body = {"model": model, "messages": messages, "tools": [_ollama_tool_spec()],
                "tool_choice": "auto", "temperature": 0, **(extra_body or {})}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:  # surface the provider's message, not a bare status
            raise RuntimeError(f"model endpoint returned {e.code}: {e.read()[:300].decode(errors='replace')}") from e
        usage = data.get("usage") or {}
        in_tok += usage.get("prompt_tokens") or 0
        out_tok += usage.get("completion_tokens") or 0
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            answer = (msg.get("content") or "").strip()
            break
        for call in calls:
            raw = call.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {"sql": raw, "purpose": ""}
            if on_status:
                on_status(f"Running SQL: {args.get('purpose', '')}")
            text, _ = execute_run_sql(store, args, records)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": text})
    else:
        answer = "Stopped after too many query rounds without a final answer."

    # Reasoning models may emit <think> blocks in content; the answer is what follows.
    if "</think>" in answer:
        answer = answer.split("</think>", 1)[1].strip()
    result = ChatResult(
        answer=answer or "(empty answer)", queries=records, provider="openai-compatible", model=model,
        seconds=time.perf_counter() - t0, input_tokens=in_tok or None, output_tokens=out_tok or None,
    )
    return result, messages[1:]
