from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from t212_mcp import digest, snapshots
from t212_mcp.config import Settings

NOW = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)


def pos(ticker, name, qty, value, weight, isin=None, pnl_pct=10.0):
    return {"ticker": ticker, "name": name, "isin": isin, "quantity": qty, "value": value, "weight_pct": weight,
            "pnl_pct": pnl_pct}


POSITIONS = [
    pos("AAPL_US_EQ", "Apple", 10, 1100.0, 55.0, "US0378331005"),
    pos("VWRLl_EQ", "Vanguard FTSE All-World (Dist)", 6, 600.0, 30.0, "IE00B3RBWM25"),
    pos("TSLA_US_EQ", "Tesla", 1, 300.0, 15.0, "US88160R1014"),
]
PREVIOUS = {
    "taken_at": "2026-10-05T06:00:00+00:00", "total_value": 1900.0, "invested_value": 1900.0,
    "unrealized_pnl": 0.0, "cash_available": 0.0,
    "positions": {
        "AAPL_US_EQ": {"name": "Apple", "quantity": 10, "value": 1000.0, "pnl": 0.0},     # +10% price
        "VWRLl_EQ": {"name": "Vanguard FTSE All-World (Dist)", "quantity": 4, "value": 400.0, "pnl": 0.0},  # bought
        "MSFT_US_EQ": {"name": "Microsoft", "quantity": 2, "value": 500.0, "pnl": 0.0},   # closed
    },
}
SUMMARY = {"currency": "GBP", "total_value": 2000.0, "cash": {"available_to_trade": 0.0},
           "investments": {"current_value": 2000.0, "unrealized_pnl": 100.0}}


def test_rows_separate_price_moves_from_trades():
    rows = {r["ticker"]: r for r in digest.holding_rows(POSITIONS, PREVIOUS)}
    assert rows["AAPL_US_EQ"]["change_pct"] == 10.0 and rows["AAPL_US_EQ"]["change"] == 100.0
    vwrl = rows["VWRLl_EQ"]  # price unchanged (100/share); buying 2 more is a note, not a gain
    assert vwrl["change_pct"] == 0.0 and vwrl["change"] == 0.0 and vwrl["note"] == "4 → 6 shares"
    assert rows["TSLA_US_EQ"]["note"] == "new position" and rows["TSLA_US_EQ"]["change"] is None
    assert rows["MSFT_US_EQ"]["note"] == "closed"
    assert [r["ticker"] for r in digest.holding_rows(POSITIONS, PREVIOUS)][0] == "AAPL_US_EQ"


def test_first_digest_has_no_changes_or_notes():
    rows = digest.holding_rows(POSITIONS, None)
    assert all(r["change"] is None and r["note"] is None for r in rows)
    assert digest.render_chart(rows) is None


def test_news_targets_add_large_look_through_companies():
    exposure = {"all_securities": [
        {"name": "Apple Inc", "isin": "US0378331005", "pct_of_portfolio": 60.0},   # already a top position
        {"name": "NVIDIA CORP", "isin": "US67066G1040", "pct_of_portfolio": 6.2},  # only via the ETF
        {"name": "Microsoft Corp", "isin": "US5949181045", "pct_of_portfolio": 4.9},
    ]}
    targets = digest.news_targets(POSITIONS, exposure)
    assert [t["name"] for t in targets] == ["Apple", "Vanguard FTSE All-World", "Tesla", "Nvidia Corp"]
    assert "look-through" in targets[-1]["reason"]


def feed(*items):
    body = "".join(
        f"<item><title>{t} - {src}</title><link>https://news.example/{i}</link><source url='x'>{src}</source>"
        f"<pubDate>{(NOW - timedelta(hours=h)).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>"
        for i, (t, src, h) in enumerate(items))
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


@respx.mock
async def test_fetch_news_keeps_recent_unique_headlines():
    respx.get(url__regex=r"news\.google\.com/rss/search\?q=%22Apple%22").respond(text=feed(
        ("Apple beats estimates", "Reuters", 2), ("Old Apple story", "FT", 30), ("Chip deal announced", "BBC", 1),
        ("Apple beats estimates", "CNBC", 3)))
    respx.get(url__regex=r"news\.google\.com/rss/search\?q=%22Nvidia").respond(text=feed(
        ("Chip deal announced", "BBC", 1), ("Nvidia guidance raised", "CNBC", 3)))
    respx.get(url__regex=r"news\.google\.com/rss/search\?q=%22Tesla").respond(500)
    targets = [{"name": "Apple", "reason": ""}, {"name": "Nvidia Corp", "reason": ""}, {"name": "Tesla", "reason": ""}]
    news = await digest.fetch_news(targets, now=NOW)
    titles = {b["target"]["name"]: [i["title"] for i in b["items"]] for b in news}
    assert titles == {"Apple": ["Chip deal announced", "Apple beats estimates"], "Nvidia Corp": ["Nvidia guidance raised"]}
    assert news[0]["items"][0]["source"] == "BBC"
    assert "stock+OR+shares+OR+earnings" in str(respx.calls[0].request.url)


def test_chart_and_email_render():
    rows = digest.holding_rows(POSITIONS, PREVIOUS)
    png = digest.render_chart(rows)
    assert png and png.startswith(b"\x89PNG")
    news = [{"target": {"name": "Apple", "reason": "55.0% of portfolio"},
             "items": [{"title": "Apple <beats>", "link": "https://n/1", "source": "Reuters", "published": NOW}]}]
    page = digest.render_html(SUMMARY, rows, news, PREVIOUS, True, NOW)
    assert "cid:holdings-chart" in page and "£2,000.00" in page and "+£100.00" in page
    assert "Apple &lt;beats&gt;" in page and "4 → 6 shares" in page and "closed" in page
    assert digest.subject_line(SUMMARY, PREVIOUS, NOW) == "Portfolio Tue 06 Oct: £2,000.00 (+£100.00)"


@respx.mock
async def test_send_email_embeds_chart_inline():
    route = respx.post("https://api.resend.com/emails").respond(json={"id": "em_1"})
    s = Settings(_env_file=None, api_key="k", resend_api_key="re_x", digest_to="me@example.com, other@example.com")
    assert await digest.send_email(s, "Subj", "<p>hi</p>", {"holdings-chart": b"\x89PNG"}) == "em_1"
    body = __import__("json").loads(route.calls[0].request.content)
    assert body["to"] == ["me@example.com", "other@example.com"]
    assert body["attachments"][0]["content_id"] == "holdings-chart"
    assert route.calls[0].request.headers["authorization"] == "Bearer re_x"


def test_only_sends_at_uk_hour_across_clock_change():
    summer = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)   # 07:00 BST
    winter = datetime(2026, 12, 7, 7, 0, tzinfo=timezone.utc)  # 07:00 GMT
    assert digest.is_local_hour(7, "Europe/London", summer) and not digest.is_local_hour(7, "Europe/London", summer + timedelta(hours=1))
    assert digest.is_local_hour(7, "Europe/London", winter) and not digest.is_local_hour(7, "Europe/London", winter - timedelta(hours=1))


EXPOSURE = {
    "all_securities": [
        {"name": "Nvidia", "isin": "US67066G1040", "value": 1457.0, "pct_of_portfolio": 14.6, "direct": 1100.0,
         "via_funds": {"GLBLl_EQ": 357.0}},
        {"name": "Microsoft Corp", "isin": "US5949181045", "value": 378.0, "pct_of_portfolio": 3.8, "direct": 0.0,
         "via_funds": {"GLBLl_EQ": 378.0}},
    ],
    "countries": [{"name": "United States", "value": 1835.0, "pct": 18.4}],
    "coverage": {"funds_with_data_pct": 100.0, "funds": [
        {"ticker": "GLBLl_EQ", "status": "ok", "as_of": "2026-10-05"},
        {"ticker": "EMRGl_EQ", "status": "stale", "as_of": "2026-10-01"}]},
}


def test_exposure_rows_show_how_each_company_is_held_and_its_change():
    previous = {"all_securities": [{"name": "Nvidia", "isin": "US67066G1040", "pct_of_portfolio": 14.2, "value": 1400.0}]}
    rows = digest.exposure_rows(EXPOSURE, previous)
    assert rows[0] == {"name": "Nvidia", "isin": "US67066G1040", "value": 1457.0, "pct": 14.6, "change": 57.0,
                       "change_pct": 4.07, "held": "Direct, GLBL"}
    assert rows[1]["held"] == "GLBL" and rows[1]["change"] is None  # not in the previous digest


def test_companies_come_first_and_positions_last():
    previous = {"all_securities": [{"name": "Nvidia", "isin": "US67066G1040", "pct_of_portfolio": 14.2, "value": 1400.0}]}
    rows = digest.exposure_rows(EXPOSURE, previous)
    page = digest.render_html(SUMMARY, digest.holding_rows(POSITIONS, PREVIOUS), [], PREVIOUS, False, NOW,
                              EXPOSURE, rows, True)
    assert page.index("What you really own") < page.index("News for your largest holdings") < page.index("Your positions")
    assert "cid:lookthrough-chart" in page and "Change by holding" not in page
    assert "Covers 100% of your fund value" in page and "2026-10-01 to 2026-10-05" in page
    assert "older data for EMRG" in page and "Direct, GLBL" in page and "United States 18%" in page
    assert "+£57.00" in page and "+4.07%" in page
    assert digest.render_chart(rows).startswith(b"\x89PNG")  # companies' change chart


def test_without_look_through_the_positions_lead():
    page = digest.render_html(SUMMARY, digest.holding_rows(POSITIONS, PREVIOUS), [], PREVIOUS, True, NOW)
    assert "What you really own" not in page and page.index("Change by holding") < page.index("News for")


# ------------------------------------------------------------------ fixed daily comparison point (database)


@pytest.fixture
def db_settings(database_url):
    return Settings(_env_file=None, api_key="k", env="demo", database_url=database_url)


def test_changes_are_measured_against_the_previous_days_set_time_baseline(db_settings):
    from datetime import date

    from t212_mcp.lookthrough.store import Store

    store = Store(db_settings.database_url)
    london = ZoneInfo("Europe/London")
    for day, total in ((date(2026, 10, 5), 1000.0), (date(2026, 10, 6), 1100.0)):
        store.save_baseline(user_id="owner", env="demo", day=day, taken_at=datetime(day.year, day.month, day.day, 7, tzinfo=london),
                            portfolio={**PREVIOUS, "total_value": total}, exposure={"all_securities": [], "day": str(day)})
    # a manual digest later on the 6th saved only its own snapshot, not a baseline
    snapshots.save(db_settings.database_url, digest.snapshot_key("demo"), {**PREVIOUS, "taken_at": "2026-10-06T17:00:00+00:00"},
                   user_id="owner")

    later_today = datetime(2026, 10, 6, 18, 0, tzinfo=london)
    portfolio, exposure = digest.comparison_point(store, db_settings, later_today)
    assert portfolio["total_value"] == 1000.0 and exposure["day"] == "2026-10-05"  # the 5th's 07:00, not today's runs
    next_morning = datetime(2026, 10, 7, 7, 0, tzinfo=london)
    assert digest.comparison_point(store, db_settings, next_morning)[0]["total_value"] == 1100.0


def test_without_baselines_the_last_earlier_day_snapshot_is_used(db_settings):
    from t212_mcp.lookthrough.store import Store

    store = Store(db_settings.database_url)
    key = digest.snapshot_key("demo")
    snapshots.save(db_settings.database_url, key, {**PREVIOUS, "taken_at": "2026-10-05T17:50:00+00:00", "total_value": 900.0}, user_id="owner")
    snapshots.save(db_settings.database_url, key, {**PREVIOUS, "taken_at": "2026-10-06T08:00:00+00:00", "total_value": 950.0}, user_id="owner")
    portfolio, exposure = digest.comparison_point(store, db_settings, datetime(2026, 10, 6, 12, 0, tzinfo=ZoneInfo("Europe/London")))
    assert portfolio["total_value"] == 900.0 and exposure is None  # today's snapshot is not a comparison point


async def test_only_the_set_time_run_saves_a_baseline(db_settings, monkeypatch):
    from t212_mcp.lookthrough.store import Store

    async def fake_build(settings):
        return digest.Digest(subject="s", html="<p>", images={}, snapshot={**PREVIOUS, "taken_at": "2026-10-07T06:00:00+00:00"},
                             exposure={"all_securities": []})

    async def fake_send(*a):
        return "em"

    settings = db_settings.model_copy(update={"resend_api_key": "re_x", "digest_to": "me@example.com"})
    monkeypatch.setattr(digest, "build", fake_build)
    monkeypatch.setattr(digest, "send_email", fake_send)
    store = Store(settings.database_url)
    monkeypatch.setattr(digest, "is_local_hour", lambda h, tz, now=None: False)
    await digest.run(settings)
    assert store.latest_baseline("demo", before=datetime(2100, 1, 1).date(), user_id="owner") is None
    monkeypatch.setattr(digest, "is_local_hour", lambda h, tz, now=None: True)
    await digest.run(settings)
    assert store.latest_baseline("demo", before=datetime(2100, 1, 1).date(), user_id="owner")["portfolio"]["taken_at"] == "2026-10-07T06:00:00+00:00"
