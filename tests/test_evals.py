"""The eval harness itself: offline, no LLM, no database."""

import httpx
import pytest
from langchain_core.runnables import RunnableLambda

from evals import component, fixture_web
from evals.cases import (DRAFT_CASES, E2E_CASES, FIXTURE_TODAY, GOLD_FUND, GOLD_EXPECT, HSBC, HSBC_EXPECT, HSBC_FUND,
                         HSBC_WEB, INVESCO, INVESCO_EXPECT, INVESCO_FUND, INVESCO_WEB, REPAIR_CASES, WAHED, WAHED_EXPECT,
                         WAHED_FUND, WAHED_WEB)
from evals.graders import grade_recipe, run_recipe
from t212_mcp.config import Settings
from t212_mcp.lookthrough.graph import RepairDecision
from t212_mcp.lookthrough.recipes import Recipe, StaticHolding
from t212_mcp.lookthrough.validate import isin_checksum_ok

GOLD = Recipe(kind="static", scope="isin", issuer="Invesco", issuer_match="^Invesco", isin=GOLD_FUND.isin,
              static_holdings=[StaticHolding(name="Gold", weight_pct=100, country="Commodity")])
CANONICAL = {HSBC_FUND.isin: HSBC, WAHED_FUND.isin: WAHED, INVESCO_FUND.isin: INVESCO, GOLD_FUND.isin: GOLD}


class StubModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def with_structured_output(self, schema, method=None):
        return RunnableLambda(lambda _: self.outputs.pop(0))


@pytest.fixture
def settings():
    return Settings(_env_file=None, api_key="k")


@pytest.mark.parametrize("recipe,fund,web,expect", [
    (HSBC, HSBC_FUND, HSBC_WEB, HSBC_EXPECT), (WAHED, WAHED_FUND, WAHED_WEB, WAHED_EXPECT),
    (INVESCO, INVESCO_FUND, INVESCO_WEB, INVESCO_EXPECT), (GOLD, GOLD_FUND, fixture_web.FixtureWeb(), GOLD_EXPECT),
])
async def test_canonical_recipes_pass_their_cases(recipe, fund, web, expect):
    with fixture_web.offline(), fixture_web.use(web):
        grade = await grade_recipe(recipe, fund, expect, FIXTURE_TODAY)
    assert grade.passed, grade.failure
    assert all(grade.checks.values()), grade.checks


@pytest.mark.parametrize("case", REPAIR_CASES, ids=lambda c: c.id)
async def test_every_repair_case_starts_broken(case):
    with fixture_web.offline(), fixture_web.use(case.web):
        out = await run_recipe(case.broken, case.fund, FIXTURE_TODAY)
    assert out.failure


async def test_offline_web_blocks_unknown_urls():
    with fixture_web.offline(), fixture_web.use(HSBC_WEB):
        async with httpx.AsyncClient() as c:
            assert (await c.get("https://example.com/holdings.csv")).status_code == 404


async def test_grade_reports_failed_hard_checks():
    with fixture_web.offline(), fixture_web.use(HSBC_WEB):
        grade = await grade_recipe(HSBC, HSBC_FUND, HSBC_EXPECT.__class__(min_holdings=10_000), FIXTURE_TODAY)
    assert not grade.passed and grade.failure == "failed checks: min_holdings"


async def test_component_runner_with_stub_model(settings):
    outputs = [CANONICAL[c.fund.isin] for c in DRAFT_CASES]
    for c in REPAIR_CASES:
        if c.expect_action == "fix":
            outputs.append(RepairDecision(action="fix", reason="stub", recipe=CANONICAL[c.fund.isin]))
        else:
            outputs.append(RepairDecision(action="give_up", reason="stub"))  # wrong: should rediscover
    ids = {c.id for c in DRAFT_CASES + REPAIR_CASES}  # the stub can't drive the entity-resolution agent
    results = await component.run(settings, trials=1, case_ids=ids, concurrency=1, model=StubModel(outputs))
    passed = {r["id"]: r["trials"][0]["passed"] for r in results}
    assert passed.pop("repair_cookie_wall") is False
    assert all(passed.values()), [r for r in results if not r["trials"][0]["passed"]]


def test_e2e_cases_have_valid_isins():
    for c in E2E_CASES:
        assert isin_checksum_ok(c.fund.isin), c.id
        assert all(isin_checksum_ok(i) for i in c.expect.must_include), c.id
