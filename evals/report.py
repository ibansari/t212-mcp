"""Write a run to evals/results/<run_id>.json and .md, and compare runs."""

import json
import subprocess
from datetime import datetime
from pathlib import Path

RESULTS = Path(__file__).parent / "results"


def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip() + ("+dirty" if _dirty() else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def _dirty() -> bool:
    return bool(subprocess.run(["git", "status", "--porcelain", "--", "src"], capture_output=True, text=True).stdout)


def summarize(cases: list[dict]) -> dict:
    trials = [t for c in cases for t in c["trials"]]
    n = len(trials) or 1
    return {
        "cases": len(cases),
        "trials": len(trials),
        "pass_rate": round(sum(t["passed"] for t in trials) / n, 3),
        "cases_always_pass": sum(all(t["passed"] for t in c["trials"]) for c in cases),
        "avg_tokens": round(sum(t.get("tokens", 0) for t in trials) / n),
        "avg_seconds": round(sum(t.get("seconds", 0) for t in trials) / n, 1),
    }


def build(tier: str, model: str, settings: dict, cases: list[dict], started: datetime) -> dict:
    safe_model = "".join(ch if ch.isalnum() or ch in "-." else "_" for ch in model)
    return {
        "run_id": f"{started:%Y%m%d-%H%M%S}-{tier}-{safe_model}",
        "tier": tier,
        "model": model,
        "started_at": started.isoformat(timespec="seconds"),
        "git": git_sha(),
        "settings": settings,
        "summary": summarize(cases),
        "cases": cases,
    }


def markdown(run: dict) -> str:
    s = run["summary"]
    lines = [
        f"# Eval run {run['run_id']}",
        "",
        f"- Tier: **{run['tier']}** · Model: **{run['model']}** · Git: `{run['git']}`",
        f"- Pass rate: **{s['pass_rate']:.0%}** ({s['trials']} trials, {s['cases_always_pass']}/{s['cases']} cases pass every trial)",
        f"- Average per trial: {s['avg_tokens']:,} tokens, {s['avg_seconds']} s",
        "",
        "| Case | Step | Passed | Avg tokens | Avg s | First failure |",
        "|---|---|---|---|---|---|",
    ]
    for c in run["cases"]:
        t = c["trials"]
        passed = sum(x["passed"] for x in t)
        fail = next((x.get("failure") for x in t if not x["passed"]), None) or ""
        fail = fail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{c['id']}` | {c['step']} | {passed}/{len(t)} | {sum(x.get('tokens', 0) for x in t) // len(t):,} "
                     f"| {sum(x.get('seconds', 0) for x in t) / len(t):.1f} | {fail[:140]} |")
    return "\n".join(lines) + "\n"


def save(run: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{run['run_id']}.json"
    path.write_text(json.dumps(run, indent=1, default=str))
    path.with_suffix(".md").write_text(markdown(run))
    return path


def compare() -> str:
    runs = [json.loads(p.read_text()) for p in sorted(RESULTS.glob("*.json"))]
    if not runs:
        return "No eval runs yet."
    lines = ["| Run | Tier | Model | Git | Pass rate | Cases always pass | Tokens/trial | s/trial |",
             "|---|---|---|---|---|---|---|---|"]
    for r in runs:
        s = r["summary"]
        lines.append(f"| {r['run_id']} | {r['tier']} | {r['model']} | {r['git']} | {s['pass_rate']:.0%} "
                     f"| {s['cases_always_pass']}/{s['cases']} | {s['avg_tokens']:,} | {s['avg_seconds']} |")
    return "\n".join(lines)
