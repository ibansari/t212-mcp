"""Agent observability without a third-party service: one log line per agent step, and the full run tree (model
calls, tool calls, inputs/outputs, tokens, timings, errors) stored in Postgres, LangSmith-style.

Only the agent steps (discover, draft, repair) are traced. They never see positions or portfolio values, so
traces hold prompts, fund names and public web content only."""

import json
import logging
from collections import Counter
from typing import Any

from langchain_core.tracers.base import BaseTracer
from langchain_core.tracers.schemas import Run

log = logging.getLogger("t212_mcp.agent")

MAX_TEXT = 4000  # per stored input/output
MAX_LOG = 160  # per log line fragment


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=str)
        except (TypeError, ValueError):
            value = str(value)
    value = " ".join(value.split()) if limit <= MAX_LOG else value
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _tokens(outputs: dict | None) -> int:
    """Total tokens reported anywhere in a model run's outputs (usage_metadata or llm_output.token_usage)."""
    total = 0

    def walk(o):
        nonlocal total
        if isinstance(o, dict):
            usage = o.get("usage_metadata") or o.get("token_usage")
            if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
                total += usage["total_tokens"]
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(outputs or {})
    return total


def _tool_calls(outputs: dict | None) -> list[str]:
    names: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for call in o.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("name"):
                    names.append(call["name"])
            for k, v in o.items():
                if k != "tool_calls":
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(outputs or {})
    return list(dict.fromkeys(names))


def _last_message(inputs: dict) -> Any:
    """A model call's input is the whole conversation so far; keep only the newest message."""
    messages = inputs.get("messages")
    while isinstance(messages, list) and messages and isinstance(messages[-1], list):
        messages = messages[-1]
    return messages[-1] if isinstance(messages, list) and messages else inputs


def _first_line(value: Any) -> str:
    text = value.get("output", value) if isinstance(value, dict) else value
    text = getattr(text, "content", text)
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip() and "untrusted_web_content" not in ln]
    return lines[0] if lines else ""


def node(run: Run) -> dict:
    is_model = run.run_type in ("llm", "chat_model")
    out = {
        "type": run.run_type,
        "name": run.name,
        "ms": int((run.end_time - run.start_time).total_seconds() * 1000) if run.end_time else None,
        "input": _text(_last_message(run.inputs) if is_model else run.inputs, MAX_TEXT),
        "output": _text(run.outputs, MAX_TEXT),
        "children": [node(c) for c in sorted(run.child_runs, key=lambda c: c.start_time)],
    }
    if is_model:
        out["tokens"] = _tokens(run.outputs)
    if run.error:
        out["error"] = _text(run.error.strip().splitlines()[-1], 500)
    return out


def summarize(tree: dict) -> dict:
    models, tools, tokens = 0, Counter(), 0

    def walk(n):
        nonlocal models, tokens
        if n["type"] in ("llm", "chat_model"):
            models += 1
            tokens += n.get("tokens", 0)
        elif n["type"] == "tool":
            tools[n["name"]] += 1
        for c in n["children"]:
            walk(c)

    walk(tree)
    return {"model_calls": models, "tools": dict(tools), "tokens": tokens}


class AgentTracer(BaseTracer):
    """Logs each model and tool step as it happens and keeps the finished run tree for one agent step."""

    run_inline = True  # keep step order and the token count deterministic

    def __init__(self, ticker: str, step: str, start_tokens: int = 0):
        super().__init__()
        self.ticker, self.step = ticker, step
        self.total_tokens = start_tokens
        self.steps = 0
        self.trees: list[dict] = []

    def _label(self) -> str:
        return f"{self.ticker} {self.step}"

    def _on_llm_end(self, run: Run) -> None:
        tokens = _tokens(run.outputs)
        self.total_tokens += tokens
        calls = _tool_calls(run.outputs)
        log.info("%s model %s tok (%s total)%s", self._label(), f"{tokens:,}", f"{self.total_tokens:,}",
                 f" → {', '.join(calls)}" if calls else " → answer")


    def _on_tool_start(self, run: Run) -> None:
        self.steps += 1
        args = run.inputs.get("input", run.inputs)
        if isinstance(args, dict) and len(args) == 1:
            args = next(iter(args.values()))
        log.info("%s step %d %s %s", self._label(), self.steps, run.name, _text(args, MAX_LOG))

    def _on_tool_end(self, run: Run) -> None:
        log.info("%s   → %s", self._label(), _text(_first_line(run.outputs), MAX_LOG))

    def _on_tool_error(self, run: Run) -> None:
        log.info("%s   → tool error: %s", self._label(), _text(run.error, MAX_LOG))

    def _persist_run(self, run: Run) -> None:
        tree = node(run)
        self.trees.append(tree)
        s = summarize(tree)
        log.info("%s done: %d model calls, tools %s, %s tok%s", self._label(), s["model_calls"],
                 s["tools"] or "none", f"{s['tokens']:,}", f", error: {tree['error']}" if tree.get("error") else "")
