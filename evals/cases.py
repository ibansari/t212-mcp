"""Eval cases.

Component cases replay the issuer files in tests/fixtures, so their expectations are exact. E2E cases run against
live issuer sites, so they check invariants that survive holdings changes (size, mega-cap constituents, source).
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from t212_mcp.lookthrough.recipes import ColumnMap, Recipe

from .fixture_web import FixtureWeb, Page
from .graders import Expect, Fund

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
FIXTURE_TODAY = date(2026, 10, 5)  # fixtures are dated 2026-10-01/02; validation staleness is measured from here

# Large constituents used as e2e invariants.
APPLE, MICROSOFT, NVIDIA = "US0378331005", "US5949181045", "US67066G1040"
TSMC, TSMC_ADR = "TW0002330008", "US8740391003"
SAMSUNG, SK_HYNIX = "KR7005930003", "KR7000660001"


# ---------------------------------------------------------------- fixture funds

HSBC_FUND = Fund("IE0009BC6K22", "HSBC MSCI Emerging Markets Islamic Screened Capped UCITS ETF", "HIES")
WAHED_FUND = Fund("IE00073MUWT4", "Wahed Dow Jones Islamic World ETF", "DJIW")
INVESCO_FUND = Fund("IE000UOXRAM8", "Invesco UCITS ETF", "IGDA")
GOLD_FUND = Fund("IE00B579F325", "Invesco Physical Gold ETC", "SGLD")

HSBC_URL = "https://www.assetmanagement.hsbc.co.uk/api/v1/download/document/{isin_lower}/gb/en/holdings"
WAHED_SHEET = (FIX / "wahed_link.txt").read_text().strip()

HSBC = Recipe(
    kind="http_file", scope="issuer", issuer="HSBC", issuer_match=r"^HSBC\b", url=HSBC_URL, file_format="xls",
    header_contains="ISIN", as_of="cell_right_of:Date",
    columns=ColumnMap(name="SecurityName", weight="Weighting", isin="ISIN", country="Country"),
)
WAHED = Recipe(
    kind="scrape_link", scope="issuer", issuer="Wahed", issuer_match=r"^Wahed\b",
    page_url="https://www.wahed.com/uk/{ticker_lower}", link_pattern=r"docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}",
    header_contains="ISIN", as_of="column:Date", weight_scale="fraction",
    columns=ColumnMap(name="SecurityName", weight="Weight", isin="ISIN"),
)
INVESCO = Recipe(
    kind="browser_json", scope="issuer", issuer="Invesco", issuer_match=r"^Invesco\b",
    page_url="https://www.invesco.com/uk/en/", url="https://dng-api.invesco.com/x/{isin}/holdings",
    file_format="json", json_holdings_path="holdings", as_of="json:effectiveDate",
    columns=ColumnMap(name="name", weight="weight", isin="isin"),
)

HSBC_WEB = FixtureWeb((
    Page(r"assetmanagement\.hsbc\.co\.uk/api/v1/download/document/ie0009bc6k22/", (FIX / "hsbc_IE0009BC6K22.xls").read_bytes()),
))
WAHED_WEB = FixtureWeb((
    Page(r"wahed\.com/uk/djiw/?$", f'<html><a href="{WAHED_SHEET}">Holdings</a></html>'.encode(), content_type="text/html"),
    Page(r"docs\.google\.com/spreadsheets/d/1Zjd3FTnt7AkY1lQontz0n7bCatAodG_d/export", (FIX / "wahed_IE00073MUWT4.xlsx").read_bytes()),
))
INVESCO_WEB = FixtureWeb((
    Page(r"dng-api\.invesco\.com/x/IE000UOXRAM8/holdings", (FIX / "invesco_IE000UOXRAM8_holdings.json").read_bytes(),
         content_type="application/json"),
))
COOKIE_WALL_WEB = FixtureWeb((
    Page(r"assetmanagement\.hsbc\.co\.uk/", b"<html><body><h1>Please accept cookies to continue</h1></body></html>",
         content_type="text/html"),
))

HSBC_EXPECT = Expect(min_holdings=300, top_isin=SK_HYNIX, portable=True)
WAHED_EXPECT = Expect(min_holdings=80, top_isin=TSMC_ADR, portable=True)
INVESCO_EXPECT = Expect(min_holdings=1000, top_isin=NVIDIA, kinds=("browser_json",), portable=True)
GOLD_EXPECT = Expect(kinds=("static",))


# ---------------------------------------------------------------- component: draft


@dataclass(frozen=True)
class DraftCase:
    id: str
    fund: Fund
    findings: str  # the research report the discover step would hand over
    web: FixtureWeb
    expect: Expect


DRAFT_CASES = [
    DraftCase("draft_hsbc_xls", HSBC_FUND, """\
Issuer: HSBC Asset Management.
Best source: https://www.assetmanagement.hsbc.co.uk/api/v1/download/document/ie0009bc6k22/gb/en/holdings
Fetch: direct download, no browser needed. The lowercase ISIN is in the URL; other HSBC ETFs use the same URL with \
their own ISIN.
Format: xls. A preamble has label/value pairs in columns A/B: 'Name', 'Date' (e.g. 2026-10-01), 'Fund Size'. Then \
the header row: ISIN | CUSIP | SecurityName | NumberOfShare | MarketValue | Country | LocalCurrencyCode | Weighting.
Weights are percent (16.10713 means 16.1%). The file lists all ~377 holdings.""", HSBC_WEB, HSBC_EXPECT),
    DraftCase("draft_wahed_sheet_link", WAHED_FUND, """\
Issuer: Wahed Invest.
The product page https://www.wahed.com/uk/djiw has a 'Holdings' link to a Google Sheets file \
(href like https://docs.google.com/spreadsheets/d/<id>/edit...). The sheet id changes when Wahed republishes, so the \
link must be found on the page each time. Other Wahed product pages use the lowercase ticker in the path.
Format: spreadsheet (exported as xlsx). Header row: Date | Fund | Ticker | ISIN | SecurityName | Shares | \
<market value> | Weight. The Date column holds the as-of date on every row.
Weights are fractions (0.0925 means 9.25%). All holdings are listed.""", WAHED_WEB, WAHED_EXPECT),
    DraftCase("draft_invesco_browser_json", INVESCO_FUND, """\
Issuer: Invesco.
Holdings come from the JSON API https://dng-api.invesco.com/x/IE000UOXRAM8/holdings. It rejects plain HTTP clients \
and only answers fetches made from inside a browser session on the Invesco site: open https://www.invesco.com/uk/en/ \
first, then fetch the API from that page. The ISIN is in the URL path.
Format: JSON. Top-level 'effectiveDate' is the as-of date (e.g. 2026-10-02); 'holdings' is a list of objects with \
keys name, isin, cusip, weight.
Weights are percent (8.662). All holdings (over 1,000) are listed.""", INVESCO_WEB, INVESCO_EXPECT),
    DraftCase("draft_physical_gold", GOLD_FUND, """\
Issuer: Invesco.
Invesco Physical Gold ETC is backed by allocated physical gold bullion held in a vault. It holds no securities and \
publishes no holdings list; its exposure is 100% gold.""", FixtureWeb(), GOLD_EXPECT),
]


# ---------------------------------------------------------------- component: repair


@dataclass(frozen=True)
class RepairCase:
    id: str
    fund: Fund
    broken: Recipe  # run against `web` to produce the failure the model sees
    web: FixtureWeb
    expect_action: str  # fix | rediscover | give_up
    expect: Expect  # applied to the fixed recipe when expect_action == "fix"


REPAIR_CASES = [
    RepairCase("repair_wrong_weight_column", HSBC_FUND,
               HSBC.model_copy(update={"columns": ColumnMap(name="SecurityName", weight="Weight", isin="ISIN")}),
               HSBC_WEB, "fix", HSBC_EXPECT),
    RepairCase("repair_wrong_header_marker", HSBC_FUND, HSBC.model_copy(update={"header_contains": "Ticker"}),
               HSBC_WEB, "fix", HSBC_EXPECT),
    RepairCase("repair_fraction_weights", WAHED_FUND, WAHED.model_copy(update={"weight_scale": "percent"}),
               WAHED_WEB, "fix", WAHED_EXPECT),
    RepairCase("repair_wrong_json_path", INVESCO_FUND, INVESCO.model_copy(update={"json_holdings_path": "data.holdings"}),
               INVESCO_WEB, "fix", INVESCO_EXPECT),
    RepairCase("repair_cookie_wall", HSBC_FUND, HSBC, COOKIE_WALL_WEB, "rediscover", Expect()),
]


# ---------------------------------------------------------------- e2e (live web)


@dataclass(frozen=True)
class E2ECase:
    id: str
    fund: Fund
    expect: Expect


E2E_CASES = [
    E2ECase("ishares_sp500", Fund("IE00B5BMR087", "iShares Core S&P 500 UCITS ETF USD (Acc)", "CSPX"),
            Expect(min_holdings=450, must_include=(APPLE, MICROSOFT, NVIDIA), source_hosts=("ishares.com", "blackrock.com"))),
    E2ECase("ishares_msci_world", Fund("IE00B4L5Y983", "iShares Core MSCI World UCITS ETF USD (Acc)", "SWDA"),
            Expect(min_holdings=1000, must_include=(APPLE, MICROSOFT, NVIDIA), source_hosts=("ishares.com", "blackrock.com"))),
    E2ECase("ishares_em_imi", Fund("IE00BKM4GZ66", "iShares Core MSCI EM IMI UCITS ETF USD (Acc)", "EIMI"),
            Expect(min_holdings=1000, must_include=(TSMC,), source_hosts=("ishares.com", "blackrock.com"))),
    E2ECase("vanguard_sp500", Fund("IE00B3XXRP09", "Vanguard S&P 500 UCITS ETF (USD) Distributing", "VUSA"),
            Expect(min_holdings=450, must_include=(APPLE, MICROSOFT, NVIDIA), source_hosts=("vanguard.co.uk", "vanguard.com"))),
    E2ECase("vanguard_all_world", Fund("IE00B3RBWM25", "Vanguard FTSE All-World UCITS ETF (USD) Distributing", "VWRL"),
            Expect(min_holdings=1500, must_include=(APPLE, MICROSOFT, NVIDIA), source_hosts=("vanguard.co.uk", "vanguard.com"))),
    E2ECase("xtrackers_msci_world", Fund("IE00BJ0KDQ92", "Xtrackers MSCI World UCITS ETF 1C", "XDWD"),
            Expect(min_holdings=1000, must_include=(APPLE, MICROSOFT, NVIDIA), source_hosts=("xtrackers.com", "dws.com"))),
    E2ECase("hsbc_em_islamic", HSBC_FUND, Expect(min_holdings=200, must_include=(SAMSUNG,), source_hosts=("hsbc.co.uk", "hsbc.com"))),
    E2ECase("wahed_islamic_world", WAHED_FUND, Expect(min_holdings=50, source_hosts=("wahed.com", "google.com"))),
    E2ECase("invesco_physical_gold", GOLD_FUND, GOLD_EXPECT),
    E2ECase("ishares_physical_gold", Fund("IE00B4ND3602", "iShares Physical Gold ETC", "IGLN"), GOLD_EXPECT),
]


# ---------------------------------------------------------------- component: entity resolution
# Real clusters from a portfolio's look-through, with OpenFIGI evidence as returned on 2026-10-06. The agent gets
# no tools in evals, so it is judged on the evidence alone.


@dataclass(frozen=True)
class ResolveCase:
    id: str
    securities: dict  # isin -> (OpenFIGI name, security type, exchange, ticker, name in the fund file)
    expect: tuple  # the correct partition: tuples of ISINs, one per company


RESOLVE_CASES = [
    ResolveCase("resolve_tsmc_adr", {
        "TW0002330008": ("TAIWAN SEMICONDUCTOR MANUFAC", "Common Stock", "TT (Taiwan Stock Exchange)", "2330", "Taiwan Semiconductor Manufacturing"),
        "US8740391003": ("TAIWAN SEMICONDUCTOR-SP ADR", "Depositary Receipt", "US", "TSM", "TAIWAN SEMICONDUCTOR-SP ADR")},
        (("TW0002330008", "US8740391003"),)),
    ResolveCase("resolve_amd_vs_amec", {
        "US0079031078": ("ADVANCED MICRO DEVICES", "Common Stock", "US", "AMD", "Advanced Micro Devices Inc"),
        "CNE100003MM9": ("ADVANCED MICRO-FABRICATION-A", "Common Stock", "CH", "688012", "Advanced Micro-fabrication Equipment")},
        (("US0079031078",), ("CNE100003MM9",))),
    ResolveCase("resolve_merck_vs_merck_kgaa", {
        "US58933Y1055": ("MERCK & CO. INC.", "Common Stock", "US", "MRK", "MERCK & CO. INC."),
        "DE0006599905": ("MERCK KGAA", "Common Stock", "GR", "MRK", "Merck Kgaa")},
        (("US58933Y1055",), ("DE0006599905",))),
    ResolveCase("resolve_reliance_gdr_vs_us_reliance", {
        "INE002A01018": ("RELIANCE INDUSTRIES LIMITED", "Common Stock", "IN", "RELIANCE", "Reliance Industries Ltd"),
        "US7594701077": ("RELIANCE INDS-SPONS GDR 144A", "Depositary Receipt", "LX", "RIGDS", "Reliance Inds-spons Gdr 144a"),
        "US7595091023": ("RELIANCE INC", "Common Stock", "US", "RS", "Reliance Inc")},
        (("INE002A01018", "US7594701077"), ("US7595091023",))),
    ResolveCase("resolve_delta_parent_vs_thai_subsidiary", {
        "TW0002308004": ("DELTA ELECTRONICS INC", "Common Stock", "TT (Taiwan Stock Exchange)", "2308", "Delta Electronics Inc"),
        "TH0528A10Z14": ("DELTA ELECTRONICS THAI-FORGN", "Common Stock", "TB", "DELTA/F", "Delta Electronics (Thailand)"),
        "TH0528010R18": ("DELTA ELECTRONICS THAI-NVDR", "Depositary Receipt", "TB", "DELTA-R", "Delta Electronics Thai-nvdr")},
        (("TW0002308004",), ("TH0528A10Z14", "TH0528010R18"))),
    ResolveCase("resolve_coca_cola_vs_bottlers", {
        "US1912161007": ("COCA-COLA CO/THE", "Common Stock", "US", "KO", "Coca-cola Co/the"),
        "US1910981026": ("COCA-COLA CONSOLIDATED INC", "Common Stock", "US", "COKE", "Coca-cola Consolidated Inc"),
        "MX01KO000002": ("COCA-COLA FEMSA SAB DE CV", "Unit", "US", "COCSF", "Coca-Cola Femsa SAB de CV")},
        (("US1912161007",), ("US1910981026",), ("MX01KO000002",))),
    ResolveCase("resolve_petrobras_ordinary_and_preference", {
        "BRPETRACNPR6": ("PETROBRAS - PETROLEO BRAS-PR", "Preference", "BZ", "PETR4", "Petroleo Brasileiro SA Pet"),
        "BRPETRACNOR9": ("PETROBRAS - PETROLEO BRAS", "Common Stock", "BZ", "PETR3", "Petroleo Brasileiro SA Pet")},
        (("BRPETRACNPR6", "BRPETRACNOR9"),)),
]
