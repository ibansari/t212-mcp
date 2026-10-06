from datetime import datetime, timedelta, timezone

import httpx
import respx

from t212_mcp import digest
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
    assert await digest.send_email(s, "Subj", "<p>hi</p>", b"\x89PNG") == "em_1"
    body = __import__("json").loads(route.calls[0].request.content)
    assert body["to"] == ["me@example.com", "other@example.com"]
    assert body["attachments"][0]["content_id"] == "holdings-chart"
    assert route.calls[0].request.headers["authorization"] == "Bearer re_x"


def test_only_sends_at_uk_hour_across_clock_change():
    summer = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)   # 07:00 BST
    winter = datetime(2026, 12, 7, 7, 0, tzinfo=timezone.utc)  # 07:00 GMT
    assert digest.is_local_hour(7, "Europe/London", summer) and not digest.is_local_hour(7, "Europe/London", summer + timedelta(hours=1))
    assert digest.is_local_hour(7, "Europe/London", winter) and not digest.is_local_hour(7, "Europe/London", winter - timedelta(hours=1))
