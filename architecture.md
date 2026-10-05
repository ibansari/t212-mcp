# Architecture

## High-level flow

Claude calls a tool, and the server handles it on one of three routes. The dotted line is only used with `allow_agent`.

```mermaid
flowchart LR
    claude["Claude"] --> server{{"t212-mcp"}}

    server --> portfolio["Portfolio"]
    server --> exposure["Exposure"]
    server --> refresh["Refresh"]

    portfolio --> t212[("Trading 212")]
    exposure --> db[("Postgres")]
    refresh --> web[("Issuer sites")]
    refresh -.-> openai[("OpenAI")]
    refresh --> db

    classDef route fill:#fff7ed,stroke:#f97316,color:#431407
    classDef ext fill:#eef2ff,stroke:#6366f1,color:#1e1b4b
    class portfolio,exposure,refresh route
    class t212,web,openai,db ext
```

| Route | Tools | Reads from | Writes to | Speed |
|---|---|---|---|---|
| 1. Portfolio | `get_portfolio_update`, `get_account_summary`, `get_positions`, `get_position`, `get_dividends`, `get_order_history`, `get_transactions` | Trading 212 API (briefly cached) | `get_portfolio_update` saves a snapshot | Seconds |
| 2. Exposure | `get_etf_exposure`, `get_exposure_changes`, `get_holdings_status` | Postgres only | Nothing | Instant |
| 3. Refresh | `refresh_etf_holdings` (or `t212-mcp refresh-holdings`) | Trading 212, issuer sites, OpenAI if the agent is allowed | Recipes, fund status, holdings, exposure snapshot | Seconds without the agent, minutes with it |

Route 2 only shows what route 3 last saved, so run a refresh first.

## Inside the server

```mermaid
flowchart TB
    subgraph entry["Entry"]
        http["HTTP /mcp<br/>bearer token check"]
        stdio["stdio"]
        cli["CLI<br/>refresh-holdings"]
    end

    tools["server.py<br/>MCP tools"]
    client["client.py<br/>Trading 212 client: cache, retry on 429"]
    snaps["snapshots.py<br/>portfolio snapshot + diff"]

    subgraph lt["lookthrough/"]
        pipeline["graph.py<br/>LangGraph pipeline"]
        recipes["recipes.py<br/>recipe registry"]
        extract["extractors.py<br/>download + parse (httpx, Chromium)"]
        validate["validate.py<br/>sanity checks"]
        expo["exposure.py<br/>combine holdings"]
        store["store.py<br/>fund state, holdings, exposure"]
        agent["agent_tools.py + llm.py<br/>search, browse, ChatOpenAI"]
    end

    db[("Postgres<br/>db/models.py")]

    http & stdio --> tools
    cli --> pipeline
    tools --> client
    tools --> snaps
    tools -- "exposure routes" --> store
    tools -- "refresh route" --> pipeline
    pipeline --> recipes & extract & validate & expo & store
    pipeline -. "allow_agent" .-> agent
    snaps & recipes & store --> db
    pipeline -- "checkpoints" --> db
```

## Refresh route in detail (per fund)

A refresh loads your positions from Trading 212, runs this flow for each ETF, then combines the results into one exposure snapshot. Dotted steps only run with `allow_agent`.

```mermaid
flowchart TD
    fund(["ETF held"]) --> saved["Try saved recipes<br/>download, parse, validate"]
    saved -- "valid" --> ok(["ok<br/>holdings saved"])
    saved -- "failed or none,<br/>agent not allowed" --> fail

    saved -. "no recipe" .-> discover["Agent researches<br/>the issuer's site"]
    saved -. "recipe broken" .-> repair{"LLM: fix,<br/>rediscover or give up?"}

    discover -.-> draft["LLM drafts<br/>a new recipe"]
    draft -.-> test["Test it<br/>download, parse, validate"]
    test -. "valid" .-> save["Save recipe<br/>+ holdings"] --> ok
    test -. "invalid" .-> repair
    repair -. "fix" .-> test
    repair -. "rediscover" .-> discover
    repair -. "give up / out of attempts<br/>/ token budget spent" .-> fail

    fail(["stale: keep last good holdings<br/>unresolved: none yet"])

    classDef good fill:#ecfdf5,stroke:#10b981,color:#064e3b
    classDef bad fill:#fef2f2,stroke:#ef4444,color:#450a0a
    classDef ai fill:#f5f3ff,stroke:#8b5cf6,color:#2e1065
    class ok,save good
    class fail bad
    class discover,draft,repair ai
```

## Data model

```mermaid
erDiagram
    portfolio_snapshots ||--o{ portfolio_positions : has
    recipes ||--o{ recipes : "superseded_by"
    recipes |o--o{ funds : "current recipe"
    recipes |o--o{ holdings_snapshots : "fetched with"
    funds ||--o{ holdings_snapshots : has
    holdings_snapshots ||--o{ holdings : contains

    portfolio_snapshots {
        int id PK
        string env
        timestamptz taken_at
        float total_value
        float invested_value
        float unrealized_pnl
        float cash_available
    }
    portfolio_positions {
        int snapshot_id PK,FK
        string ticker PK
        string name
        float quantity
        float value
        float pnl
    }
    recipes {
        string id PK
        string kind
        string scope
        string issuer
        string isin
        jsonb spec
        timestamptz discovered_at
        string discovered_by
        timestamptz superseded_at
        string superseded_by FK
    }
    funds {
        string isin PK
        string status
        timestamptz checked_at
        string recipe_id FK
        text last_error
        jsonb validation_report
    }
    holdings_snapshots {
        int id PK
        string fund_isin FK
        string recipe_id FK
        string as_of
        string coverage
        text source_url
        jsonb countries
        jsonb sectors
        timestamptz fetched_at
    }
    holdings {
        int id PK
        int snapshot_id FK
        int position
        text name
        float weight_pct
        string isin
        string country
        string sector
    }
    exposure_snapshots {
        int id PK
        date as_of UK
        float invested_value
        int securities_count
        float top10_pct
        float cash_inside_funds
        float funds_with_data_pct
        jsonb payload
    }
    agent_runs {
        int id PK
        timestamptz at
        string event
        string isin
        string model
        string recipe_id
        int tokens
        int attempts
        text error
    }
```

`exposure_snapshots` and `agent_runs` have no foreign keys. Exposure is a computed daily summary, and `agent_runs` is an append-only log.
