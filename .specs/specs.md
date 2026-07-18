# Project Hermes — Signal-Driven Options Trading Agent
### Technical Specification v1.0

**Purpose of this document:** feed this to your dev LLM (or use it yourself) as the source of truth for building Hermes on your VPS. It defines scope, architecture, data contracts, security controls, and open decisions. Sections marked **[DECISION NEEDED]** must be resolved by you (the account owner) before implementation — they involve risk limits or legal/ToS judgment calls the LLM shouldn't make on its own.

---

## 0. Ground-Truth Constraints (verified July 2026)

These are facts, not assumptions — build against them:

1. **Robinhood Agentic Trading** connects via an MCP server to a dedicated, isolated agentic account funded separately from your main portfolio. Every transaction triggers a push notification, and you can disconnect the agent at any time.
2. **At beta launch, Agentic Trading supports equities only.** Options, crypto, futures, and event contracts are on Robinhood's roadmap but not confirmed live. **[DECISION NEEDED]: Confirm directly in your Robinhood account whether options order placement is available via the MCP server before writing the execution module.** If not yet available, Hermes must run in **paper-trading mode only** for the options flow until it is.
3. **X (Twitter) API Constraints**: Twitter API v2 Basic tier costs $100/month and allows 10,000 reads/month. Your polling schedule (Sec 2.1) generates exactly 480 API calls/day (9,600/month assuming 20 trading days). This fits neatly into the Basic tier limit, leaving a small buffer of 400 calls. The dashboard must track the pull count against this 10,000 monthly limit so you don't unexpectedly hit the hard cap.
4. Robinhood's own agentic trading disclosures state plainly: this involves real risk of total loss of the funds in that account, AI agents can misinterpret instructions, and you are responsible for every trade the agent places. Nothing in this spec changes that. Hermes should be built to minimize downside from bad parses or bad data, not to remove your oversight.

---

## 1. System Overview

Hermes is a self-hosted agent running on your VPS that:

1. Polls a single X account (`@kttechprivate`) on a configurable cadence, looking for `#ALERT` posts.
2. Parses each alert into a structured trade signal (ticker, expiry, strike, type, action, price, sizing notes).
3. Applies a price-tolerance rule to decide whether to place the trade.
4. Executes (or paper-executes) the trade via Robinhood's Agentic Trading MCP.
5. Places a limit sell order at a configurable profit target (default 20%) and monitors it.
6. Sends a Discord notification at each meaningful event (alert seen, trade decision, trade executed/skipped, limit sell filled).
7. Logs everything to a database and exposes it via a secure, externally-accessible web dashboard.
8. Can be killed instantly, globally or per-component, at any time.

### 1.1 High-Level Architecture

```
+------------------+     +-------------------+     +--------------------+
|  Poller          |---->|  Alert Parser      |---->|  Decision Engine    |
|  (X API client)  |     |  (regex-based,     |     |  (price tolerance,  |
|  cadence-aware   |     |   zero-latency)    |     |   position sizing)  |
+------------------+     +-------------------+     +----------+---------+
                                                                |
                    +-------------------------------------------+------------+
                    v                                           v            |
          +-------------------+                       +--------------------+ |
          | Discord Notifier   |                       | Trade Executor      | |
          | (alert + trade     |<----------------------| (Robinhood MCP,     | |
          |  events)           |                       |  paper mode flag)   | |
          +-------------------+                       +----------+---------+ |
                                                                   |          |
                                                        +----------+---------+
                                                        | Limit Sell Monitor  |
                                                        | (tracks open sell   |
                                                        |  orders, logs fills)|
                                                        +----------+---------+
                                                                   |          |
                                                                   v          |
                                                         +--------------------+
                                                         |  Trade/Event Log   |
                                                         |  (SQLite)          |<-+
                                                         +----------+---------+
                                                                    |
                                                                    v
                                                         +--------------------+
                                                         |  Web Dashboard      |
                                                         |  (read-only,        |
                                                         |   Cloudflare Tunnel)|
                                                         +--------------------+

          +----------------------------------------------------------+
          |  Kill Switch / Control Plane (highest priority path)      |
          |  -- can halt Poller, Executor, or entire system            |
          +----------------------------------------------------------+
```

### 1.2 Deployment Model

- Each box above is a separate systemd service (or Docker container) — not one monolithic script. This is what makes it "modular": you can kill the Executor while leaving the Poller/Notifier running, for example.
- Shared state (signals, decisions, trades, config) lives in a single database, not in-memory, so services can restart independently without losing state.
- A lightweight message queue or simply a shared DB table with a `status` column is enough to connect the pipeline stages — no need for Kafka/RabbitMQ at this scale.

---

## 2. Component 1 — Alert Poller

### 2.1 Polling Schedule (configurable, not hardcoded)

| Window | Cadence |
|---|---|
| 09:10–09:40 ET | every 15 seconds |
| 15:30–16:15 ET | every 15 seconds |
| 09:00–09:10, 09:40–15:30 ET | every 120 seconds |
| Outside 09:00–16:15 ET | idle (no polling) |

All times are **US Eastern (ET)**. The poller must use a timezone-aware clock (e.g. `pytz` / `zoneinfo` with `America/New_York`) — not the VPS system clock, which may be UTC. The config specifies the timezone explicitly.

All four windows and both cadences must be defined in a config file (Section 8), not in code, including the ability to add/remove windows.

**Market holidays**: The poller should skip US stock market holidays (NYSE calendar). Maintain a configurable holiday list or use a library like `exchange_calendars` to avoid wasting API calls on days the market is closed.

### 2.2 Fetch Logic

- Use the X API's user-timeline endpoint scoped to the single account, `since_id` cursor-based, so each poll only pulls new tweets rather than re-fetching the last N.
- Persist the last-seen tweet ID to disk/DB so a service restart doesn't reprocess or skip tweets.
- Track and log every API call (timestamp, endpoint) to the DB, per your requirement to see "twitter API calls" in the dashboard.
- **Quota guardrail:** Track the monthly pull count. If it approaches the 10,000 limit, alert you via Discord and optionally degrade the polling cadence to avoid hitting the hard cap before month-end.

### 2.3 Filtering

- Only process tweets containing `#ALERT` (case-insensitive).
- Ignore retweets/quote-tweets unless explicitly enabled in config.
- Deduplicate: if a poll overlaps and returns a tweet already logged, skip it (idempotent by tweet ID).

---

## 3. Component 2 — Alert Parser

### 3.1 Fields to extract, based on your sample tweets

| Field | Example | Notes |
|---|---|---|
| `action` | `BTO` (new position) or `ADD` (averaging in) | Detect `(ADD)` in header line |
| `ticker` | `SPY`, `QQQ` | |
| `expiry` | `7/15` | Normalize to ISO date; must infer year |
| `strike` | `752`, `754` | |
| `option_type` | `C` (call) / `P` (put) | |
| `price` | `1.81`, or `AVG: .98` for adds | `AVG:` line replaces the plain price line on ADD alerts |
| `trade_style` | `0DTE`, `SWING`, `DAYTRADE`, `LOTTO` | Free-text tags in the body — used for logging/labeling only, not decisioning, unless you want it to affect sizing later |
| `raw_text` | full tweet text | Always store the raw text alongside parsed fields for audit/debugging |

### 3.2 Parsing Approach (Zero-Latency Regex — No LLM Required)

The signal format is highly structured and consistent across all six sample tweets. **Pure Python regular expressions** are the correct approach here — they parse in microseconds with zero external dependencies, no API cost, and no latency. Do not use an LLM for parsing.

1. **Detect Action**:
   - `BTO` (new position): Look for `BTO\s+\$([A-Z]+)`
   - `ADD` (averaging in): Look for `#ALERT\s*\(ADD\)` in the header line
2. **Extract Contract Details**: 
   - Regex: `\$([A-Z]+)\s+(\d{1,2}/\d{1,2})\s+(\d+\.?\d*)([CP])`
   - Captures: Ticker (`SPY`), Expiry (`7/15`), Strike (`752`), Type (`C`).
3. **Extract Price**:
   - For `BTO`: The price appears on its own line after the contract line. Regex: `^\s*(\d*\.?\d+)\s*$` (multiline mode). Note the `\d*` — prices like `.98` have no leading zero.
   - For `ADD`: Extract from `AVG:` line. Regex: `AVG:\s*(\d*\.?\d+)` — handles both `1.08` and `.98` formats cleanly.
4. **Extract Trade Style** (for logging only):
   - Scan the body for keywords: `0DTE`, `SWING`, `DAYTRADE`, `LOTTO`. These are informational tags, not used for trade decisions.
5. **Year Inference for Expiry**:
   - The tweet only gives `MM/DD`. Infer the year: if the date has already passed this year, it's next year. For `0DTE` alerts, the expiry must equal today's date — validate this.
6. **Validation & Fallback**: If the regex fails to extract all essential fields (action, ticker, expiry, strike, option_type, price), the parser must:
   - Log the raw tweet and the failure reason.
   - Send a Discord notification tagged `REVIEW NEEDED`.
   - **Never** pass an incomplete parse to the Decision Engine.

**Unit tests**: Include a test suite with all six sample tweets above (plus edge cases like missing price, malformed ticker, PUT options) to validate the parser before deployment. This is cheap insurance against regex bugs.

### 3.3 Handling ADD Alerts

- An `(ADD)` alert should be matched to the most recent open position (same ticker+expiry+strike+type) in the trade log, not just appended blindly.
- If no matching open position exists in the log (e.g. Hermes missed the original due to downtime), treat as a **new position** but flag it in the log as `add_without_parent` for your review.

---

## 4. Component 3 — Decision Engine (Price Tolerance & Sizing)

This encodes the rule you described:

```
recommended_price = alert.recommended_price   # e.g. 1.81 for BTO, or the extracted AVG price for ADD

if market_ask <= recommended_price:
    -> BUY 1 contract
elif market_ask <= recommended_price * 1.10:
    -> BUY 1 contract   # up to 10% above recommended
else:
    -> SKIP, log as "price_exceeded_tolerance" (more than 10% higher)
```

**Take Profit (Limit Sell) Logic**:
- Once a buy order (BTO or ADD) is filled, the engine must immediately calculate the target sell price: `target_sell_price = fill_price * (1 + take_profit_pct / 100)` (default 20% profit).
- For `ADD` alerts: Calculate the *new* volume-weighted average cost across all contracts in the position, then set the new `target_sell_price = new_average_cost * (1 + take_profit_pct / 100)`.
- **Rounding Requirement (Tick Size)**: Robinhood will reject options orders with invalid tick sizes (e.g., $1.215). The engine MUST round the `target_sell_price` to exactly 2 decimal places (penny increments for SPY/QQQ).
- **No Automated Stop Loss**: The signals explicitly state "expecting 0" (max loss is 100% of premium). No stop-loss orders should be placed; the system relies entirely on the limit sell.

**Limit Sell Order Monitoring**:
- The Executor (or a dedicated monitor loop) must check open limit sell orders during the active polling windows.
- When a limit sell fills, log the realized P/L, update the position status to `closed`, and send a Discord notification.
- **0DTE End of Day Rule**: If a limit sell order for a `0DTE` option is still open at a configurable cutoff time (default 15:50 ET), the Executor must cancel the limit sell and immediately execute a **Market Sell** to salvage any remaining premium.

Configurable parameters (Section 8), not hardcoded:
- `price_tolerance_pct` (default 10%)
- `take_profit_pct` (default 20%) — target profit for the limit sell order.
- `contracts_per_signal` (default 1) — Defaults to 1 for both BTO and ADD based on your specifications, but kept configurable.
- `max_open_positions` — cap on simultaneous option positions
- `max_daily_spend` — hard dollar ceiling across all trades in a day
- `per_trade_max_spend` — sanity ceiling per single order

The Decision Engine must fetch a live quote (bid/ask) from Robinhood before comparing to `recommended_price` — do not trust a stale or cached price for this comparison, since the whole rule is price-sensitive.

Every decision (buy / skip / needs-review) is logged with the reasoning fields (recommended price, observed price, tolerance band, resulting action) so the dashboard can show *why* a trade was or wasn't made — this is what you asked for as "profit/loss, which trades" transparency.

---

## 5. Component 4 — Trade Executor

- Talks to Robinhood exclusively through the official Agentic Trading MCP connection to the dedicated agentic account — no scraping, no unofficial/reverse-engineered API libraries. This keeps you inside Robinhood's supported integration path and its account-level safety controls (isolated funds, kill switch, trade notifications) rather than outside them.
- **Paper mode**: a global config flag that, when on, runs the entire pipeline identically but logs simulated fills (using the live quote at decision time) instead of calling the real order-placement tool. Default this to **on** for a new deployment. You flip it off deliberately.
- **Options availability check**: before attempting a live options order, the Executor should verify the capability exists on your account (see Section 0.2) and fail loudly/safely into paper mode if not, rather than erroring silently.
- **Order confirmation & Take Profit**: After a buy order is submitted, the Executor must confirm the fill status and `fill_price`. Immediately upon fill, it must place a **Limit Sell** order for the filled quantity at `target_sell_price`. Use `GTC` (Good Till Cancelled) for SWING trades, and `Day` for 0DTE/DAYTRADE. **Retry logic**: If placing the limit sell fails (e.g., API timeout), it must retry with exponential backoff to avoid leaving a position unprotected.
- **Handling ADDs for Limit Sells**: If this was an ADD, the Executor must first **cancel** the existing limit sell order for that position, recalculate the volume-weighted average cost, and place a **new** limit sell order for the *total* quantity at the updated target price.
- **Limit sell monitoring**: During active polling windows, check the status of all open limit sell orders. When filled, log realized P/L and notify via Discord. **In paper mode**: the monitor must poll the live market bid price and simulate a fill when the bid crosses the `target_sell_price`.
- Idempotency: each decision should carry a unique ID; the Executor must not double-submit if retried after a timeout.

---

## 6. Component 5 — Discord Notifications

Send to your `@benkiproject` Discord (either a channel named `benkiproject` or tagging the user/role) via a Discord webhook. This requires no bot hosting. Include these events, each with a distinct, greppable prefix:

| Event | Example message |
|---|---|
| Alert detected | `ALERT — BTO $SPY 7/15 752C @ 1.81 (SWING)` |
| Add detected | `ADD — $SPY 7/15 754C AVG .98` |
| Parse needs review | `REVIEW NEEDED — couldn't confidently parse tweet: "..."` |
| Trade executed (live) | `EXECUTED — 1x $SPY 754C @ 1.02 (paper: false)` |
| Trade executed (paper) | `PAPER TRADE — 1x $SPY 754C @ 1.02` |
| Limit Sell Placed | `LIMIT SELL PLACED — 1x $SPY 754C @ 1.22 (20% target)` |
| Limit Sell Filled | `SOLD — 1x $SPY 754C @ 1.22 — P/L: +$0.21 (+20.8%)` |
| Trade skipped | `SKIPPED — ask 2.15 exceeds 1.81 +10% tolerance` |
| Kill switch engaged | `KILL SWITCH ACTIVE — all trading halted` |
| System error | `ERROR — [component] — [message]` |

Config: webhook URL, per-event-type on/off toggles, and a rate limit (so a burst of adds doesn't spam the channel).

---

## 7. Component 6 — Logging & Web Dashboard

### 7.1 Data Model (minimum tables)

- `alerts` — raw tweet, parsed fields, parse status (success/needs_review), timestamp
- `decisions` — link to alert, recommended price, observed price, action taken, reasoning
- `trades` — link to decision, paper/live flag, buy order id, fill price, quantity, status (open/closed/expired)
- `positions` — groups related trades (BTO + ADDs) for the same contract; tracks total quantity, avg cost, current P/L status
- `limit_orders` — sell order id, link to position, target price, status (pending/filled/cancelled), fill timestamp, realized P/L
- `api_calls` — service (X / Robinhood), endpoint, timestamp
- `system_events` — kill switch toggles, config changes, errors, service restarts

### 7.2 Dashboard Views

- **Live feed** — chronological stream of alerts → decisions → trades, most recent first
- **Positions** — open positions, unrealized P/L (needs a periodic price-refresh job)
- **Trade history** — closed trades, realized P/L, win rate, filterable by ticker/date/paper-vs-live
- **Cost & Quota tracking** — X API pulls used vs monthly limit. No LLM token tracking needed (parsing is regex-based).
- **System health** — poller last-run timestamp per window, kill switch status, service uptime

### 7.3 Access & External Exposure

- Dashboard is a **read-only** view of the DB — no trade-triggering controls in the web UI itself, to avoid it becoming a second attack surface for accidental live trades. Kill switch lives in its own control path (Section 9), not the dashboard.
- The dashboard must be **secure and accessible from outside the network** (per requirements). Recommended approach:
  1. Dashboard binds to `127.0.0.1` only (never `0.0.0.0`).
  2. Use **Cloudflare Tunnel** (`cloudflared`) to expose it externally via a subdomain (e.g. `hermes.yourdomain.com`). This avoids opening firewall ports and provides automatic HTTPS.
  3. Add **Cloudflare Access** (zero-trust) as the auth layer — email OTP or SSO, no passwords to manage.
  4. Alternatively, if you don't have a domain: use HTTP Basic Auth over a WireGuard/Tailscale VPN.
- Do **not** expose the dashboard port directly via VPS firewall rules — no open ports beyond SSH.

---

## 8. Configuration System

Single YAML (or `.env` + YAML) file, hot-reloadable where safe, version-controlled separately from secrets:

```yaml
polling:
  timezone: "America/New_York"
  windows:
    - {start: "09:10", end: "09:40", interval_sec: 15}
    - {start: "15:30", end: "16:15", interval_sec: 15}
    - {start: "09:00", end: "09:10", interval_sec: 120}
    - {start: "09:40", end: "15:30", interval_sec: 120}
  target_account: "kttechprivate"
  monthly_api_call_ceiling: 10000  # or whatever your tier allows
  skip_market_holidays: true

decision:
  price_tolerance_pct: 10
  take_profit_pct: 20
  contracts_per_signal: 1
  max_open_positions: 10
  max_daily_spend_usd: 500
  per_trade_max_spend_usd: 500

execution:
  paper_mode: true
  robinhood_account_id: <agentic account id, from secrets>
  error_circuit_breaker_count: 3
  zero_dte_market_sell_cutoff: "15:50"  # Time in ET to cancel limit sell and market sell

notifications:
  discord_webhook_url: <from secrets>
  events_enabled: [alert, add, review, executed, paper, skipped, limit_sell_placed, limit_sell_filled, kill_switch, error]

dashboard:
  bind_address: "127.0.0.1"
  port: 8420
  # External access via Cloudflare Tunnel (see Section 7.3)
```

Secrets (X API keys, Discord webhook URL, Robinhood credentials/tokens) live in a separate `.env` file with `chmod 600` — **never** in the YAML that might get committed to git or pasted into an LLM chat.

---

## 9. Security & Kill Switch Design

Treat this section as non-negotiable regardless of how the rest gets built.

### 9.1 Kill Switches (multiple, redundant, independent of each other)

1. **Global halt file** — the simplest and most robust: every service checks for the existence of a file (e.g. `/opt/hermes/HALT`) at the top of every loop iteration and before every trade submission. Creating that file with `touch` from any SSH session instantly stops everything, even if the dashboard or Discord bot is itself broken. This should be your primary, always-available switch.
2. **Discord kill command** — a slash command or a specific message in the channel (e.g. `!hermes halt`) that the notifier service watches for and translates into creating the halt file. Convenient, but treat it as secondary to #1 since it depends on more moving parts (bot connectivity, Discord uptime).
3. **Robinhood-side disconnect** — Robinhood's own agentic trading product includes an account-level disconnect/pause control as a third, independent layer outside Hermes entirely — worth knowing that even if your VPS is fully compromised, you can cut Hermes off from your Robinhood funds directly in the Robinhood app.
4. **Granular halts** — separate flags for "stop new trades" vs "stop polling" vs "stop everything," since e.g. you might want to keep watching for alerts and logging without letting anything execute.

### 9.2 Access Controls

- SSH: key-based auth only, disable password auth once the box is set up (you mentioned a root password — rotate to key-based access and disable root SSH login entirely, use a sudo user instead).
- Principle of least privilege: the Executor service's Robinhood credentials should only ever touch the dedicated agentic account, never your main brokerage account.
- Secrets stored with restrictive file permissions (`600`), owned by the service user, not root, not world-readable.
- Dashboard behind auth (Section 7.3).

### 9.3 Guardrails Against Runaway Behavior

- `max_daily_spend_usd` and `max_open_positions` (Section 8) as hard circuit breakers enforced in code, independent of what any single signal says.
- Duplicate-alert protection (Section 2.3) so a retried poll can't double-buy.
- If the Executor throws more than N errors in a row (config: `error_circuit_breaker_count`), auto-engage the halt file and alert you — don't let a bug loop into repeated bad orders.
- All config changes and halts are logged to `system_events` with timestamp, so you have an audit trail of who/what changed behavior and when.

### 9.4 Legal/ToS Notes — **[DECISION NEEDED, not something the LLM should resolve for you]**

- Confirm your use of the X API for this purpose complies with X's Developer Agreement (automated polling of a single account for a personal tool is generally within normal API use, but verify current terms — especially around redistributing tweet content; the Discord notification should paraphrase/summarize rather than republish the tweet verbatim if that's a concern for you).
- Confirm the `@kttechprivate` account's own terms (if any) around automated consumption/redistribution of their paid signal content.
- Read Robinhood's agentic trading terms in full before enabling live mode — you remain responsible for every trade the agent places, regardless of automation.

---

## 10. Build Phases (suggested order for your dev LLM)

1. **Phase 0** — Config system + halt-file kill switch + logging DB schema + parser unit tests (foundation everything else depends on)
2. **Phase 1** — Poller + Parser, paper-mode only, Discord notifications for alerts (no trading yet) — validate parsing accuracy against real tweets for a few days before touching money
3. **Phase 2** — Decision Engine + paper-trade Executor + limit sell simulation — validate the price-tolerance logic, take-profit logic, and position tracking against real signals, still no live orders
4. **Phase 3** — Dashboard (read-only views of Phase 1–2 data) + Cloudflare Tunnel setup for external access
5. **Phase 4** — Live Executor behind explicit `paper_mode: false` flag, starting with `max_daily_spend_usd` set very low, raised deliberately over time
6. **Phase 5** — Hardening: circuit breakers, error alerting, granular kill switches, SSH lockdown, VPS firewall rules

Recommend running Phase 1–3 for at least a week or two of live market hours before flipping `paper_mode: false`, so you have real parse-accuracy and decision-accuracy data before any real money is at risk.

---

## 11. Final Pre-Flight Checklist (before dev starts)

- [ ] Confirm options trading is actually enabled on your Robinhood agentic account via MCP (if not, Phase 1–3 run paper-only)
- [ ] Confirm your Twitter API Basic tier is active (the 480 calls/day fits the 10K/month limit cleanly)
- [ ] Set up Cloudflare Tunnel (or alternative) for dashboard external access
- [ ] Review X API ToS and the signal provider's own terms around automated use