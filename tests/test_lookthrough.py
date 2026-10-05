import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from langchain_core.runnables import RunnableLambda

from t212_mcp import db
from t212_mcp.config import Settings
from t212_mcp.lookthrough import extractors
from t212_mcp.lookthrough.exposure import compute_exposure
from t212_mcp.lookthrough.extractors import FundHoldings, Holding, _parse_payload, extract
from t212_mcp.lookthrough.graph import Pipeline, RepairDecision
from t212_mcp.lookthrough.recipes import ColumnMap, Recipe, Registry
from t212_mcp.lookthrough.store import Store
from t212_mcp.lookthrough.validate import isin_checksum_ok, validate

FIX = Path(__file__).parent / "fixtures"
HSBC_ISIN, WAHED_ISIN, INVESCO_ISIN = "IE0009BC6K22", "IE00073MUWT4", "IE000UOXRAM8"
HSBC_URL = "https://www.assetmanagement.hsbc.co.uk/api/v1/download/document/{isin_lower}/gb/en/holdings"

HSBC = Recipe(
    kind="http_file", scope="issuer", issuer="HSBC", issuer_match=r"^HSBC\b", url=HSBC_URL, file_format="xls",
    header_contains="ISIN", as_of="cell_right_of:Date",
    columns=ColumnMap(name="SecurityName", weight="Weighting", isin="ISIN", country="Country"),
)
WAHED = Recipe(
    kind="scrape_link", scope="issuer", issuer="Wahed", issuer_match=r"^Wahed\b", page_url="https://www.wahed.com/uk/{ticker_lower}",
    link_pattern=r"docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}", header_contains="ISIN", as_of="column:Date",
    weight_scale="fraction", columns=ColumnMap(name="SecurityName", weight="Weight", isin="ISIN"),
)
INVESCO = Recipe(
    kind="browser_json", scope="issuer", issuer="Invesco", issuer_match=r"^Invesco\b", page_url="https://www.invesco.com/uk/en/",
    url="https://dng-api.invesco.com/x/{isin}/holdings", file_format="json", json_holdings_path="holdings",
    as_of="json:effectiveDate", columns=ColumnMap(name="name", weight="weight", isin="isin"),
)
FUND = {"isin": HSBC_ISIN, "name": "HSBC MSCI Emerging Markets Islamic Screened Capped (Acc)", "ticker": "HIES"}
TODAY = date(2026, 10, 5)


@pytest.fixture
def settings(database_url):
    return Settings(_env_file=None, api_key="k", database_url=database_url)


def hsbc_route():
    return respx.get(HSBC_URL.format(isin_lower=HSBC_ISIN.lower())).respond(content=(FIX / "hsbc_IE0009BC6K22.xls").read_bytes())


# ------------------------------------------------------------------ extractors


@respx.mock
async def test_http_file_xls_with_placeholders():
    hsbc_route()
    fh = await extract(HSBC, HSBC_ISIN, "HIES")
    assert fh.as_of == "2026-10-01"
    assert fh.holdings[0].name == "SK hynix Inc" and fh.holdings[0].country == "South Korea"
    assert round(sum(h.weight_pct for h in fh.holdings)) == 100


@respx.mock
async def test_scrape_link_finds_google_sheet_and_exports_xlsx():
    link = (FIX / "wahed_link.txt").read_text().strip()
    respx.get("https://www.wahed.com/uk/djiw").respond(html=f'<a href="{link}">Holdings</a>')
    respx.get(url__regex=r"https://docs\.google\.com/spreadsheets/d/.+/export\?format=xlsx").respond(
        content=(FIX / "wahed_IE00073MUWT4.xlsx").read_bytes()
    )
    fh = await extract(WAHED, WAHED_ISIN, "DJIW")
    assert fh.holdings[0].isin == "US8740391003"
    assert 99 < sum(h.weight_pct for h in fh.holdings) < 101  # fractions scaled to percent
    assert "DJIW" in fh.source_excerpt


def test_json_payload_parsing():
    holdings, excerpt, as_of = _parse_payload((FIX / "invesco_IE000UOXRAM8_holdings.json").read_bytes(), "json", INVESCO)
    assert len(holdings) > 1000 and holdings[0].isin == "US67066G1040" and as_of == "2026-10-02"


@respx.mock
async def test_html_instead_of_file_is_an_error():
    respx.get(HSBC_URL.format(isin_lower=HSBC_ISIN.lower())).respond(html="<html>Please accept cookies</html>")
    with pytest.raises(extractors.ExtractionError, match="HTML page"):
        await extract(HSBC, HSBC_ISIN, "HIES")


@respx.mock
async def test_extraction_errors_show_what_the_source_contains():
    hsbc_route()
    wrong_col = HSBC.model_copy(update={"columns": ColumnMap(name="SecurityName", weight="Weight")})
    with pytest.raises(extractors.ExtractionError) as e:
        await extract(wrong_col, HSBC_ISIN, "HIES")
    assert "weight='Weight' (NOT FOUND)" in str(e.value) and "Weighting" in str(e.value)

    with pytest.raises(extractors.ExtractionError) as e:
        await extract(HSBC.model_copy(update={"header_contains": "Ticker"}), HSBC_ISIN, "HIES")
    assert "row 6: ISIN | CUSIP | SecurityName" in str(e.value)


def test_json_path_error_lists_usable_paths():
    bad = INVESCO.model_copy(update={"json_holdings_path": "data.holdings"})
    with pytest.raises(extractors.ExtractionError, match=r"'holdings' \(\d+ items; keys: name, isin"):
        _parse_payload((FIX / "invesco_IE000UOXRAM8_holdings.json").read_bytes(), "json", bad)


# ------------------------------------------------------------------ validator


def fund_holdings(weights, isins=None, as_of="2026-10-01", url="https://x/ie0009bc6k22"):
    isins = isins or ["US67066G1040"] * len(weights)
    return FundHoldings(isin=HSBC_ISIN, as_of=as_of, coverage="full", source_url=url,
                        holdings=[Holding(name=f"S{i}", weight_pct=w, isin=isins[i]) for i, w in enumerate(weights)])


def test_isin_checksum():
    assert isin_checksum_ok("US67066G1040") and isin_checksum_ok("IE0009BC6K22")
    assert not isin_checksum_ok("US67066G1041")


def test_validator_flags_fraction_weights_old_data_and_wrong_fund():
    rep = validate(fund_holdings([0.1] * 10), HSBC_ISIN, "HIES", FUND["name"], today=TODAY)
    assert any("fraction" in e for e in rep.errors)
    rep = validate(fund_holdings([10] * 10, as_of="2026-08-01"), HSBC_ISIN, "HIES", FUND["name"], today=TODAY)
    assert any("days old" in e for e in rep.errors)
    rep = validate(fund_holdings([10] * 10, url="https://other.example/fund.xls"), HSBC_ISIN, "HIES", FUND["name"], today=TODAY)
    assert any("belongs to this fund" in e for e in rep.errors)
    assert validate(fund_holdings([10] * 10), HSBC_ISIN, "HIES", FUND["name"], today=TODAY).ok


def test_validator_rejects_big_turnover():
    prev = fund_holdings([10] * 10, isins=["US67066G1040"] * 10)
    cur = fund_holdings([10] * 10, isins=["US0378331005"] * 10)
    rep = validate(cur, HSBC_ISIN, "HIES", FUND["name"], previous=prev, today=TODAY)
    assert any("turnover" in e for e in rep.errors)


# ------------------------------------------------------------------ exposure


def test_exposure_joins_direct_and_fund_holdings_by_isin():
    positions = [
        {"ticker": "NVDA_US_EQ", "name": "Nvidia", "isin": "US67066G1040", "value": 200.0},
        {"ticker": "IGDA", "name": "Invesco Global", "isin": INVESCO_ISIN, "value": 1000.0},
    ]
    fh = FundHoldings(isin=INVESCO_ISIN, as_of="2026-10-02", coverage="full", source_url="s",
                      holdings=[Holding(name="NVIDIA CORP USD0.001", weight_pct=10, isin="US67066G1040"),
                                Holding(name="APPLE INC", weight_pct=89, isin="US0378331005"),
                                Holding(name="USD Cash", weight_pct=1)],
                      countries={"United States": 95, "Japan": 5})
    exp = compute_exposure(positions, {INVESCO_ISIN}, {INVESCO_ISIN: fh}, {INVESCO_ISIN: "ok"})
    nvda = next(s for s in exp["all_securities"] if s["isin"] == "US67066G1040")
    assert nvda["value"] == 300.0 and nvda["direct"] == 200.0 and nvda["via_funds"] == {"IGDA": 100.0}
    assert nvda["name"] == "Nvidia"
    assert exp["cash_inside_funds"] == 10.0
    countries = {c["name"]: c["value"] for c in exp["countries"]}
    assert countries["United States"] == 200 + 950 and countries["Japan"] == 50


# ------------------------------------------------------------------ graph routing (stub LLM)


class StubModel:
    """Returns queued structured outputs; stands in for any provider."""

    def __init__(self, outputs):
        self.outputs = list(outputs)

    def with_structured_output(self, schema, method=None):
        return RunnableLambda(lambda _: self.outputs.pop(0))


def broken(recipe: Recipe) -> Recipe:
    return recipe.model_copy(update={"columns": ColumnMap(name="SecurityName", weight="Weight", isin="ISIN")})


@respx.mock
async def test_registry_recipe_passes_without_llm(settings):
    hsbc_route()
    Registry(settings.database_url).save(HSBC, "test")
    p = Pipeline(settings, model=StubModel([]))
    out = await p.fund_graph().ainvoke({"fund": FUND, "allow_agent": True, "siblings": []})
    assert out["status"] == "ok" and out["origin"] == "registry"
    assert Store(settings.database_url).last_good(HSBC_ISIN) is not None


@respx.mock
async def test_broken_registry_recipe_is_repaired_and_saved(settings):
    hsbc_route()
    Registry(settings.database_url).save(broken(HSBC), "test")
    p = Pipeline(settings, model=StubModel([RepairDecision(action="fix", reason="wrong weight column", recipe=HSBC)]))
    out = await p.fund_graph().ainvoke({"fund": FUND, "allow_agent": True, "siblings": []})
    assert out["status"] == "ok", out
    saved = Registry(settings.database_url).all()
    assert len(saved) == 1 and saved[0].columns.weight == "Weighting" and saved[0].scope == "issuer"


@respx.mock
async def test_agent_gives_up_and_fund_is_unresolved(settings):
    hsbc_route()
    Registry(settings.database_url).save(broken(HSBC), "test")
    p = Pipeline(settings, model=StubModel([RepairDecision(action="give_up", reason="not published")]))
    out = await p.fund_graph().ainvoke({"fund": FUND, "allow_agent": True, "siblings": []})
    assert out["status"] == "unresolved" and "gave up" in out["detail"]


@respx.mock
async def test_repair_attempts_are_capped(settings):
    hsbc_route()
    Registry(settings.database_url).save(broken(HSBC), "test")
    fixes = [RepairDecision(action="fix", reason="try", recipe=broken(HSBC)) for _ in range(10)]
    p = Pipeline(settings, model=StubModel(fixes))
    out = await p.fund_graph().ainvoke({"fund": FUND, "allow_agent": True, "siblings": []})
    assert out["status"] == "unresolved"
    assert out["attempts"] <= settings.max_repair_attempts + 1


@respx.mock
async def test_agent_disabled_means_no_llm_calls(settings):
    hsbc_route()
    Registry(settings.database_url).save(broken(HSBC), "test")
    p = Pipeline(settings, model=StubModel([]))  # any LLM call would pop from an empty list and fail
    out = await p.fund_graph().ainvoke({"fund": FUND, "allow_agent": False, "siblings": []})
    assert out["status"] == "unresolved"


# ------------------------------------------------------------------ storage


def test_registry_keeps_superseded_recipes(settings):
    reg = Registry(settings.database_url)
    first = reg.save(broken(HSBC), "test")
    second = reg.save(HSBC, "test")
    assert [r.id for r in reg.all()] == [second.id]  # one active recipe per issuer
    with db.sessions(settings.database_url)() as s:
        old = s.get(db.models.Recipe, first.id)
        assert old.superseded_by == second.id and old.superseded_at is not None


def test_store_keeps_one_snapshot_per_publication(settings):
    store = Store(settings.database_url)
    store.save_fund(HSBC_ISIN, status="ok", holdings=fund_holdings([60, 40]), error=None)
    store.save_fund(HSBC_ISIN, status="ok", holdings=fund_holdings([50, 50]))  # same as_of: replaces
    store.save_fund(HSBC_ISIN, status="stale", error="HTTP 500")  # status only: holdings kept
    fh = store.last_good(HSBC_ISIN)
    assert [h.weight_pct for h in fh.holdings] == [50, 50]
    assert store.load_fund(HSBC_ISIN)["status"] == "stale" and store.load_fund(HSBC_ISIN)["error"] == "HTTP 500"
    with db.sessions(settings.database_url)() as s:
        assert s.query(db.models.HoldingsSnapshot).count() == 1


def test_agent_run_limit_counts_runs_not_events(settings):
    store = Store(settings.database_url)
    store.log_run({"isin": HSBC_ISIN, "event": "agent_start", "model": "m"})
    store.log_run({"isin": HSBC_ISIN, "event": "recipe_saved", "recipe_id": "r", "tokens": 10})
    assert store.agent_runs_today() == 1
    assert [r["event"] for r in store.runs(limit=1)] == ["recipe_saved"]
