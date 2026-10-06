"""Entity resolution: no network, LLM or database."""

import pytest
import respx

from t212_mcp.config import Settings
from t212_mcp.lookthrough import entities
from t212_mcp.lookthrough.entities import CompanyGroup, apply_groups, candidate_clusters, issuer_key
from t212_mcp.lookthrough.exposure import compute_exposure
from t212_mcp.lookthrough.extractors import FundHoldings, Holding


@pytest.mark.parametrize("a,b", [
    ("ALPHABET INC-CL A", "ALPHABET INC-CL C"),
    ("SAMSUNG ELECTRONICS-PREF", "SAMSUNG ELECTRONICS CO LTD"),
    ("ALIBABA GROUP HOLDING-SP ADR", "ALIBABA GROUP HOLDING LTD"),
    ("MERCK &amp; CO. INC.", "MERCK & CO INC"),
])
def test_rules_match_share_classes_and_listings(a, b):
    assert issuer_key(a) == issuer_key(b)


@pytest.mark.parametrize("a,b", [
    ("SAMSUNG ELECTRONICS CO LTD", "SAMSUNG ELECTRO-MECHANICS CO"),
    ("DELTA ELECTRONICS INC", "DELTA ELECTRONICS THAI-NVDR"),
    ("MERCK & CO INC", "MERCK KGAA"),
])
def test_rules_keep_different_companies_apart(a, b):
    assert issuer_key(a) != issuer_key(b)


def ent(key, **kw):
    return {"entity_key": key, "name": None, "method": "figi", **kw}


def test_near_misses_become_clusters_for_the_agent():
    known = {"TW0002330008": ent("TAIWAN SEMICONDUCTOR MANUFAC"), "US8740391003": ent("TAIWAN SEMICONDUCTOR"),
             "US0079031078": ent("ADVANCED MICRO DEVICES"), "CNE100003MM9": ent("ADVANCED MICRO FABRICATION EQUIP"),
             "US67066G1040": ent("NVIDIA")}
    shares = {"TW0002330008": 1.05, "US8740391003": 0.47, "US0079031078": 1.49, "CNE100003MM9": 0.01, "US67066G1040": 5.9}
    clusters = candidate_clusters(known, shares, decided=set())
    assert ["TAIWAN SEMICONDUCTOR", "TAIWAN SEMICONDUCTOR MANUFAC"] in clusters
    assert ["ADVANCED MICRO DEVICES", "ADVANCED MICRO FABRICATION EQUIP"] in clusters
    assert not any("NVIDIA" in c for c in clusters)
    decided = {frozenset(c) for c in clusters}
    assert candidate_clusters(known, shares, decided) == []  # never asked twice


def test_agent_groups_become_shared_keys():
    known = {"TW0002330008": ent("TAIWAN SEMICONDUCTOR MANUFAC"), "US8740391003": ent("TAIWAN SEMICONDUCTOR")}
    groups = [CompanyGroup(isins=["TW0002330008", "US8740391003"], name="Taiwan Semiconductor Manufacturing",
                           reason="ADR of the same company")]
    updates = apply_groups(groups, known, set(known))
    assert {u["entity_key"] for u in updates.values()} == {"TAIWAN SEMICONDUCTOR MANUFACTURING"}
    assert all(u["method"] == "llm" and u["name"] == "Taiwan Semiconductor Manufacturing" for u in updates.values())


def test_exposure_adds_up_lines_of_one_company():
    positions = [{"ticker": "BABA_US_EQ", "name": "Alibaba", "isin": "US01609W1027", "value": 190.0},
                 {"ticker": "EMRGl_EQ", "name": "EM fund", "isin": "IE0000000002", "value": 1000.0}]
    fund = FundHoldings(isin="IE0000000002", as_of="2026-10-05", coverage="full", source_url="s", holdings=[
        Holding(name="ALIBABA GROUP HOLDING LTD", weight_pct=10, isin="KYG017191142"),
        Holding(name="TENCENT", weight_pct=90, isin="KYG875721634")])
    ents = {"US01609W1027": {"key": "ALIBABA", "name": None}, "KYG017191142": {"key": "ALIBABA", "name": None}}
    exp = compute_exposure(positions, {"IE0000000002"}, {"IE0000000002": fund}, {"IE0000000002": "ok"}, entities=ents)
    baba = next(r for r in exp["all_securities"] if r["name"] == "Alibaba")
    assert baba["value"] == 290.0 and baba["direct"] == 190.0 and baba["via_funds"] == {"EMRGl_EQ": 100.0}
    assert [m["isin"] for m in baba["members"]] == ["US01609W1027", "KYG017191142"]


class MemStore:
    def __init__(self):
        self.e, self.d, self.traces = {}, set(), []

    def entities(self, isins=None):
        return {i: dict(v) for i, v in self.e.items() if isins is None or i in isins}

    def save_entities(self, rows):
        self.e.update(rows)

    def decided_clusters(self):
        return set(self.d)

    def mark_decided(self, clusters):
        self.d.update(clusters)

    def save_trace(self, **kw):
        self.traces.append(kw)


@respx.mock
async def test_resolve_identifies_new_isins_then_lets_the_agent_settle_clusters(monkeypatch):
    respx.post("https://api.openfigi.com/v3/mapping").respond(json=[
        {"data": [{"name": "TAIWAN SEMICONDUCTOR MANUFAC", "ticker": "2330", "exchCode": "TT", "securityType2": "Common Stock"}]},
        {"data": [{"name": "TAIWAN SEMICONDUCTOR-SP ADR", "ticker": "TSM", "exchCode": "US", "securityType2": "Depositary Receipt"}]},
        {"warning": "No identifier found."}])

    async def fake_adjudicate(model, clusters, known, names, callbacks, limit):
        return [CompanyGroup(isins=["TW0002330008", "US8740391003"], name="Taiwan Semiconductor Manufacturing", reason="ADR")]

    monkeypatch.setattr(entities, "adjudicate", fake_adjudicate)
    store = MemStore()
    securities = [{"isin": "TW0002330008", "name": "TSMC", "pct_of_portfolio": 1.05},
                  {"isin": "US8740391003", "name": "TSM ADR", "pct_of_portfolio": 0.47},
                  {"isin": "XX0000000000", "name": "Unknown Co", "pct_of_portfolio": 0.2},
                  {"isin": "YY0000000000", "name": "Tiny Co", "pct_of_portfolio": 0.001}]  # below materiality
    stats = await entities.resolve(store, Settings(_env_file=None, api_key="k"), securities, allow_agent=True, model=object())
    assert stats == {"material": 3, "new": 3, "clusters": 1, "merged_by_agent": 1}
    assert store.e["XX0000000000"]["method"] == "name" and "YY0000000000" not in store.e
    assert store.e["TW0002330008"]["entity_key"] == store.e["US8740391003"]["entity_key"]
    again = await entities.resolve(store, Settings(_env_file=None, api_key="k"), securities, allow_agent=True, model=object())
    assert again["new"] == 0 and again["clusters"] == 0  # cached, nothing re-asked
