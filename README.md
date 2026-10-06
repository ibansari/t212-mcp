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

The server refuses to listen on a non-local address without one of these:

- **Sign-in with WorkOS AuthKit** (multi-user, recommended for anything public): each person signs in and sees only their own Trading 212 account. See [Multiple users](#multiple-users).
- **A static bearer token** (single-user): set `T212_MCP_AUTH_TOKEN`, and every request needs `Authorization: Bearer <token>`.

Whichever you use, create Trading 212 keys with read scopes only. Then even a leaked credential can't place orders or move money.

## Multiple users

Anyone you let sign in can connect their own Trading 212 account and ask Claude about it. Fund holdings, recipes and company matching are shared, so a fund researched once serves everyone. Each person's portfolio, snapshots and look-through history are private to them.

**Sign-in and key storage are provided by [WorkOS](https://workos.com):**

- **Claude's sign-in:** AuthKit hosts the login and registers Claude's connector automatically. This server only checks the AuthKit-issued tokens.
- **The account page** (`/account`): people sign in with AuthKit and paste a read-only Trading 212 key. The key is checked against Trading 212, then stored encrypted in **WorkOS Vault**. The database keeps only a reference to it.
- **Who can sign up** (open, invite-only, or particular sign-in methods) is set in the WorkOS dashboard, not in code.

**Setup:**

1. In the [WorkOS dashboard](https://dashboard.workos.com):
   1. Turn on AuthKit and **Dynamic Client Registration** (Applications → Configuration).
   2. Add the redirect URI `https://<your-domain>/account/callback`.
   3. Add `https://<your-domain>/mcp` as a **resource indicator**, so tokens are issued for this server.
   4. Note your AuthKit domain, API key and client ID.
2. Set these variables:

   | Variable | Value |
   |---|---|
   | `T212_AUTHKIT_DOMAIN` | e.g. `https://your-app.authkit.app` |
   | `T212_WORKOS_API_KEY` | WorkOS API key (`sk_...`). Used for the account page's sign-in and for Vault |
   | `T212_WORKOS_CLIENT_ID` | WorkOS client ID (`client_...`) |
   | `T212_WORKOS_COOKIE_PASSWORD` | Seals the account page's session cookie. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
   | `T212_OWNER_EMAIL` | Your sign-in email. You keep the server's own key (`T212_API_KEY`), your existing data, the digest and admin rights |
   | `T212_ADMIN_EMAILS` | Optional. Others who may use the discovery agent and see every agent trace |
   | `T212_PUBLIC_URL` | `https://<your-domain>`. Not needed on Railway, which supplies the domain |

3. If the database predates multi-user support, run `t212-mcp upgrade-multiuser` once. It assigns existing personal data to you and removes the old sign-in session table.
4. Each person, including you, goes to `https://<your-domain>/account`, signs in and connects a key. You use the server's key. Then they add the connector in Claude: `https://<your-domain>/mcp`.

**Guardrails:**

- **The discovery agent spends OpenAI credit, and it's on by default for everyone.** It's capped at 400k tokens per fund (`T212_AGENT_TOKEN_BUDGET`) and 5 agent runs a day across all users (`T212_MAX_AGENT_RUNS_PER_DAY`). Turn it off with `T212_AGENT_ENABLED=false`. Admins can additionally read every agent trace.
- **Refresh cooldown:** non-admins can start one refresh every 10 minutes.
- **Only one refresh runs at a time,** across all users and services, enforced by a database lock.
- **Privacy:** `get_holdings_status` and `get_agent_trace` only show funds the caller holds.
- **Deleting data:** on the account page, people can delete their key and history.

Holding other people's API keys carries responsibility: check Trading 212's API terms before inviting anyone.

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
| `refresh_etf_holdings` | Starts a background re-download of fund holdings; funds without a working recipe are researched by the LLM agent (on by default; `allow_agent=false` to skip). Follow progress with `get_holdings_status` |

The server also provides the resource `portfolio://summary` and the prompt `daily_briefing`.

## ETF look-through

The look-through tools read data saved in the database by a refresh. Each fund's holdings are fetched by a *recipe* that says where the issuer publishes the holdings file and how to parse it. Refreshing with saved recipes needs no LLM.

When a fund has no recipe, or its recipe stops working, a discovery agent can search the issuer's site and write or repair one. The agent uses OpenAI: set `OPENAI_API_KEY` in `.env`, and optionally `T212_LLM_MODEL` (default `gpt-6.1-sol`) and `T212_REASONING_EFFORT` (default `high`). The agent only runs when you allow it, and it is capped at `T212_MAX_AGENT_RUNS_PER_DAY` runs per day (default 5).

For issuers whose source is already known (`src/t212_mcp/lookthrough/known_sources.py`, currently HSBC and Invesco), a fund with no saved recipe is tried against that source first. If it works, it's saved as a recipe and no LLM is needed. The agent also gets the known source as a starting hint when researching.

**Companies, not just ISINs.** A company can appear under several ISINs: share classes (Alphabet A/C), preference shares, and home listings vs ADRs/GDRs (TSMC in Taipei and New York). After each refresh, securities making up at least 0.01% of the portfolio are grouped into companies, in three stages:

1. **[OpenFIGI](https://www.openfigi.com):** identifies each ISIN (name, security type, exchange). Free, and cached per ISIN.
2. **Rules:** names that match once class and listing markers are removed (CL A, SP ADR, PREF, GDR 144A) are treated as one company.
3. **An agent:** settles near-matches, such as "Taiwan Semiconductor Manufac" vs "Taiwan Semiconductor-SP ADR". It runs by default (turn it off with `T212_AGENT_ENABLED=false`), uses the OpenFIGI evidence plus search tools, and returns a structured decision. Its decisions are cached in `security_entities` and traced like the discovery agent.

Separately listed affiliates stay separate, e.g. Samsung Electronics vs Samsung Electro-Mechanics, or Merck & Co vs Merck KGaA.

**What the agent did:**
- **Railway logs:** every agent step logs one line, with the tool and its main argument, a short result, and tokens used, plus a running total against the cap.
- **Postgres:** the full run tree, with inputs, outputs, tokens, timings and errors, is stored in `agent_traces`. Ask Claude for `get_agent_trace` on a ticker to see why a fund failed.

When a recipe fails, the error shows what the source actually contains: its available columns and first data row, its first rows when the header row can't be found, the lists inside a JSON response, or the text of an HTML page served where a file was expected. The repair step uses this to fix the recipe from evidence, or researches again when the source has moved or is blocked.

```bash
uv run t212-mcp refresh-holdings                 # re-run saved recipes only
uv run t212-mcp refresh-holdings --no-agent      # saved recipes only, no OpenAI spend (the agent is on by default)
uv run t212-mcp install-schedule --at 07:30      # macOS: daily refresh (with the agent) via launchd
uv run t212-mcp uninstall-schedule
```

The scheduled job logs to `~/.t212_mcp/refresh.log`.

## Morning digest

`t212-mcp send-digest` emails a portfolio summary:
- **Totals:** total value and the change since the last digest.
- **Chart and table:** how each holding moved. The change is the price move applied to the shares you hold now, so buying or selling shows as a note, not as a gain or loss.
- **Headlines:** from the last 24 hours, for your 10 largest positions plus any company that makes up more than 5% of the portfolio once ETFs are looked through.

Headlines come from Google News RSS, with no API key and no LLM. Look-through companies only appear once a look-through refresh has run.

Email goes through [Resend](https://resend.com). Set these:

| Variable | Value |
|---|---|
| `T212_RESEND_API_KEY` | Your Resend API key |
| `T212_DIGEST_TO` | Recipient. Comma-separate several |
| `T212_DIGEST_FROM` | Optional. Defaults to `onboarding@resend.dev`, which can only send to your Resend sign-up address. Verify a domain in Resend to send anywhere else |

```bash
uv run t212-mcp send-digest --preview digest.html   # write the email locally (plus digest.png) instead of sending
uv run t212-mcp send-digest                         # send now
```

**On Railway** a separate `digest` service runs it, from the same repo and image. Its start command is `t212-mcp send-digest --only-at-local-hour 7 --tz Europe/London` and its cron schedule is `0 6,7 * * 1-5`; both are set in the service settings.
- It's scheduled for 06:00 and 07:00 UTC on weekdays, and `--only-at-local-hour 7` sends only when it's 07:00 in London. That keeps the email at 07:00 UK time on both sides of the clock change.
- Changes are measured against a fixed daily point. The run at the set time (`T212_DIGEST_HOUR`, default 7, in `T212_DIGEST_TZ`, default Europe/London) saves a baseline of your portfolio and look-through. Every email compares with the previous day's baseline, so the 07:00 email shows a clean day-on-day change and manual runs never move the comparison point.
- The baseline is saved only after the email sends. If a set-time run is missed, the next email compares with the last baseline available, and its header says which.

### Railway service settings

Railway's config files (`railway.json`) are deprecated, so each service's settings live in Railway:

| Service | Settings |
|---|---|
| `mcp` | Dockerfile `Dockerfile`, health check `/health` (timeout 120 s), restart on failure (5 retries), public domain |
| `digest` | Dockerfile `Dockerfile`, start command and cron schedule as above, never restart, no domain |
| `Postgres` | Railway's Postgres. Both services use `T212_DATABASE_URL=${{Postgres.DATABASE_URL}}` |

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

## Evals

`evals/` scores the look-through agent. Both tiers need `OPENAI_API_KEY`.

| Tier | What runs | Web | Graded on |
|---|---|---|---|
| `component` | The `draft` and `repair` LLM steps, given a fixed research report or a broken recipe and its real error. Also `resolve`: the entity-resolution agent on real clusters of securities, with no search tools | Offline: issuer files are served from `tests/fixtures/` | Does the recipe the model returns extract holdings that pass validation, with the right size and top holding? For repair, did the model pick the right action (fix / rediscover / give up)? |
| `e2e` | The full discover → draft → test → repair graph for 10 real funds (iShares, Vanguard, Xtrackers, HSBC, Wahed, physical gold), starting from no recipes | Live | Is a recipe saved and validated? Does it hold enough securities, including mega-caps like Apple and TSMC, from the issuer's own site? Tokens, attempts and time |

```bash
uv run python -m evals list                              # all cases
uv run python -m evals component                         # 9 cases x 3 trials
uv run python -m evals component --model gpt-5 --reasoning-effort ""   # compare configs ("" = model default)
uv run python -m evals e2e --case ishares_sp500 --trials 3
uv run python -m evals report                            # compare all saved runs
```

Each run is saved to `evals/results/<run_id>.json` plus a Markdown summary (git-ignored). Use `--fail-under 0.8` to exit non-zero in CI.

- **Component cases** use pinned fixture data, so the same answer always gets the same grade. Only the model's answers vary.
- **E2E runs** use a separate `<database>_eval` database, wiped before every trial, and never touch your real recipes. They need local Postgres and Chromium (`uv run playwright install chromium`). They cost real tokens, so start with one case. Issuer sites change, so expect some failures caused by the sites rather than the agent; the failure text in the report shows which.
- **The harness has its own tests** in `tests/test_evals.py`. They run offline with a stub model.

Component results so far:

| Model | Before error evidence | After |
|---|---|---|
| `gpt-6.1-sol` (high effort, default) | 70% | 100%, ~2.1k tokens and 6 s per call |
| `gpt-5` | 59% | 100%, ~3.2k tokens and 15 s per call |

Both models now pass every component case, so these cases no longer tell models apart. The live `e2e` tier hasn't been run yet.

## Development

```bash
uv run pytest                          # mocked HTTP; needs local Postgres (docker compose up -d db)
uv run fastmcp dev src/t212_mcp/server.py   # MCP Inspector
```

Tests use a `<database>_test` database on the server from `T212_DATABASE_URL` (or set `T212_TEST_DATABASE_URL`). They create it if it's missing and recreate the tables at the start of each run.

Responses are cached briefly to stay within Trading 212's rate limits (for example, the account summary allows 1 request every 5s). If a request is rate-limited (HTTP 429), the client retries it once.
