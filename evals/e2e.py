"""End-to-end evals: the full per-fund graph against live issuer sites, starting from an empty registry.

Uses its own database (`<database>_eval`), wiped before every trial, so trials are independent and never touch
your real recipes. Trials run one at a time because they share that database.
"""

import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from t212_mcp import db
from t212_mcp.config import Settings
from t212_mcp.db.models import Base
from t212_mcp.lookthrough.graph import Pipeline
from t212_mcp.lookthrough.validate import validate

from .cases import E2E_CASES, E2ECase
from .graders import SOFT_CHECKS, holdings_checks, recipe_checks

log = logging.getLogger("evals.e2e")


def eval_database(database_url: str) -> str:
    url = make_url(database_url)
    url = url.set(database=f"{url.database}_eval")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": url.database}):
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    return url.render_as_string(hide_password=False)


def wipe(database_url: str) -> None:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with db.engine(database_url).begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


async def run_trial(case: E2ECase, settings: Settings) -> dict:
    wipe(settings.database_url)
    p = Pipeline(settings)
    t0 = time.monotonic()
    try:
        out = await p.fund_graph().ainvoke({"fund": case.fund.as_dict(), "allow_agent": True, "siblings": []},
                                           config={"recursion_limit": 100})
    except Exception as e:
        return {"passed": False, "failure": f"graph error: {type(e).__name__}: {e}", "tokens": 0,
                "seconds": round(time.monotonic() - t0, 1)}
    trial = {"status": out.get("status"), "detail": out.get("detail", ""), "tokens": out.get("tokens", 0),
             "attempts": out.get("attempts", 0), "seconds": round(time.monotonic() - t0, 1)}
    recipes = p.registry.all()
    fh = p.store.last_good(case.fund.isin)
    if out.get("status") != "ok" or fh is None or not recipes:
        return trial | {"passed": False, "failure": out.get("error") or out.get("detail") or "no recipe saved"}
    recipe = recipes[0]
    report = validate(fh, case.fund.isin, case.fund.ticker, case.fund.name)
    checks = recipe_checks(recipe, case.expect) | holdings_checks(fh, case.expect)
    failed = [k for k, v in checks.items() if not v and k not in SOFT_CHECKS]
    return trial | {
        "passed": report.ok and not failed,
        "checks": checks,
        "failure": "; ".join(report.errors) or (f"failed checks: {', '.join(failed)}" if failed else None),
        "stats": report.stats,
        "recipe": recipe.model_dump(exclude_none=True, exclude={"discovered_at"}),
    }


async def run(settings: Settings, trials: int, case_ids: set[str] | None = None) -> list[dict]:
    settings = settings.model_copy(update={"database_url": eval_database(settings.database_url),
                                           "max_agent_runs_per_day": 1_000_000})
    out = []
    for case in [c for c in E2E_CASES if not case_ids or c.id in case_ids]:
        results = []
        for n in range(trials):
            log.info("%s: trial %d/%d", case.id, n + 1, trials)
            results.append(await run_trial(case, settings))
            log.info("%s: %s", case.id, "pass" if results[-1]["passed"] else f"FAIL {results[-1].get('failure', '')[:160]}")
        out.append({"id": case.id, "step": "e2e", "trials": results})
    return out
