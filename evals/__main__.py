import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from t212_mcp.config import Settings

from . import report
from .cases import DRAFT_CASES, E2E_CASES, REPAIR_CASES, RESOLVE_CASES


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals", description="Evals for the look-through agent")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, trials, help_ in (("component", 3, "draft/repair steps against fixture files (no live web)"),
                                ("e2e", 1, "full agent against live issuer sites")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--trials", type=int, default=trials, help=f"runs per case (default {trials})")
        p.add_argument("--model", help="OpenAI model (default: T212_LLM_MODEL)")
        p.add_argument("--reasoning-effort", help="e.g. low, medium, high (default: T212_REASONING_EFFORT)")
        p.add_argument("--case", action="append", dest="cases", metavar="ID", help="only these cases (repeatable)")
        p.add_argument("--fail-under", type=float, help="exit 1 if the pass rate is below this (0-1)")
        if name == "component":
            p.add_argument("--concurrency", type=int, default=4)
    sub.add_parser("list", help="list cases")
    sub.add_parser("report", help="compare all saved runs")
    args = parser.parse_args()

    if args.command == "list":
        for step, cases in (("draft", DRAFT_CASES), ("repair", REPAIR_CASES), ("e2e", E2E_CASES)):
            for c in cases:
                print(f"{step:7} {c.id:30} {c.fund.ticker:6} {c.fund.name}")
        for c in RESOLVE_CASES:
            print(f"{'resolve':7} {c.id:40} {len(c.securities)} securities")
        return
    if args.command == "report":
        print(report.compare())
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "primp", "ddgs", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = Settings()
    if settings.openai_api_key is None and not os.environ.get("OPENAI_API_KEY"):
        parser.error("set OPENAI_API_KEY in .env or the environment")
    if args.model:
        settings = settings.model_copy(update={"llm_model": args.model})
    if args.reasoning_effort is not None:  # "" means the model's default
        settings = settings.model_copy(update={"reasoning_effort": args.reasoning_effort or None})
    case_ids = set(args.cases) if args.cases else None
    started = datetime.now()
    if args.command == "component":
        from .component import run

        cases = asyncio.run(run(settings, args.trials, case_ids, args.concurrency))
    else:
        from .e2e import run

        cases = asyncio.run(run(settings, args.trials, case_ids))
    run_settings = {k: getattr(settings, k) for k in ("reasoning_effort", "agent_token_budget", "agent_recursion_limit",
                                                       "max_repair_attempts", "search_provider")}
    model = f"{settings.llm_model}@{settings.reasoning_effort}" if settings.reasoning_effort else settings.llm_model
    result = report.build(args.command, model, run_settings, cases, started)
    path = report.save(result)
    print(report.markdown(result))
    print(f"Saved {path} and {path.with_suffix('.md').name}")
    if args.fail_under is not None and result["summary"]["pass_rate"] < args.fail_under:
        sys.exit(1)


main()
