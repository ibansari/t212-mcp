# API reference

The server speaks the [Model Context Protocol](https://modelcontextprotocol.io). It exposes 12 tools, 1 resource and 1 prompt.

- **HTTP:** `POST <host>/mcp` (streamable HTTP). Requests are authenticated by GitHub sign-in (OAuth 2.1, limited to `T212_ALLOWED_GITHUB_USERS`) or by a static `Authorization: Bearer <token>` (`T212_MCP_AUTH_TOKEN`). Requests without valid credentials get `401`. `GET /health` returns `ok` and needs no authentication.
- **stdio:** `t212-mcp` with no arguments.

Money is in the account currency unless a field names a currency. Percentages are 0–100. Timestamps are ISO 8601.

Errors come back as MCP tool errors (`isError: true`) with a readable message. See [Errors](#errors).

| Tool | Changes state | Purpose |
|---|---|---|
| [`get_portfolio_update`](#get_portfolio_update) | Saves a snapshot | Totals, movers, concentration, changes since the last call |
| [`get_account_summary`](#get_account_summary) | No | Account totals, cash and P&L |
| [`get_positions`](#get_positions) | No | All open positions |
| [`get_position`](#get_position) | No | One position |
| [`get_dividends`](#get_dividends) | No | Recent dividends |
| [`get_order_history`](#get_order_history) | No | Recent orders and fills |
| [`get_transactions`](#get_transactions) | No | Deposits, withdrawals, fees, interest |
| [`get_etf_exposure`](#get_etf_exposure) | No | Look-through exposure |
| [`get_exposure_changes`](#get_exposure_changes) | No | How exposure shifted over time |
| [`get_holdings_status`](#get_holdings_status) | No | Look-through pipeline health |
| [`get_agent_trace`](#get_agent_trace) | No | Why the discovery agent did what it did for a fund |
| [`refresh_etf_holdings`](#refresh_etf_holdings) | Yes | Start a background refresh of fund holdings, optionally with the agent |

None of the tools can place, change or cancel orders.

---

## Portfolio tools

These call the Trading 212 API. Responses are cached briefly (account summary 5 s, positions 2 s, instrument metadata 24 h) to stay within its rate limits.

### `get_portfolio_update`

The main "how is my portfolio doing?" tool. Each call saves a portfolio snapshot to the database and compares it with the previous one for the same environment (`live` / `demo`).

Annotations: `readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: false`.

**Parameters:** none.

**Returns**

| Field | Type | Description |
|---|---|---|
| `as_of` | string | When this snapshot was taken |
| `summary` | object | Same shape as [`get_account_summary`](#get_account_summary) |
| `position_count` | int | Number of open positions |
| `top_gainers` | Brief[] | Up to 3 positions with the highest positive `pnl_pct` |
| `top_losers` | Brief[] | Up to 3 positions with the most negative `pnl_pct` |
| `concentration.largest_holdings` | Brief[] | Top 5 positions by value |
| `concentration.top5_weight_pct` | float | Combined weight of those 5 |
| `since_last_update` | object \| null | `null` on the first call; otherwise see below |

`Brief` = `{ticker, name, value, pnl, pnl_pct, weight_pct}`.

`since_last_update`:

| Field | Type | Description |
|---|---|---|
| `since` | string | Timestamp of the previous snapshot |
| `total_value_change` | float | |
| `unrealized_pnl_change` | float | |
| `cash_change` | float | Change in cash available to trade |
| `positions_opened` | `{ticker, name, quantity}[]` | Held now, not before |
| `positions_closed` | `{ticker, name, quantity}[]` | Held before, not now |
| `quantity_changes` | `{ticker, name, from, to}[]` | Positions whose quantity changed |
| `biggest_pnl_gains` | `{ticker, name, pnl_change}[]` | Up to 3 largest P&L increases |
| `biggest_pnl_drops` | `{ticker, name, pnl_change}[]` | Up to 3 largest P&L decreases |

### `get_account_summary`

Account overview. Also available as the resource `portfolio://summary`.

**Parameters:** none.

**Returns**

```json
{
  "account_id": 1,
  "currency": "GBP",
  "total_value": 1200.0,
  "cash": { "available_to_trade": 200.0, "in_pies": 0.0, "reserved_for_orders": 0.0 },
  "investments": {
    "current_value": 1000.0,
    "total_cost": 800.0,
    "unrealized_pnl": 200.0,
    "unrealized_pnl_pct": 25.0,
    "realized_pnl": 5.0
  }
}
```

`unrealized_pnl_pct` is `null` when the total cost is 0.

### `get_positions`

All open positions, sorted in descending order.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `sort_by` | `"value"` \| `"pnl"` \| `"pnl_pct"` \| `"weight"` | `"value"` | Sort key (descending) |
| `limit` | int \| null | `null` | Return only the top N |

**Returns** `{count, positions: Position[]}`. `count` is the total number of positions, before `limit` is applied.

`Position`:

| Field | Type | Description |
|---|---|---|
| `ticker` | string | Trading 212 ticker, e.g. `AAPL_US_EQ` |
| `name` | string | |
| `isin` | string \| null | |
| `instrument_currency` | string \| null | Currency the instrument trades in |
| `quantity` | float | |
| `average_price` | float \| null | In instrument currency |
| `current_price` | float \| null | In instrument currency |
| `value` | float | Current value |
| `cost` | float | Total cost |
| `pnl` | float | Unrealised P&L |
| `pnl_pct` | float \| null | `null` when cost is 0 |
| `fx_impact` | float \| null | Currency effect on P&L |
| `weight_pct` | float \| null | Share of invested value |
| `opened_at` | string \| null | |

### `get_position`

One position in detail.

**Parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `ticker` | string | yes | Trading 212 ticker (`AAPL_US_EQ`) or plain symbol (`AAPL`, case-insensitive) |

**Returns** one [`Position`](#get_positions). Fails if no open position matches, and the message lists the tickers held.

### `get_dividends`

Recent dividends, newest first.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `20` | Maximum number of dividends |
| `ticker` | string \| null | `null` | Only this Trading 212 ticker |

**Returns** `{count, total, dividends[]}`. Each dividend is `{ticker, name, paid_on, amount, currency, quantity, gross_per_share, type}`. `total` is the sum of `amount`.

### `get_order_history`

Recent historical orders, newest first.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `20` | Maximum number of orders |
| `ticker` | string \| null | `null` | Only this Trading 212 ticker |

**Returns** `{count, orders[]}`. Each order is:

| Field | Description |
|---|---|
| `id`, `ticker`, `name` | |
| `side`, `type`, `status` | As reported by Trading 212 |
| `created_at`, `filled_at` | |
| `quantity` | Filled quantity, falling back to the ordered quantity |
| `fill_price` | |
| `net_value`, `realized_pnl`, `currency` | Effect on the account |

### `get_transactions`

Recent cash movements: deposits, withdrawals, fees, transfers and interest.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `20` | Maximum number of transactions |

**Returns** `{count, transactions[]}`. Transactions are passed through unchanged from the Trading 212 API.

---

## Look-through tools

These read from the database, which a refresh fills (`refresh_etf_holdings` or `t212-mcp refresh-holdings`). Only `refresh_etf_holdings` calls external services.

### `get_etf_exposure`

What you really own once each ETF is broken into its holdings, combined with the stocks you hold directly. Uses the latest refresh.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `top_n` | int | `25` | Number of rows in the breakdown |
| `group_by` | `"security"` \| `"country"` \| `"sector"` | `"security"` | Which breakdown to return |

**Returns**

| Field | Type | Description |
|---|---|---|
| `as_of` | string | Date of the refresh |
| `invested_value` | float | Total invested value |
| `securities_count` | int | Distinct underlying securities |
| `top10_pct` | float | Combined weight of the 10 largest securities |
| `cash_inside_funds` | float | Cash and similar lines inside funds |
| `coverage` | object | See below |
| `top_securities` | Security[] | When `group_by = "security"` |
| `countries` | `{name, value, pct}[]` | When `group_by = "country"` |
| `sectors` | `{name, value, pct}[]` | When `group_by = "sector"` |

`Security` = `{name, isin, value, pct_of_portfolio, direct, via_funds}`. `direct` is the value held directly, and `via_funds` maps each fund ticker to the value held through it.

`coverage` = `{funds_value, funds_with_data_pct, uncovered_value, funds[]}`. Each fund is `{ticker, name, value, status, coverage, as_of, holdings_count}`, where `status` is `ok`, `stale`, `unresolved` or `missing`, and `coverage` is `full` or `partial` (the issuer lists only top holdings). Value the server can't see through is grouped under `Unknown (...)` countries and sectors.

### `get_exposure_changes`

How exposure shifted between the latest refresh and the one `days` ago. Falls back to the oldest refresh if there's none that old. Needs at least two refreshes on different days.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `days` | int | `7` | How far back to compare |

**Returns**

| Field | Type | Description |
|---|---|---|
| `from`, `to` | string | Dates compared |
| `invested_value_change` | float | |
| `biggest_security_shifts` | `{name, isin, pct_change, from_pct, to_pct}[]` | Up to 10, largest absolute change first |
| `biggest_country_shifts` | `{country, pct_change, to_pct}[]` | Up to 10 |

### `get_holdings_status`

Health of the look-through pipeline.

**Parameters:** none.

**Returns**

| Field | Type | Description |
|---|---|---|
| `llm_model` | string | Configured OpenAI model |
| `refresh` | object | The current or last background refresh; see below |
| `funds` | object[] | One per fund ever refreshed; see below |
| `recipes` | int | Number of active recipes |
| `recent_agent_runs` | object[] | Last 10 agent events: `{at, event, isin, model, recipe_id, tokens, attempts, error}`, with empty fields left out |

Each fund: `{isin, status, checked_at, holdings_as_of, holdings_count, recipe, error}`, where `recipe` is `{id, kind, scope, issuer, discovered_by}` or `null`.

`refresh`:

| Field | Description |
|---|---|
| `state` | `idle` (none since the server started), `running`, `done` or `failed` |
| `allow_agent`, `started_at`, `finished_at` | |
| `as_of` | When `done`: date of the new exposure snapshot |
| `results` | When `done`: `{ticker, isin, status, detail, tokens}[]`, one per fund. `status` is `ok`, `stale` (refresh failed, older holdings kept) or `unresolved`; `detail` explains why |
| `funds_with_data_pct` | When `done` |
| `error` | When `failed` |

Refresh status is kept in memory, so a server restart resets it to `idle`. Saved holdings and exposure are kept.

### `get_agent_trace`

Recent discovery-agent traces for one fund, newest first. Use it to see why the agent failed to find or fix a holdings source.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `fund` | string | required | Ticker (e.g. `IGDA`) or ISIN |
| `limit` | int | `3` | Number of traces (max 10) |

**Returns** `{fund, traces[]}`. Each trace is `{at, isin, ticker, step, model, tokens, duration_ms, error, tree}`. `step` is `discover`, `draft` or `repair`. `tree` is the run as nested nodes `{type, name, ms, input, output, tokens?, error?, children}`, with model calls (`llm`) and tool calls (`tool`) in order. Texts are capped at 4,000 characters, and a model call's input keeps only its newest message. The last 30 traces per fund are kept. Traces contain prompts, fund names and public web content, never positions or values.

### `refresh_etf_holdings`

Starts a background refresh and returns at once. The refresh re-downloads holdings for every ETF you hold, then saves a new exposure snapshot for today, replacing any earlier one from the same day. Follow it with [`get_holdings_status`](#get_holdings_status), using its `refresh` field.

It runs in the background because an agent refresh can take minutes, longer than hosting proxies allow for a single request.

Annotations: `readOnlyHint: false`, `destructiveHint: false`, `openWorldHint: true`.

**Parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `allow_agent` | bool | `false` | Let the discovery agent research funds that have no working recipe. Uses OpenAI, can take several minutes, and is limited by `T212_MAX_AGENT_RUNS_PER_DAY` and `T212_AGENT_TOKEN_BUDGET` |

Without `allow_agent`, only saved recipes are used and no LLM is called. Funds run 4 at a time, or one at a time with the agent, so a recipe learned for one fund can be reused by the issuer's other funds.

**Returns** `{started, state, allow_agent, started_at}`.
- `started: true` with `state: "running"` when a refresh was started.
- `started: false` with a `reason` when one is already running. Only one refresh runs at a time.

---

## Resource

| URI | MIME type | Contents |
|---|---|---|
| `portfolio://summary` | `application/json` | Same as [`get_account_summary`](#get_account_summary) |

## Prompt

| Name | Arguments | Description |
|---|---|---|
| `daily_briefing` | none | Asks the model to call `get_portfolio_update` and write a briefing of under 200 words, with no buy/sell advice |

---

## Errors

| Message starts with | Cause |
|---|---|
| `Missing configuration` | `T212_API_KEY` is not set |
| `Trading 212 rejected the credentials` | HTTP 401: wrong key or secret, or the key belongs to the other environment |
| `The API key lacks permission` | HTTP 403: the key is missing a read scope |
| `Trading 212 rate limit hit` | HTTP 429, still limited after one automatic retry |
| `Could not reach Trading 212` | Network error |
| `Not found` / `Trading 212 returned HTTP` | Other API errors |
| `No open position matching` | `get_position` with an unknown ticker |
| `No look-through data yet` | `get_etf_exposure` before any refresh |
| `Need at least two daily look-through snapshots` | `get_exposure_changes` with fewer than two refreshes |

---

## Command line

```
t212-mcp [serve] [--http] [--host HOST] [--port PORT]
t212-mcp refresh-holdings [--allow-agent]
t212-mcp send-digest [--preview FILE.html] [--only-at-local-hour H] [--tz ZONE]
t212-mcp install-schedule [--at HH:MM]
t212-mcp uninstall-schedule
```

| Command | Description |
|---|---|
| `serve` (default) | Run the MCP server over stdio, or over HTTP with `--http` (default `127.0.0.1:8765`). A non-local `--host` requires `T212_MCP_AUTH_TOKEN` |
| `refresh-holdings` | Same as `refresh_etf_holdings`; prints the per-fund results |
| `send-digest` | Email the morning digest via Resend (see the README). `--preview` writes it locally instead of sending; `--only-at-local-hour 7 --tz Europe/London` exits without sending unless it's 07:00 there |
| `install-schedule` | macOS: daily `refresh-holdings --allow-agent` via launchd (default 07:30), logging to `~/.t212_mcp/refresh.log` |
| `uninstall-schedule` | Remove the launchd job |

## Configuration

Read from the environment or `.env`. All names have the `T212_` prefix except `OPENAI_API_KEY`.

| Variable | Default | Description |
|---|---|---|
| `T212_API_KEY` | required | Trading 212 API key |
| `T212_API_SECRET` | none | API secret (older keys work without one) |
| `T212_ENV` | `live` | `live` or `demo` |
| `T212_DATABASE_URL` | `postgresql+psycopg://t212:t212@localhost:5432/t212` | Postgres connection |
| `T212_GITHUB_CLIENT_ID` | none | GitHub OAuth App client ID; turns on GitHub sign-in |
| `T212_GITHUB_CLIENT_SECRET` | none | GitHub OAuth App client secret |
| `T212_ALLOWED_GITHUB_USERS` | none | Comma-separated GitHub usernames or numeric user IDs allowed in (required with GitHub sign-in) |
| `T212_PUBLIC_URL` | `https://$RAILWAY_PUBLIC_DOMAIN` | The server's public base URL, used for OAuth redirects |
| `T212_MCP_AUTH_TOKEN` | none | Static bearer token for HTTP (ignored when GitHub sign-in is configured) |
| `PORT` | `8765` | HTTP port (set by Railway) |
| `T212_RESEND_API_KEY` | none | Resend API key for `send-digest` |
| `T212_DIGEST_TO` | none | Digest recipient(s), comma-separated |
| `T212_DIGEST_FROM` | `Portfolio digest <onboarding@resend.dev>` | Digest sender; use an address on a domain verified in Resend |
| `OPENAI_API_KEY` | none | For the discovery agent |
| `T212_LLM_MODEL` | `gpt-6.1-sol` | OpenAI model |
| `T212_REASONING_EFFORT` | `high` | Reasoning effort sent to OpenAI (empty for the model's default) |
| `T212_LLM_KWARGS` | `{}` | Extra `ChatOpenAI` arguments, as JSON |
| `T212_SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo`, `tavily` or `brave` |
| `T212_AGENT_TOKEN_BUDGET` | `400000` | Token cap per fund |
| `T212_AGENT_RECURSION_LIMIT` | `40` | Maximum agent steps per discovery |
| `T212_MAX_REPAIR_ATTEMPTS` | `3` | Draft/repair attempts per fund |
| `T212_MAX_AGENT_RUNS_PER_DAY` | `5` | Agent runs per day |
| `T212_DATA_DIR` | `~/.t212_mcp` | Where the scheduled job writes its log |
