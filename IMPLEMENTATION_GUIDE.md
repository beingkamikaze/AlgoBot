# TradingBot: Validation and Implementation Guide

Draft 1, 23 October 2026. Scope: Indian markets (NSE intraday equity, index futures
and options), Zerodha Kite for live orders, our own paper-trading engine, Next.js
frontend, Python backend, Supabase (Postgres, pgvector, Auth, Realtime, Storage),
GPT-5 as the language model.

---

## 0. The verdict in one page

**Will it make trading easier? Yes, if we build the right thing.** A system that
scans for setups, sizes every position, enforces your limits, keeps a complete
journal and shows you your own statistics will remove most of the work and most of
the emotional mistakes from trading.

**Will "AI that learns from mistakes and increases accuracy over time" make you
profitable? Not as described, and believing it would is the biggest risk in this
project.** Four reasons:

1. **The base rate is brutal.** SEBI's study of FY25 found about 91 % of
   individual F&O traders lost money, roughly ₹1.06 lakh crore in aggregate. Its
   FY26 study found about 87.7 % still lost money. Most of those people also used
   "proven" indicators.
2. **Indicators are not proven edges.** RSI, MACD, Supertrend, VWAP and the rest
   are public formulas everyone already uses. Any edge they had is mostly priced
   in, and what remains shifts with the market regime. Whether an indicator works
   *for this instrument, this timeframe, these costs and this regime* is something
   we measure, not something we inherit from a successful trader.
3. **RAG is memory, not learning.** Storing trades in a vector database lets the
   model *recall* similar past trades. It does not make the model better at
   predicting prices. With a few hundred trades, any "lesson" the model draws is
   mostly noise. Without statistical checks, it will find patterns that are not
   there and confidently overfit.
4. **"Accuracy" is the wrong target.** A strategy that wins 80 % of the time can
   lose money (small wins, big losses: typical of option selling). A strategy that
   wins 35 % can make money (big wins, small losses: typical of breakouts). What we
   optimise is **expectancy after costs**, with **drawdown** held inside limits.

**So the reframe:** build a **rule-based trading system with an AI analyst on
top**, not an AI that trades.

| Job | Done by | Why |
| --- | --- | --- |
| Signals (when to buy or sell) | Deterministic strategy code | Same input, same output; can be backtested |
| Position size, risk limits, kill switch | Deterministic risk engine | Money decisions must never depend on a model's mood |
| Order placement | Execution engine, after your approval (or tightly-limited automation later) | Regulatory and safety reasons, below |
| Proving a strategy works | Backtester + walk-forward + paper trading | The only honest evidence |
| Journal, post-mortems, finding your mistakes, explaining results, research | GPT-5 with memory (RAG) | What language models are actually good at |
| Changing a strategy | GPT-5 *proposes*, the backtester *tests*, **you** approve | "Learning" that cannot quietly overfit |

That is the version that makes trading easier *and* has a chance of making you
better over time.

### Facts that change the plan

- **GIFT Nifty is probably not tradable for you.** It trades on NSE IX in GIFT
  City, not on NSE, and Zerodha does not offer it. Sources disagree on whether
  resident Indians may trade it at all: the Liberalised Remittance Scheme does not
  appear to allow funding leveraged derivatives there. Confirm with an NSE IX
  broker before planning on it. **What we can do:** use GIFT Nifty's price as a
  pre-market signal for Nifty, which is how most Indian traders use it.
- **Bank Nifty has no weekly expiry.** Since 20 November 2024 SEBI allows weekly
  contracts on one benchmark index per exchange. On NSE that is Nifty 50. Bank
  Nifty is monthly only (last Tuesday), and its lot size is 30 since the January
  2026 contracts. Lot sizes are reviewed periodically, so the app must read them
  from Kite's instrument list every day, never hard-code them.
- **SEBI's retail algo framework (in force since 1 April 2026):**
  - API orders are accepted only from a registered app on a **whitelisted static IP**.
  - A **fresh 2FA login every trading day** (no long-lived tokens).
  - Up to **10 orders per second**; above that the algo needs exchange registration.
  - A self-built algo is for **personal use** (you and immediate family). Selling or
    sharing it with others requires empanelment through a broker, and a "black box"
    strategy requires a SEBI Research Analyst registration.
  - API **market orders without market protection are rejected**: use limit orders,
    or set the `market_protection` parameter in Kite Connect.
- **Costs are a large share of results.** Zerodha brokerage, STT, exchange fees,
  stamp duty and GST are significant, and STT on F&O was raised in October 2024.
  Every backtest and paper trade must charge current costs, read from one config
  that we update when the rates change.

---

## 1. What the product does

### For you, day to day

1. **Before 9:15:** a pre-market brief. GIFT Nifty's move, global cues, FII/DII
   flows, today's results calendar, your watchlist's news, and which of your
   strategies are active for today's regime.
2. **During the session:** live charts with your strategies' signals drawn on them;
   a signal card for each setup ("ORB long RELIANCE: entry 1201.4, stop 1192.6,
   target 1219.0, 49 shares, max loss ₹498 incl. charges, historical expectancy
   +0.18R over 212 trades, similar past trades: 7, of which 3 won").
3. **One click (or one yes) to trade.** The risk engine checks it, then it goes to
   Kite (live) or to the paper engine (mock). Stop placed automatically.
4. **Live position panel:** P&L, distance to stop and target, time to forced exit,
   your daily loss used.
5. **After the close:** every trade gets an automatic post-mortem; the AI analyst
   writes a daily review ("you took 2 trades outside your window; both lost").
6. **Weekly:** a statistics review per strategy, per regime, per time of day, plus
   *proposals* for changes, each with backtest evidence attached.

### Modes

| Mode | Orders go to | Use |
| --- | --- | --- |
| Backtest | History | Prove a strategy before anything else |
| Paper | Our simulator, live prices | Prove it in the present, with realistic fills |
| Live (assisted) | Kite, after your confirm | The default for real money |
| Live (auto) | Kite, no confirm, inside hard limits | Only for strategies that passed every gate, small size, within SEBI's personal-use rules |

---

## 2. Architecture

```
                         ┌──────────────────────────────────────────┐
  Browser  ──HTTPS──▶    │  Next.js (App Router) on Vercel or VPS    │
  (you)     ◀─Realtime─  │  Charts (TradingView Lightweight Charts)  │
                         │  Supabase Auth (session), shadcn/ui       │
                         └───────────────┬──────────────────────────┘
                                         │ REST + WebSocket (JWT from Supabase)
                         ┌───────────────▼──────────────────────────┐
                         │  Python backend: FastAPI (api)            │
                         │   strategies · risk · orders · journal    │
                         ├───────────────────────────────────────────┤
                         │  Workers (same codebase, separate procs)  │
                         │   market-data   KiteTicker websocket      │
                         │   strategy-runner (bar close -> signals)  │
                         │   execution     (OMS, reconciliation)     │
                         │   paper-engine  (simulated fills)         │
                         │   scheduler     (login reminder, 15:15    │
                         │                  flatten, EOD jobs)       │
                         │   ai-analyst    (GPT-5: reviews, RAG)     │
                         └──┬───────────┬──────────────┬────────────┘
                            │           │              │
                 ┌──────────▼──┐  ┌─────▼──────┐  ┌────▼──────────────┐
                 │ Zerodha     │  │ Supabase   │  │ OpenAI API        │
                 │ Kite Connect│  │ Postgres   │  │ GPT-5 + embeddings│
                 │ REST + WS   │  │ pgvector   │  └───────────────────┘
                 │ (static IP) │  │ Realtime   │
                 └─────────────┘  │ Storage    │   TradingView alerts
                                  └────────────┘   ──webhook──▶ backend
```

### Key choices, and why

- **Backend talks to Kite through the official `pykiteconnect` SDK, not the Kite
  MCP server.** MCP is for chatting with Claude or another assistant. A trading
  backend needs the SDK's full order parameters (including `market_protection`),
  the KiteTicker websocket, and its own error handling.
- **Run the backend on a small VPS in Mumbai** (AWS `ap-south-1` with an Elastic IP,
  or a DigitalOcean or Hetzner VPS with a fixed IP). This solves SEBI's static-IP rule,
  gives low latency to NSE, and keeps the system running when your laptop sleeps.
  The frontend can live anywhere.
- **Market data:** Kite's websocket (KiteTicker) for live ticks and Kite's historical
  API for candles. Both need the paid Kite Connect plan (₹500 a month per app at the
  time of writing). Yahoo stays as a free fallback for research only.
- **TradingView:** two different integrations.
  1. *Charts in our app:* TradingView's open-source **Lightweight Charts** library.
  2. *Signals from your TradingView setups:* TradingView **alert webhooks** (needs a
     paid TradingView plan) posted to our backend, verified with a secret, and then
     treated like any other signal: risk check, then your confirm.
  TradingView cannot place Zerodha orders by itself; everything goes through our
  risk engine.
- **Paper trading: our own engine.** Zerodha has no paper account. Our simulator
  fills against live ticks with realistic rules (below), which is better than most
  third-party paper platforms anyway. Other brokers' sandboxes can be added later
  behind the same adapter interface.
- **Supabase:** Postgres for everything structured; `pgvector` for trade memory;
  Auth for login (add TOTP 2FA); Realtime to push positions and signals to the UI;
  Storage for chart snapshots and contract notes; Row Level Security on every table.
- **GPT-5** through the OpenAI API, with structured (JSON-schema) outputs. It never
  calls the order API directly. It writes to the journal and to *proposals*.

### Python stack

| Need | Library |
| --- | --- |
| API | FastAPI, Pydantic v2, Uvicorn |
| Workers and scheduling | `arq` or Celery on Redis; APScheduler for market-clock jobs |
| Broker | `kiteconnect` (official) |
| Data | pandas, polars (fast backtests), numpy |
| Indicators | TA-Lib (C, fast) or `pandas-ta`; our own tested wrappers |
| Backtesting | Our event-driven backtester (shares code with live), plus `vectorbt` for fast parameter research |
| Options maths | `py_vollib` (Black-76 Greeks, implied volatility) |
| Stats | scipy, statsmodels |
| DB | `supabase-py` for auth-aware calls; SQLAlchemy + asyncpg for heavy queries |
| LLM | `openai` SDK |
| Tests | pytest, hypothesis |

### Frontend stack

Next.js (App Router, TypeScript), Tailwind + shadcn/ui, TanStack Query, Zustand for
local state, `@supabase/ssr` for auth, Lightweight Charts, Recharts for statistics.

---

## 3. Modules

### 3.1 Market data

- **Instruments:** download Kite's instrument dump every morning (tokens, lot sizes,
  tick sizes, expiries, strikes). Never hard-code a lot size or expiry.
- **Live:** one KiteTicker connection subscribed to your watchlist, index futures,
  and the options chain strikes you need (ATM ± N). Builds 1-minute and 5-minute
  candles in memory; writes closed candles to Postgres.
- **History:** Kite historical API for backfill (respect its rate limits); store
  candles in a `candles` table partitioned by month, or in Parquet files on disk
  for backtests.
- **Derived data:** VWAP, opening range, previous day high/low, options chain
  snapshots every minute (LTP, OI, IV, Greeks), PCR, max pain, India VIX.
- **Calendar:** NSE holidays (fetched from NSE), expiry calendar from the
  instrument dump, special sessions.
- **Pre-market:** GIFT Nifty (as a signal only), SGX/US futures, FII/DII data (NSE
  publishes it daily).

### 3.2 Indicator library

A thin, tested layer over TA-Lib or pandas-ta, so every strategy calls the same
implementation in backtest, paper and live. Each indicator has:

- one definition (inputs, parameters, warm-up bars needed);
- a unit test against known values;
- **no look-ahead** (only closed bars; tested explicitly).

Starter set: EMA, SMA, VWAP (+ bands), RSI, MACD, Supertrend, ATR, Bollinger Bands,
Keltner, ADX/DMI, Stochastic, OBV, volume profile (POC/VAH/VAL), pivot points
(classic, CPR), opening range, previous-day levels, ORB, implied volatility, IV
rank and percentile, Greeks, OI change, PCR.

### 3.3 Strategy engine

Strategies are **code plus a versioned config**, not prompts:

```python
class Strategy(Protocol):
    id: str
    version: str
    instruments: Selector        # e.g. "NIFTY FUT current month", "NSE EQ liquid"
    timeframe: str               # "5m"
    warmup_bars: int
    def on_bar(self, ctx: BarContext) -> list[Signal]: ...
```

A `Signal` carries: instrument, side, entry type and price, stop, target(s), time
stop, the reason (indicator values at the moment), the strategy version and the
market regime tag. **The same strategy object runs in backtest, paper and live.**
That is the single most important design rule: the code that passed the backtest
is the code that trades.

Parameters live in `strategy_versions` (JSON). A change to a parameter makes a new
version, and a new version starts from zero evidence.

### 3.4 Starter strategy catalogue (to be *tested*, not trusted)

| # | Strategy | Market | Core rule | Known failure mode |
| --- | --- | --- | --- | --- |
| 1 | Opening-range breakout | Liquid stocks, Nifty futures | First 5-min close above the 15-min range; stop at range mid; 2R or time exit | Chop days: many false breaks. Filter by gap, VIX, trend day |
| 2 | VWAP pullback in trend | Stocks, index futures | Price above VWAP and EMA20 > EMA50; buy a pullback to VWAP that holds | Trend days only; dies in ranges |
| 3 | Supertrend + ADX trend follow | Index futures | Enter on Supertrend flip when ADX > 20; trail on Supertrend | Whipsaws at low ADX; late entries |
| 4 | Bollinger / RSI mean reversion | Large-cap stocks | Close outside the lower band, RSI(2) < 10, above 200 EMA on daily | Catching falling knives on news days |
| 5 | Previous-day high/low break with CPR | Index futures | Break of PDH/PDL with narrow CPR | Fails on wide-CPR days |
| 6 | Option buying on index breakout | Nifty weekly options | Strategy 1 or 3 signal on the index, buy ATM/1-strike-ITM option, stop on the *index* level | Theta and IV crush; needs a fast move |
| 7 | Defined-risk option selling | Nifty weekly | Iron condor / short strangle **with hedges** at IV rank > 50, exit at 50 % of credit or 2× loss | Rare large losses wipe out months; never naked |
| 8 | Expiry-day strategies | Nifty | Specific rules for expiry-day gamma | Extremely fast; last to build, smallest size |

Rules for the catalogue:

- Start with **one strategy, one market** (strategy 1 on liquid stocks: we already
  have it in the NSE skills). Add the next only when the first has passed paper
  trading.
- Option strategies come **after** the options data pipeline, Greeks, margin
  calculation (SPAN + exposure, from Kite's margins API) and the expiry calendar
  are in place and tested.
- "Used by successful traders" is a source of *ideas*. Our backtests decide.

### 3.5 Backtester

Event-driven, bar by bar, sharing the strategy, risk and fill code with live. Must have:

- **Realistic costs:** brokerage, STT, exchange, SEBI, stamp duty, GST per segment
  (equity intraday, futures, options), from one versioned `cost_model` config.
- **Slippage:** at least one tick plus a fraction of the bar's range for market
  entries; stop orders fill at the stop *or worse* (gap-through); options use the
  quoted spread.
- **Fill rules:** a limit buy fills only if the low trades *below* the limit (not
  merely touches it); a bar that hits both stop and target counts as the stop.
- **Session rules:** entry window, forced exit time (before Zerodha's 3:25 PM MIS
  square-off), no trading on holidays, circuit limits.
- **Contract roll** for futures; real historical lot sizes; expiry handling.
- **Outputs:** every trade, an equity curve, and the metrics in section 5.

### 3.6 Risk engine (the most important module)

Every order, from any source (strategy, TradingView webhook, manual click, AI), passes here.
Port the checks from our `nse-risk` skill and extend them:

- **Per trade:** stop required; max loss (incl. costs) ≤ limit; reward:risk ≥ minimum;
  size within position cap and cash; no averaging down; lot-size multiples.
- **Per day:** max daily loss (hard stop: no new entries after it); max trades; max
  consecutive losses (e.g. stop after 3); entry window.
- **Per account:** max open positions, max margin used %, max exposure per underlying,
  no naked short options (configurable), max loss per week and month.
- **F&O specific:** margin check through Kite's order-margins API before placing;
  no new positions in the last N minutes before expiry; liquidity check (bid-ask
  spread and OI) for option strikes.
- **Kill switch:** one button (and an automatic trigger on daily-loss breach or a
  broker error storm) that cancels all open orders, squares off all positions, and
  disables trading until you re-enable it the next day.
- **Rate limiting:** stay far below SEBI's 10 orders per second.

### 3.7 Execution (order management)

- An order state machine: `proposed → risk_passed → approved → sent → open →
  (partially_)filled → closed`, or `rejected / cancelled / expired`.
- **Stop management:** after an entry fills, place its stop immediately. If the
  stop is rejected, alert you and exit the position (a position must never be
  unprotected). Use GTT or OCO-style logic where Kite supports it; otherwise our
  engine cancels the other leg when one fills.
- **Reconciliation** every few seconds and at startup: compare our positions with
  Kite's `positions` and `orders`, and alert on any difference.
- **Idempotency:** every order carries our tag/ID; retries never create duplicates.
- **Daily login:** at about 8:45 AM the app notifies you to log in to Kite (SEBI's
  daily 2FA). Until then, live trading is disabled.
- **Forced exit:** 15:15 square-off job for intraday; expiry-day rules for options.

### 3.8 Paper engine

Same interface as the Kite adapter (`place_order`, `cancel_order`, `positions`...),
filling against live ticks:

- limit orders fill when the market trades through the price, with queue-position
  pessimism;
- market and stop orders fill at the next tick plus slippage;
- options fill at the bid (sell) or ask (buy), not the last price;
- real margin blocked, real costs charged;
- it logs exactly the same records as live, so statistics compare like for like.

### 3.9 Journal and trade memory (the RAG part, done properly)

For every trade, mock or real, store three layers:

1. **Structured facts (Postgres):** strategy and version, instrument, entry and exit
   times and prices, size, costs, R multiple, MAE/MFE (worst and best excursion),
   holding time, which rule triggered the exit, all indicator values at entry, the
   regime tags (trend/range, VIX bucket, gap size, day of week, expiry day,
   time of day), news flags, whether every rule was followed.
2. **Context snapshot (Storage):** chart image at entry and exit, options chain
   snapshot for F&O.
3. **Narrative (pgvector):** a short text the AI writes after the trade: setup,
   what happened, what went right or wrong, and *your own notes* (how you felt, why
   you overrode something). Embedded with OpenAI embeddings.

How the AI uses memory:

| Moment | Retrieval | Output |
| --- | --- | --- |
| New signal | SQL: this strategy's stats in this regime. Vector: the 5–10 most similar past trades | A note on the signal card: "Similar setups: 7, 3 won, avg −0.1R. 4 of 7 losers were after 14:00" |
| After each trade | This trade + its rules | Post-mortem, rule-violation tags, mistake category |
| End of day | Today's trades | Daily review, discipline score |
| Weekly | All trades, grouped | Statistics review and **proposals** (below) |

Two rules: **numbers come from SQL, not from the model's reading of embeddings**,
and the model never decides a trade by itself.

### 3.10 The learning loop (how it gets better without fooling itself)

```
trades + journal ─▶ AI analyst finds a pattern ─▶ PROPOSAL (e.g. "skip ORB when gap > 1%")
                                                    │
                     backtest on data the proposal was NOT found in (out-of-sample)
                                                    │  passes significance + cost checks?
                                                    ▼
                         paper-trade the new version for N trades in parallel
                                                    │  still better?
                                                    ▼
                         YOU approve ─▶ new strategy version goes live (small size first)
```

What counts as "better" is fixed in advance (section 5), so neither you nor the
model can move the goalposts after seeing the results.

### 3.11 AI analyst (GPT-5) jobs

- Pre-market brief (news, results calendar, GIFT Nifty, FII/DII, your watchlist).
- Signal-card context (similar trades, regime, news risk).
- Post-trade post-mortems, daily and weekly reviews.
- Proposals for strategy changes, with a hypothesis and a test plan in JSON.
- Answering questions in plain words ("why did the risk engine block my Bank Nifty
  trade?", "how does Supertrend work?", "show my worst time of day").
- An **indicator explainer**: since you mentioned remembering how every indicator
  works, the app has a knowledge base of each indicator (formula, intuition,
  typical uses, failure modes, *your* results with it), which the assistant can
  answer from.

Guardrails: structured outputs only; it cannot call execution; every claim with a
number must cite the SQL result it came from; cost caps per day.

### 3.12 "Why this trade": every signal explains itself

Every signal the app shows (buy, sell, or an options position) carries an
explanation card. It is built from facts first, and the language model only
turns those facts into plain words; it may not add a reason that is not in the
facts.

| Section | What it says | Comes from |
| --- | --- | --- |
| **The rule that fired** | "ORB long: the 9:35 five-minute bar closed at 1,204.2, above the 9:15–9:30 high of 1,199.0 (+0.43 %)" | Strategy code: the exact condition and the indicator values at that bar |
| **Supporting conditions** | "Above VWAP (1,195.8) · EMA20 > EMA50 · volume 1.8× the opening bars · Nifty up 0.6 %" | Strategy filters, each one marked met or not met |
| **The plan** | Entry, stop (and why there: "opening-range midpoint"), target ("2R"), time exit, size, max loss with charges | Exit rules and the risk engine |
| **The evidence** | "This strategy version: 212 backtest trades, expectancy +0.18R after costs, profit factor 1.4; in a trending, low-VIX market: +0.26R over 64 trades. Paper: 38 trades, +0.11R." | SQL over backtests and paper trades |
| **Similar past trades** | "7 similar setups in your journal: 3 won, 4 lost; 3 of the 4 losses came after 14:00" | pgvector retrieval + SQL stats |
| **Risks and what would cancel it** | "Results due tomorrow · VIX rising · a close back below 1,199 invalidates the breakout" | News/calendar feed, regime tags, the strategy's invalidation rule |
| **Plain-language summary** | Two or three sentences a person can read in five seconds | GPT-5, given only the rows above |
| **Confidence, honestly** | A grade from the evidence (sample size and expectancy), never a "% chance to win" | Fixed formula in code |

The card is stored with the signal (`signals.context` and `signals.explanation`),
so after the trade the post-mortem can compare what the card claimed with what
happened. A signal whose strategy has no evidence yet (new version, too few
trades) says so in red: "No track record yet: paper only."

### 3.13 Notifications

Telegram bot or web push for: login reminder, signals awaiting approval, fills,
stop hits, risk blocks, kill-switch events, reconciliation mismatches.

---

## 4. Data model (Supabase / Postgres)

All tables have `user_id uuid references auth.users` and Row Level Security
(`user_id = auth.uid()`). Service-role access only from the backend.

```sql
-- identity and settings
create table profiles (user_id uuid primary key references auth.users, display_name text,
  tz text default 'Asia/Kolkata', created_at timestamptz default now());
create table broker_accounts (id uuid primary key default gen_random_uuid(), user_id uuid not null,
  broker text check (broker in ('zerodha','paper')), label text,
  api_key_ref text,              -- a reference into Supabase Vault, never the key itself
  access_token_enc bytea,        -- today's Kite token, encrypted, expires daily
  token_expires_at timestamptz, is_live boolean default false);
create table risk_limits (user_id uuid primary key, max_loss_per_trade numeric, max_daily_loss numeric,
  max_trades_per_day int, max_consecutive_losses int, max_open_positions int,
  max_margin_used_pct numeric, entry_start time, entry_end time, flat_by time,
  allow_naked_short_options boolean default false, updated_at timestamptz default now());

-- market reference
create table instruments (instrument_token bigint primary key, exchange text, tradingsymbol text,
  segment text, instrument_type text, name text, expiry date, strike numeric, lot_size int,
  tick_size numeric, as_of date);
create table candles (instrument_token bigint, tf text, ts timestamptz, o numeric, h numeric,
  l numeric, c numeric, v bigint, oi bigint, primary key (instrument_token, tf, ts));

-- strategies and evidence
create table strategies (id text primary key, name text, description text, market text);
create table strategy_versions (id uuid primary key default gen_random_uuid(), strategy_id text
  references strategies, version text, params jsonb, code_hash text, status text check
  (status in ('research','backtested','paper','live','retired')), created_at timestamptz default now());
create table backtests (id uuid primary key default gen_random_uuid(), strategy_version_id uuid,
  universe jsonb, start_date date, end_date date, cost_model_version text, in_sample boolean,
  metrics jsonb, created_at timestamptz default now());
create table backtest_trades (backtest_id uuid, instrument_token bigint, entry_ts timestamptz,
  exit_ts timestamptz, side text, qty int, entry numeric, exit numeric, costs numeric,
  r_multiple numeric, exit_reason text);

-- the live/paper pipeline
create table signals (id uuid primary key default gen_random_uuid(), user_id uuid,
  strategy_version_id uuid, source text check (source in ('strategy','tradingview','manual','ai_proposal')),
  instrument_token bigint, side text, entry numeric, stop numeric, target numeric,
  context jsonb,                  -- indicator values, regime tags at signal time
  explanation jsonb,              -- the "why this trade" card (section 3.12)
  status text, created_at timestamptz default now());
create table orders (id uuid primary key default gen_random_uuid(), user_id uuid, signal_id uuid,
  account_id uuid, mode text check (mode in ('paper','live')), broker_order_id text,
  params jsonb, state text, risk_verdict jsonb, approved_by text, approved_at timestamptz,
  created_at timestamptz default now(), updated_at timestamptz);
create table fills (id uuid primary key default gen_random_uuid(), order_id uuid, ts timestamptz,
  qty int, price numeric, costs jsonb);
create table trades (id uuid primary key default gen_random_uuid(), user_id uuid, mode text,
  strategy_version_id uuid, instrument_token bigint, side text, qty int,
  entry_ts timestamptz, exit_ts timestamptz, entry numeric, exit numeric,
  gross_pnl numeric, costs numeric, net_pnl numeric, r_multiple numeric,
  mae numeric, mfe numeric, exit_reason text, rules_followed boolean,
  regime jsonb, entry_context jsonb);

-- journal and memory
create extension if not exists vector;
create table trade_journal (trade_id uuid primary key references trades, user_id uuid,
  ai_postmortem text, user_notes text, mistake_tags text[], emotion text,
  chart_entry_path text, chart_exit_path text);
create table trade_memory (id uuid primary key default gen_random_uuid(), user_id uuid,
  trade_id uuid references trades, content text, embedding vector(1536),
  metadata jsonb, created_at timestamptz default now());
create index on trade_memory using hnsw (embedding vector_cosine_ops);
create table proposals (id uuid primary key default gen_random_uuid(), user_id uuid,
  strategy_id text, hypothesis text, change jsonb, evidence jsonb,
  oos_backtest_id uuid, paper_result jsonb,
  status text check (status in ('new','testing','passed','failed','approved','rejected')),
  created_at timestamptz default now());

-- safety
create table risk_events (id bigserial primary key, user_id uuid, ts timestamptz default now(),
  kind text, detail jsonb);       -- blocks, kill switch, reconciliation mismatches
create table audit_log (id bigserial primary key, user_id uuid, ts timestamptz default now(),
  actor text, action text, detail jsonb);   -- append-only
```

The embedding dimension depends on the embedding model chosen; match `vector(n)`
to it.

---

## 5. "Locking the accuracy": what actually makes results reliable

### 5.1 Measure the right things

| Metric | Meaning | Gate (starting suggestion) |
| --- | --- | --- |
| Expectancy | Average R per trade, after all costs | > +0.10R |
| Profit factor | Gross wins ÷ gross losses, after costs | > 1.3 |
| Max drawdown | Worst peak-to-trough on the equity curve | Within what you can tolerate, set in ₹ before testing |
| Trades | Sample size | ≥ 100 in backtest, ≥ 50 in paper before live |
| Win rate and average win/loss | Only meaningful together | Reported, not a gate |
| Consistency | Results by month, by regime, by time of day | No single month or regime carries the whole result |

### 5.2 Backtest hygiene (where most "proven" strategies die)

1. **Costs and slippage always on.** A strategy that only works without them does not work.
2. **No look-ahead:** only closed bars; signals act on the *next* bar or tick.
3. **No survivorship bias:** use the historical stock universe, including delisted stocks.
4. **Out-of-sample:** tune on one period, judge on a later period never looked at.
5. **Walk-forward:** repeat tune-then-test across rolling windows.
6. **Limit the number of tries:** every parameter set tried is logged; the more you
   try, the stronger the evidence must be (deflated Sharpe, or simply a stricter gate).
7. **Monte Carlo:** reshuffle trade order and resample to see the realistic drawdown range.
8. **Robustness:** small parameter changes should not flip the result (a plateau,
   not a spike).
9. **Regime check:** results split by trend/range, VIX level, expiry week.

### 5.3 The promotion ladder

```
research ─▶ backtested (in-sample) ─▶ passed out-of-sample + walk-forward
        ─▶ paper (≥ 50 trades or 4 weeks, live prices, same code)
        ─▶ live assisted, minimum size (1 lot / smallest share size) for 4 weeks
        ─▶ live assisted, normal size ─▶ (optional) live auto, inside hard limits
```

Demotion is automatic: if live expectancy over the last N trades falls below a
threshold, or drawdown exceeds the limit, the strategy drops back to paper.

### 5.4 Guarding against the AI fooling you

- The model's proposals are hypotheses; the backtester judges them on data the
  model did not see.
- Numbers shown to you come from SQL, never from the model's text.
- Every model output that reaches the UI is stored with its inputs (prompt, retrieved
  trades) so you can audit why it said something.
- No proposal goes live without your approval and a version bump.

### 5.5 Guarding against yourself

- A daily loss limit that disables trading (it does not ask).
- Mandatory journal note on any manual override of a rule.
- A "rules followed" score per day, tracked over time. For most traders this is
  worth more than any indicator.

---

## 6. Security and compliance

- **Secrets:** Kite API key and secret in Supabase Vault or the VPS's environment;
  access tokens encrypted at rest and expired daily; nothing broker-related ever
  sent to the browser.
- **Auth:** Supabase Auth with TOTP 2FA required for any live-trading action;
  re-authentication to switch a strategy to live or disable the kill switch.
- **Live order confirmation:** a separate confirm step showing the exact order;
  live-auto only for strategies at the top of the promotion ladder.
- **Audit log** append-only for every order, approval, risk block and config change.
- **Backups:** Supabase point-in-time recovery (Pro plan) or nightly dumps.
- **SEBI:** personal use only; static IP registered with Zerodha; daily login;
  stay far below 10 orders per second; keep it white-box (your own rules). If you
  ever want to offer it to others, that needs broker empanelment and, for black-box
  logic, a Research Analyst registration: plan for that separately.
- **Taxes:** intraday equity profits are speculative business income; F&O is
  non-speculative business income; both usually need an ITR-3 and possibly a tax
  audit. The trades table makes the CA's job easy; it is not tax advice.

---

## 7. Plan of action

Each phase ends with a demo and an exit criterion. Durations assume one or two
developers part-time; adjust after Phase 1.

| Phase | Build | Exit criterion |
| --- | --- | --- |
| **0. Foundations** (1 wk) | Monorepo, Supabase project, auth, RLS, CI, VPS in Mumbai with a fixed IP, Kite Connect app, daily login flow | You can log in to the app and to Kite from it; a token is stored encrypted |
| **1. Market data + charts** (2 wks) | Instrument sync, KiteTicker → candles, history backfill, charts with indicators, NSE calendar | Live Nifty futures and 20 stocks charting with VWAP/EMA/Supertrend; candles match Kite's |
| **2. Indicators + backtester** (3 wks) | Tested indicator library, cost model, event-driven backtester, metrics, walk-forward | ORB (ported from the `nse-*` skills) backtested on 2 years of liquid stocks with costs; report page |
| **3. Risk engine + paper trading** (2–3 wks) | Risk engine (port `nse-risk`), OMS, paper engine, positions UI, kill switch, 15:15 flatten | Two weeks of ORB paper trading end to end, with no manual steps except approvals |
| **4. Journal + AI analyst** (2–3 wks) | Trade journal, post-mortems, embeddings, retrieval on signal cards, daily/weekly reviews | Daily review arrives at 16:00; signal cards show similar trades; proposals logged |
| **5. Live intraday equity** (2 wks + 4 wks at minimum size) | Kite adapter live, stop placement, reconciliation, confirmations, alerts | 4 weeks live at minimum size with zero reconciliation mismatches and every rule enforced |
| **6. Index futures** (3 wks) | Nifty/Bank Nifty futures: margins API, lot sizes, rollover, strategies 3 and 5 | Passed backtest + paper gates |
| **7. Options** (5–6 wks) | Options chain pipeline, Greeks/IV, margin checks, strategies 6 and 7 with hedges only | Passed backtest + paper gates; defined-risk only |
| **8. TradingView webhooks + learning loop** (2 wks) | Webhook endpoint, proposal pipeline with automatic OOS tests | A proposal goes research → test → your approval end to end |

Paper trading continues in parallel the whole time: every new strategy runs on
paper before and alongside live.

### What to reuse from the NSE skills we built

| Skill code | Becomes |
| --- | --- |
| `nse-budget/size.py` (`tick_for`, `charges`, sizing) | `backend/risk/sizing.py`, `backend/costs/zerodha.py` |
| `nse-risk/check.py` | The core of `backend/risk/engine.py` |
| `nse-kite/ticket.py` (`parse_yes`, fingerprints, state machine) | Approval flow and order fingerprints in the OMS |
| `nse-entry/entry.py` (ORB, closed-bar logic) | `strategies/orb.py` |
| `nse-exit/exit.py` (stop/target rules) | `strategies/exits.py` |
| `nse-timemachine/timemachine.py` | Monte Carlo module of the backtester |
| `nse-alerts` (NSE calendar, holidays) | `backend/market/calendar.py` |
| `nse-news`, `nse-analyst` | Pre-market brief data sources |
| `tests/test_nse.py` | Seed of the backend test suite |

Keep the skills too: they are useful for research in a chat while the app is
being built.

---

## 8. Repository layout

```
TradingBot/
├── apps/
│   └── web/                      # Next.js
│       ├── app/(auth)/ app/(dashboard)/{live,signals,positions,journal,strategies,backtests,settings}/
│       ├── components/charts/    # Lightweight Charts wrappers
│       └── lib/supabase/
├── backend/
│   ├── api/                      # FastAPI routers
│   ├── brokers/{base.py,kite.py,paper.py}
│   ├── market/{instruments.py,ticker.py,candles.py,calendar.py,options_chain.py}
│   ├── indicators/
│   ├── strategies/{base.py,orb.py,vwap_pullback.py,...}
│   ├── backtest/{engine.py,fills.py,metrics.py,walkforward.py,montecarlo.py}
│   ├── risk/{engine.py,sizing.py,killswitch.py}
│   ├── costs/{zerodha.py,cost_model.yaml}
│   ├── oms/{orders.py,reconcile.py}
│   ├── journal/{postmortem.py,memory.py,reviews.py}
│   ├── ai/{client.py,prompts/,schemas.py}
│   ├── workers/{market_data.py,strategy_runner.py,execution.py,scheduler.py,analyst.py}
│   └── tests/
├── supabase/
│   ├── migrations/               # the SQL in section 4
│   └── seed.sql
├── IMPLEMENTATION_GUIDE.md       # this file
└── infra/                        # docker-compose, VPS setup, systemd units
```

---

## 9. Running costs (approximate, October 2026; confirm current prices)

| Item | Cost |
| --- | --- |
| Kite Connect (orders + live data + history) | ₹500 / month per app |
| Mumbai VPS with fixed IP | ~₹800–2,500 / month |
| Supabase | Free tier to start; Pro ~US$25 / month for backups and more resources |
| TradingView paid plan (only if you want webhooks) | Varies by plan |
| OpenAI API (GPT-5 + embeddings) | Usage-based; set a daily cap |
| Telegram bot | Free |

Plus trading costs: Zerodha brokerage and statutory charges on every order.

---

## 10. Decisions made (23 October 2026)

| Question | Decision | What it changes |
| --- | --- | --- |
| Hosting | **Local Mac first** | Fine for paper trading: SEBI's static-IP rule applies only to placing live orders. Move to a Mumbai VPS before going live. |
| Capital and limits | **Paper trading only for now** | The live Kite order adapter moves after the paper phases. Paper account starts with a virtual ₹10,00,000 and the limits in `risk_limits` (editable). |
| Assisted or automatic | **Assisted** | Every order, paper included, goes through a confirm step with its explanation card. |
| TradingView | **To be set up and subscribed** | Charts use the free Lightweight Charts library from day one. Webhook alerts need a paid TradingView plan: Phase 8. |
| GIFT Nifty | **Use Nifty index F&O instead** | Markets: NSE intraday equity, Nifty and Bank Nifty futures and options. |
| Option selling | **Allowed** | The risk engine still requires a margin check and a max loss per position. Defined-risk (hedged) structures are the default; naked short options need an explicit per-strategy setting. |
| Who builds | **Claude, phase by phase** | Each phase ends with a demo and a test run before the next starts. |
| Signals | **Every suggestion must say why** | Section 3.12: the explanation card is part of the signal model from Phase 2. |

### Market data during the paper phase

Paper trading needs live prices. Yahoo is free but delayed for some feeds and has
no NSE options chain. For Nifty F&O paper trading, a **Kite Connect** subscription
(₹500 a month) gives live ticks, options chains and history, and needs **no** static
IP for data. Plan: Phases 0–2 run on Yahoo and NSE data; subscribe to Kite Connect
before Phase 3 (paper trading).

### Revised phase order

0. Foundations (local) · 1. Market data + charts · 2. Indicators, backtester, explanation
cards · 3. Risk engine + paper trading (equity) · 4. Journal + AI analyst ·
5. Nifty/Bank Nifty futures (paper) · 6. Options, including selling (paper) ·
7. TradingView webhooks + learning loop · 8. Move to a VPS and add the live Kite adapter
(only when you decide to trade real money).

## 11. Open questions (earlier list)

1. **Capital and limits:** how much capital for paper and live, max loss per trade,
   per day, per month.
2. **First market:** recommendation is intraday equity ORB first (already built),
   then Nifty futures, then options.
3. **Assisted or automatic:** recommendation is assisted (you confirm every live
   order) for at least the first three months.
4. **Hosting:** VPS in Mumbai (recommended, solves the static IP) or your laptop.
5. **TradingView:** do you already have strategies or alerts there that you want to
   bring in? Which plan?
6. **GIFT Nifty:** a signal only (recommended) unless an NSE IX broker confirms you
   can trade it.
7. **Option selling:** allowed at all? If yes, defined-risk only?
8. **Developers:** who builds what (frontend / backend), and how many hours a week.

---

## Sources

- SEBI F&O study, FY25: [Business Standard](https://www.business-standard.com/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html), [Value Research](https://www.valueresearchonline.com/stories/225398/average-trader-lost-rs-1-1-lakh-fo-fy25/); FY26: [Open Magazine](https://openthemagazine.com/business/sebi-fo-loss-study-explained-why-9-in-10-retail-traders-lost-91685-crore-in-fy26)
- GIFT Nifty access: [Belong](https://getbelong.com/blog/can-nris-trade-gift-nifty-futures/), [5paisa](https://www.5paisa.com/blog/where-to-trade-gift-nifty-contracts), [MNCL](https://www.mnclgroup.com/gift-nifty-trading)
- Bank Nifty expiry and lot size: [Zerodha bulletin](https://zerodha.com/marketintel/bulletin/429705/revision-in-lot-size-of-index-derivative-contracts-from-december-30-2025), [AlgoTest](https://algotest.in/blog/bank-nifty-lot-size-2026-current-quantity-and-one-lot-cost/), [HDFC Sky](https://hdfcsky.com/blogs/share-market/bank-nifty-expiry)
- SEBI retail algo framework: [FYERS notice](https://fyers.in/notice-board/new-sebi-framework-for-retail-algo-trading-from-april-01-2026/), [Liquide](https://blog.liquide.life/sebi-algo-trading-regulations-2026/), [5paisa](https://www.5paisa.com/news/sebi-tightens-algo-trading-rules-with-mandatory-2fa-and-audit-trails-from-april-1)
- Kite: [zerodha/kite-mcp-server](https://github.com/zerodha/kite-mcp-server), [Kite forum on market protection and static IP](https://kite.trade/forum/discussion/comment/52027), [Zerodha charges](https://zerodha.com/charges/)

These are mostly secondary sources; confirm rules with SEBI, NSE and Zerodha before
relying on them, and recheck them before each phase that depends on them.
