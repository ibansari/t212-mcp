"""Deterministic grading: run a recipe, validate the holdings, and check them against a case's expectations."""

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from t212_mcp.lookthrough.extractors import ExtractionError, FundHoldings, extract
from t212_mcp.lookthrough.recipes import Recipe
from t212_mcp.lookthrough.validate import ValidationReport, validate

# Checks reported but not required for a pass.
SOFT_CHECKS = {"placeholders", "source_host"}


@dataclass(frozen=True)
class Fund:
    isin: str
    name: str
    ticker: str

    def as_dict(self) -> dict:
        return {"isin": self.isin, "name": self.name, "ticker": self.ticker}


@dataclass(frozen=True)
class Expect:
    min_holdings: int | None = None
    must_include: tuple[str, ...] = ()  # ISINs that must be among the holdings
    top_isin: str | None = None  # ISIN of the largest holding
    kinds: tuple[str, ...] = ()  # acceptable recipe kinds
    source_hosts: tuple[str, ...] = ()  # soft: source URL host should end with one of these
    portable: bool = False  # soft: URLs should use {isin}/{ticker} placeholders


@dataclass
class Outcome:
    holdings: FundHoldings | None
    report: ValidationReport | None
    failure: str | None  # extraction error or validation errors, as the pipeline would report them


async def run_recipe(recipe: Recipe, fund: Fund, today: date | None = None) -> Outcome:
    try:
        fh = await extract(recipe, fund.isin, fund.ticker)
    except ExtractionError as e:
        return Outcome(None, None, str(e))
    except Exception as e:
        return Outcome(None, None, f"{type(e).__name__}: {e}")
    report = validate(fh, fund.isin, fund.ticker, fund.name, today=today)
    return Outcome(fh, report, None if report.ok else "; ".join(report.errors))


def recipe_checks(recipe: Recipe, expect: Expect) -> dict[str, bool]:
    checks = {}
    if expect.kinds:
        checks["kind"] = recipe.kind in expect.kinds
    if expect.portable:
        checks["placeholders"] = any("{" in (u or "") for u in (recipe.url, recipe.page_url, recipe.link_pattern))
    return checks


def holdings_checks(fh: FundHoldings, expect: Expect) -> dict[str, bool]:
    checks = {}
    isins = {h.isin for h in fh.holdings if h.isin}
    if expect.min_holdings is not None:
        checks["min_holdings"] = len(fh.holdings) >= expect.min_holdings
    if expect.must_include:
        checks["must_include"] = all(i in isins for i in expect.must_include)
    if expect.top_isin and fh.holdings:
        checks["top_holding"] = max(fh.holdings, key=lambda h: h.weight_pct).isin == expect.top_isin
    if expect.source_hosts and fh.source_url != "static":
        host = urlparse(fh.source_url).hostname or ""
        checks["source_host"] = any(host == h or host.endswith("." + h) for h in expect.source_hosts)
    return checks


@dataclass
class Grade:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failure: str | None = None
    stats: dict | None = None


async def grade_recipe(recipe: Recipe, fund: Fund, expect: Expect, today: date | None = None) -> Grade:
    """Pass = the recipe extracts holdings that validate and meet every hard expectation."""
    out = await run_recipe(recipe, fund, today)
    checks = recipe_checks(recipe, expect)
    if out.holdings is not None:
        checks |= holdings_checks(out.holdings, expect)
    hard_ok = all(v for k, v in checks.items() if k not in SOFT_CHECKS)
    failure = out.failure or (None if hard_ok else "failed checks: " + ", ".join(
        k for k, v in checks.items() if not v and k not in SOFT_CHECKS))
    return Grade(passed=out.failure is None and hard_ok, checks=checks, failure=failure,
                 stats=out.report.stats if out.report else None)
