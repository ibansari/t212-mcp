# Trading 212 MCP

A read-only [FastMCP](https://gofastmcp.com) server for your Trading 212 Invest account. It can report holdings, P&L, cash, dividends and order history, and give you portfolio updates showing what changed since the last check. It never places or cancels orders.

See [API.md](API.md) for the full tool reference and [architecture.md](architecture.md) for diagrams.

## Setup

1. In Trading 212 go to **Settings → API (Beta) → Generate API key**. Grant only the read scopes: account data, portfolio, history and metadata. Copy the key and the secret; the secret is shown only once.
2. `cp .env.example .env` and fill in `T212_API_KEY`, `T212_API_SECRET` and `T212_ENV` (`live` or `demo`, matching the account the key was created in). Set `POSTGRES_PASSWORD`, and use the same password in `T212_DATABASE_URL`.
3. `uv sync`
4. Start Postgres: `docker compose up -d db`. Tables are created automatically on first use.

## Use with Claude Code

```bash
claude mcp add trading212 -- uv --directory /Users/ibraheemansari/mcp-test run t212-mcp
```

Then ask things like "give me a portfolio update" or "what dividends have I received recently?".

## Serve over HTTP

```bash
uv run t212-mcp --http            # http://127.0.0.1:8765/mcp
claude mcp add --transport http trading212-http http://127.0.0.1:8765/mcp --header "Authorization: Bearer $T212_MCP_AUTH_TOKEN"
```

When `T212_MCP_AUTH_TOKEN` is set, every request needs `Authorization: Bearer <token>`. The server refuses to listen on a non-local address without a token.

## Run with Docker Compose

`compose.yaml` runs Postgres and the MCP server over HTTP with bearer-token auth. It reads `.env`, which must set `POSTGRES_PASSWORD` and `T212_MCP_AUTH_TOKEN` (`openssl rand -hex 32`).

```bash
docker compose up -d --build                                      # http://127.0.0.1:8765/mcp
docker compose exec mcp t212-mcp refresh-holdings                 # look-through refresh inside the stack
```

Ports are published on `127.0.0.1` only. Data lives in the `pgdata` volume.

## Use with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "trading212": {
      "command": "uv",
      "args": ["--directory", "/Users/ibraheemansari/mcp-test", "run", "t212-mcp"]
    }
  }
}
```

## Tools

| Tool | What it returns |
|---|---|
| `get_portfolio_update` | Totals, top gainers and losers, concentration, and changes since the previous update (each call saves a snapshot to the database) |
| `get_account_summary` | Total value, cash breakdown, cost basis, realised and unrealised P&L |
| `get_positions` | All holdings with value, P&L, P&L % and portfolio weight (sortable) |
| `get_position` | One holding, looked up by `AAPL_US_EQ` or plain `AAPL` |
| `get_dividends` | Recent dividends and their total |
| `get_order_history` | Recent orders with fill price and value |
| `get_transactions` | Deposits, withdrawals, fees and interest |
| `get_etf_exposure` | Look-through exposure: each ETF broken into its holdings and combined with your direct stocks, grouped by security, country or sector |
| `get_exposure_changes` | How look-through exposure shifted between the latest refresh and one `days` ago |
| `get_holdings_status` | Per-fund look-through status (ok/stale/unresolved), recipe, holdings date and last error, plus recent agent runs |
| `refresh_etf_holdings` | Re-downloads fund holdings now; with `allow_agent=true`, funds without a working recipe are researched by the LLM agent |

The server also provides the resource `portfolio://summary` and the prompt `daily_briefing`.

## ETF look-through

The look-through tools read data saved in the database by a refresh. Each fund's holdings are fetched by a *recipe* that says where the issuer publishes the holdings file and how to parse it. Refreshing with saved recipes needs no LLM.

When a fund has no recipe, or its recipe stops working, a discovery agent can search the issuer's site and write or repair one. The agent uses OpenAI: set `OPENAI_API_KEY` in `.env`, and optionally `T212_LLM_MODEL` (default `gpt-5`). The agent only runs when you allow it, and it is capped at `T212_MAX_AGENT_RUNS_PER_DAY` runs per day (default 5).

```bash
uv run t212-mcp refresh-holdings                 # re-run saved recipes only
uv run t212-mcp refresh-holdings --allow-agent   # also let the agent find recipes for unresolved funds
uv run t212-mcp install-schedule --at 07:30      # macOS: daily refresh (with the agent) via launchd
uv run t212-mcp uninstall-schedule
```

The scheduled job logs to `~/.t212_mcp/refresh.log`.

## Data model

Everything is stored in Postgres (`src/t212_mcp/db/models.py`):

| Table | Holds |
|---|---|
| `portfolio_snapshots`, `portfolio_positions` | One row per `get_portfolio_update` call, with its positions |
| `recipes` | Extraction recipes. A replaced recipe is kept, with `superseded_at` and `superseded_by` set |
| `funds` | Current look-through status of each held ETF: status, recipe, last error, validation report |
| `holdings_snapshots`, `holdings` | Each validated holdings download (one per fund per publication date) and its rows |
| `exposure_snapshots` | Daily combined exposure: headline numbers as columns, full breakdown as JSONB |
| `agent_runs` | Discovery agent events. The daily limit counts `agent_start` events |

The LangGraph refresh checkpoints to the same database.

## Development

```bash
uv run pytest                          # mocked HTTP; needs local Postgres (docker compose up -d db)
uv run fastmcp dev src/t212_mcp/server.py   # MCP Inspector
```

Tests use a `<database>_test` database on the server from `T212_DATABASE_URL` (or set `T212_TEST_DATABASE_URL`). They create it if it's missing and recreate the tables at the start of each run.

Responses are cached briefly to stay within Trading 212's rate limits (for example, the account summary allows 1 request every 5s). If a request is rate-limited (HTTP 429), the client retries it once.
