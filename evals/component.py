"""Component evals: call the pipeline's draft and repair nodes with fixed inputs, grade against fixture files."""

import asyncio
import time

from t212_mcp.config import Settings
from t212_mcp.lookthrough.graph import Pipeline
from t212_mcp.lookthrough.recipes import Recipe

from . import fixture_web
from .cases import DRAFT_CASES, FIXTURE_TODAY, REPAIR_CASES, DraftCase, RepairCase
from .graders import grade_recipe, run_recipe


class _Unused:
    """Stands in for Store/Registry: draft and repair never touch the database."""


def pipeline(settings: Settings, model=None) -> Pipeline:
    return Pipeline(settings, store=_Unused(), registry=_Unused(), model=model)


async def run_draft(case: DraftCase, p: Pipeline) -> dict:
    state = {"fund": case.fund.as_dict(), "findings": case.findings, "history": [], "attempts": 0}
    t0 = time.monotonic()
    update = await p.draft(state)
    trial = {"tokens": update.get("tokens", 0), "seconds": round(time.monotonic() - t0, 1)}
    if not update.get("recipe"):
        return trial | {"passed": False, "failure": update.get("error") or "no recipe returned"}
    recipe = Recipe.model_validate(update["recipe"])
    with fixture_web.use(case.web):
        grade = await grade_recipe(recipe, case.fund, case.expect, FIXTURE_TODAY)
    return trial | {"passed": grade.passed, "checks": grade.checks, "failure": grade.failure, "stats": grade.stats,
                    "recipe": recipe.model_dump(exclude_none=True)}


async def run_repair(case: RepairCase, p: Pipeline) -> dict:
    with fixture_web.use(case.web):
        broken = await run_recipe(case.broken, case.fund, FIXTURE_TODAY)
    if broken.failure is None:
        raise AssertionError(f"{case.id}: the broken recipe unexpectedly passes")
    state = {"fund": case.fund.as_dict(), "recipe": case.broken.model_dump(), "error": broken.failure,
             "report": broken.report.model_dump() if broken.report else None, "findings": ""}
    t0 = time.monotonic()
    update = await p.repair(state)
    action, reason = update.get("repair_action"), update.get("detail", "")
    trial = {"tokens": update.get("tokens", 0), "seconds": round(time.monotonic() - t0, 1), "action": action,
             "reason": reason, "shown_failure": broken.failure}
    if reason.startswith("repair call failed"):  # the node turns LLM errors into 'rediscover'
        return trial | {"passed": False, "failure": reason}
    if action != case.expect_action:
        return trial | {"passed": False, "failure": f"chose {action}, expected {case.expect_action}"}
    if action != "fix":
        return trial | {"passed": True}
    recipe = Recipe.model_validate(update["recipe"])
    with fixture_web.use(case.web):
        grade = await grade_recipe(recipe, case.fund, case.expect, FIXTURE_TODAY)
    return trial | {"passed": grade.passed, "checks": grade.checks, "failure": grade.failure, "stats": grade.stats,
                    "recipe": recipe.model_dump(exclude_none=True)}


async def run(settings: Settings, trials: int, case_ids: set[str] | None = None, concurrency: int = 4,
              model=None) -> list[dict]:
    """One result per case: {id, step, trials: [...]}."""
    p = pipeline(settings, model)
    cases = [("draft", c, run_draft) for c in DRAFT_CASES] + [("repair", c, run_repair) for c in REPAIR_CASES]
    cases = [c for c in cases if not case_ids or c[1].id in case_ids]
    sem = asyncio.Semaphore(concurrency)

    async def one(case, fn, n):
        async with sem:
            try:
                return await fn(case, p)
            except AssertionError:
                raise
            except Exception as e:
                return {"passed": False, "failure": f"harness error: {type(e).__name__}: {e}", "tokens": 0, "seconds": 0}

    with fixture_web.offline():
        results = await asyncio.gather(*(one(c, fn, n) for _, c, fn in cases for n in range(trials)))
    out, i = [], 0
    for step, case, _ in cases:
        out.append({"id": case.id, "step": step, "trials": results[i:i + trials]})
        i += trials
    return out
