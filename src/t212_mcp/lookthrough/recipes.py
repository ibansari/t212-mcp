"""Declarative extraction recipes. Written by the discovery agent, executed by plain code."""

import hashlib
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import db
from ..db import models as m


class ColumnMap(BaseModel):
    """Source column headers (tables) or JSON keys (json) for each field. Match is case-insensitive."""

    name: str = Field(description="Header/key holding the security name")
    weight: str = Field(description="Header/key holding the portfolio weight")
    isin: str | None = Field(default=None, description="Header/key holding the security ISIN, if present")
    country: str | None = Field(default=None, description="Header/key holding the security's country, if present")
    sector: str | None = Field(default=None, description="Header/key holding the security's sector, if present")


class StaticHolding(BaseModel):
    name: str
    weight_pct: float
    isin: str | None = None
    country: str | None = None
    sector: str | None = None


class Recipe(BaseModel):
    """How to fetch and parse one fund's (or one issuer's) holdings.

    URL fields may contain the placeholders {isin}, {isin_lower}, {ticker} and {ticker_lower},
    filled per fund at run time. A recipe with scope='issuer' must use placeholders so it works for
    every fund from that issuer.
    """

    kind: Literal["http_file", "scrape_link", "browser_json", "static"] = Field(
        description=(
            "http_file: download a spreadsheet/CSV/JSON directly from `url`. "
            "scrape_link: load `page_url`, find the holdings file link with `link_pattern`, then parse it like http_file. "
            "browser_json: open `page_url` in a headless browser, then fetch the JSON API at `url` from inside the page. "
            "static: fixed composition in `static_holdings` (commodity/physical funds with no holdings list)."
        )
    )
    scope: Literal["issuer", "isin"] = Field(
        description="'issuer' if the recipe works for every fund of this issuer via placeholders; 'isin' if fund-specific"
    )
    issuer: str = Field(description="Fund issuer/brand, e.g. 'HSBC'")
    issuer_match: str = Field(description="Regex matched (case-insensitive) against the fund's name to select this recipe")
    isin: str | None = Field(default=None, description="The fund ISIN, required when scope='isin'")
    url: str | None = Field(default=None, description="File URL (http_file), or JSON API URL (browser_json)")
    page_url: str | None = Field(default=None, description="Product page URL (scrape_link, browser_json)")
    link_pattern: str | None = Field(
        default=None, description="scrape_link: regex matched against hrefs on page_url to find the holdings file"
    )
    file_format: Literal["xls", "xlsx", "csv", "json"] | None = Field(
        default=None, description="Format of the holdings data; Google Sheets links are exported as xlsx automatically"
    )
    header_contains: str | None = Field(
        default=None, description="Tables: a cell value that appears in the header row (e.g. 'ISIN'), used to find it"
    )
    json_holdings_path: str | None = Field(
        default=None, description="json: dot path to the list of holdings in the response, e.g. 'holdings' or 'data.items'"
    )
    columns: ColumnMap | None = None
    weight_scale: Literal["percent", "fraction"] = Field(
        default="percent", description="'percent' if weights are like 5.2 (=5.2%), 'fraction' if like 0.052"
    )
    as_of: str | None = Field(
        default=None,
        description=(
            "Where the holdings date is: 'cell_right_of:<label>' (tables, e.g. 'cell_right_of:Date'), "
            "'column:<header>' (tables), or 'json:<dot path>' (json, e.g. 'json:effectiveDate')"
        ),
    )
    country_breakdown_url: str | None = Field(
        default=None, description="Optional JSON URL giving the fund's official country split (browser_json/http)"
    )
    country_breakdown_path: str | None = Field(
        default=None, description="Dot path to the list of {name, value} entries in country_breakdown_url's response"
    )
    static_holdings: list[StaticHolding] | None = None
    coverage: Literal["full", "partial"] = Field(
        default="full", description="'partial' if the source only lists top holdings, not the whole fund"
    )
    notes: str = Field(default="", description="How this source was found and anything a maintainer should know")


class StoredRecipe(Recipe):
    id: str = ""
    discovered_at: str = ""
    discovered_by: str = ""


def recipe_id(recipe: Recipe) -> str:
    base = recipe.isin if recipe.scope == "isin" and recipe.isin else re.sub(r"[^a-z0-9]+", "-", recipe.issuer.lower())
    digest = hashlib.sha1(recipe.model_dump_json(exclude={"notes"}).encode()).hexdigest()[:6]
    return f"{recipe.scope}-{base}-{digest}"


def fill(template: str | None, isin: str, ticker: str) -> str | None:
    if template is None:
        return None
    return (
        template.replace("{isin_lower}", isin.lower())
        .replace("{isin}", isin)
        .replace("{ticker_lower}", ticker.lower())
        .replace("{ticker}", ticker)
    )


class Registry:
    """Recipes in the database. Replaced recipes are kept (superseded) for history."""

    def __init__(self, database_url: str):
        self.sessions = db.sessions(database_url)

    @staticmethod
    def _stored(row: m.Recipe) -> StoredRecipe:
        return StoredRecipe(**row.spec, id=row.id, discovered_at=db.iso(row.discovered_at) or "",
                            discovered_by=row.discovered_by)

    def all(self) -> list[StoredRecipe]:
        """Active recipes."""
        with self.sessions() as s:
            rows = s.scalars(select(m.Recipe).where(m.Recipe.superseded_at.is_(None)).order_by(m.Recipe.id))
            out = []
            for row in rows:
                try:
                    out.append(self._stored(row))
                except Exception:
                    continue
            return out

    def candidates(self, isin: str, fund_name: str) -> list[StoredRecipe]:
        """ISIN-specific recipes first, then issuer recipes whose pattern matches the fund name."""
        recipes = self.all()
        exact = [r for r in recipes if r.scope == "isin" and r.isin == isin]
        issuer = []
        for r in recipes:
            if r.scope != "issuer":
                continue
            try:
                if re.search(r.issuer_match, fund_name, re.IGNORECASE):
                    issuer.append(r)
            except re.error:
                continue
        return exact + issuer

    def save(self, recipe: Recipe, discovered_by: str) -> StoredRecipe:
        recipe = Recipe.model_validate(recipe.model_dump())  # drop StoredRecipe fields
        spec, rid = recipe.model_dump(exclude_none=True), recipe_id(recipe)
        now = db.utcnow()
        with self.sessions.begin() as s:
            row = s.get(m.Recipe, rid) or m.Recipe(id=rid)
            row.kind, row.scope, row.issuer, row.isin, row.spec = recipe.kind, recipe.scope, recipe.issuer, recipe.isin, spec
            row.discovered_at, row.discovered_by = now, discovered_by
            row.superseded_at = row.superseded_by = None
            s.add(row)
            s.flush()  # the new row must exist before older ones can point at it
            # One active recipe per scope key: supersede any older recipe for the same ISIN / issuer.
            q = select(m.Recipe).where(m.Recipe.superseded_at.is_(None), m.Recipe.scope == recipe.scope, m.Recipe.id != rid)
            for old in s.scalars(q):
                same = old.isin == recipe.isin if recipe.scope == "isin" else old.issuer.lower() == recipe.issuer.lower()
                if same:
                    old.superseded_at, old.superseded_by = now, rid
            return self._stored(row)
