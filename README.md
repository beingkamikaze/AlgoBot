# TradingBot

**Using the app:** see [USER_GUIDE.md](USER_GUIDE.md) for what each screen is for and the day-to-day flow.

Rule-based trading for NSE intraday equity and Nifty / Bank Nifty F&O, with an AI
analyst on top. Paper trading only for now. Every signal explains why.

The plan, decisions and architecture are in [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md).

| Part | Folder | Runs on |
| --- | --- | --- |
| Web app (Next.js 16, Tailwind, Supabase Auth) | `web/` | http://localhost:3100 |
| API (Python 3.12, FastAPI) | `backend/` | http://localhost:8100 |
| Database, auth, vector memory (Supabase) | `supabase/migrations/` | your Supabase project |

## Phase 0 status

- [x] Backend: health and market clock, Supabase session check, Zerodha cost model,
      NSE ticks and holidays, the signal and "why this trade" explanation model, tests
- [x] Database schema with Row Level Security (`supabase/migrations/0001_init.sql`)
- [x] Web app: market clock, sign in / create account, paper account summary
- [ ] Your Supabase project connected (steps below)

## Phase 1 status: market data and charts

- [x] Data: Yahoo (`.NS` stocks; Nifty, Bank Nifty, India VIX, Fin Nifty, Nifty IT) for
      1m / 5m / 15m / 1h / 1d bars; only bars inside 9:15–15:30, forming bar flagged;
      quotes for the watchlist; NSE's equity list (search) and holiday list (cached in `backend/data/`)
- [x] Indicator library (`backend/app/indicators/core.py`), one implementation for charts,
      backtests and signals: SMA, EMA, VWAP, RSI, ATR, Bollinger, MACD, Supertrend,
      opening range, previous-day levels; checked against known values (RSI = Wilder's 70.53)
- [x] API: `/api/market/chart`, `/quotes`, `/search`, `/holidays`, `/meta`
- [x] Chart page (http://localhost:3100/chart): search, timeframes, indicator toggles with
      explanations, RSI / MACD / ATR panes, volume, previous close, live watchlist, IST axis
- Limits: Yahoo data is free and can lag; indices have no volume, so VWAP appears on
  stocks now and on Nifty futures once Kite data arrives (Phase 3)

## Phase 2 status: backtester and signals that explain why

- [x] Strategy framework (`backend/app/strategies/`): a strategy reads only closed bars (tested by
      cutting the data short) and returns its rule, every filter with its numbers, stop reason and
      invalidation. Starters: opening-range breakout (5m), VWAP pullback (5m), Supertrend flip (15m)
- [x] Backtester (`backend/app/backtest/`): next-bar-open entries, stop-before-target, gaps fill at the
      open, targets must trade through, slippage, Zerodha charges, 15:15 time exit; report with gates,
      in-sample vs held-back split, Monte Carlo drawdown, results by time, VIX, Nifty gap, side, month, stock
- [x] Local bar store (`backend/data/bars/`): every bar downloaded is kept, so history grows past
      Yahoo's 60-day limit
- [x] "Why this trade" cards (`backend/app/signals/`): rule + conditions + plan + evidence for the exact
      strategy version + similar setups + risks + invalidation + a fixed-rule confidence grade
- [x] Pages: **Signals** (scan now, or replay the latest session at any time) and **Backtests**
- First results (Nifty 50, 29 Jul to 23 Oct 2026, after costs): ORB −0.18R, VWAP pullback −0.35R,
  Supertrend −0.08R per trade. None passes the gates, so every signal is graded "low" until a
  strategy earns better evidence.
- Backtest results are files in `backend/data/backtests/` until Phase 3 moves storage to Supabase.

## Phase 3 status: risk engine and paper trading

- [x] Risk engine (`backend/app/risk/engine.py`): stop required and on the right side, max loss per trade
      including Zerodha charges, daily / monthly loss limits (realized + open risk), entries per day, losses
      in a row, open positions, one position per stock, no leverage, capital-in-use cap, entry window,
      NSE hours and holidays, kill switch, limit-vs-last-price sanity. Exits are never blocked.
- [x] Paper engine (`backend/app/paper/`): orders, fills and trades in Supabase; entries fill on the next
      1-minute bar that reaches the limit; stop before target; gaps fill at the open; targets must trade
      through; forced exit at your flat-by time (15:15); unfilled entries expire after 10 minutes;
      MAE/MFE tracked; charges on every trade; every action in `audit_log`
- [x] Assisted flow: a signal card's **Paper trade** button (or a manual ticket) makes a ticket sized from
      *your* max loss; the risk verdict is shown; nothing is placed until you press **Confirm**, and the
      risk check runs again at that moment
- [x] **Paper trading** page: capital, free cash, today's and this month's result, limit meters, open
      positions with live P&L and Close, working orders, manual ticket, editable limits, closed trades,
      kill switch
- Data: Yahoo 1-minute bars (delayed); the loop checks every 20 s while the market is open
- Tested end to end against Supabase by replaying 23 Oct (RELIANCE: 177 shares sized to ₹1,996.76 max
  loss, filled 10:30, stopped 11:40, −₹1,867.51 after ₹120.52 charges); test rows removed

## Phase 4 status: journal and the GPT-5 analyst

- [x] AI client (`backend/app/ai/client.py`): Azure OpenAI GPT-5 (or OpenAI) with strict JSON outputs, a daily
      token cap (`LLM_DAILY_TOKEN_CAP`) and every call logged in `ai_usage`
- [x] Grounding check (`backend/app/ai/grounding.py`): every number the model writes must be in the facts it was
      given, or the text is rejected and the plain template is kept
- [x] Signal cards: **Explain in plain words** (GPT-5, full backtest result always first, never encourages a trade
      the evidence does not support), **Similar trades I took** (vector search over your journal), and real news
      risks (NSE results board meetings, important NSE filings)
- [x] Post-mortem for every closed paper trade, with mistake tags from a fixed list, stored in `trade_journal` and
      embedded into `trade_memory`
- [x] Daily and weekly reviews from SQL statistics; weekly hypotheses stored in `proposals` as UNTESTED
- [x] Pre-market brief (Home), Ask the analyst (Journal), Indicator guide (Learn)
- Needs `supabase/migrations/0002_journal_ai.sql` (reviews, ai_usage, journal columns, memory search)

## Phase 5 status: Nifty and Bank Nifty futures (paper)

- [x] Instruments (`backend/app/core/instruments.py`): `NIFTY-FUT` / `BANKNIFTY-FUT` resolve to the month's contract
      (e.g. `NIFTY26OCTFUT`), rolling to the next month 2 trading days before expiry; expiry = last Tuesday,
      moved earlier for holidays; lot sizes 65 and 30 (NSE, from Jan 2026); index-derivative ticks
- [x] Charges (`backend/app/core/costs.py`): futures (STT 0.05% on sells) and options (STT 0.15% of premium),
      from zerodha.com/charges read 2026-10-24. One Nifty lot costs about ₹880 a round trip
- [x] Backtester, risk engine and paper engine trade lots: whole-lot quantities, futures charges, margin
      (estimated at 12% of the value) blocked instead of the full value, leverage warning
- [x] New strategy: previous-day high/low breakout with a narrow-CPR filter
- [x] Signals and Backtests: "Index futures" universe; cards show contract, lots, exposure, margin and futures risks
- Until Kite data: futures are priced from the INDEX (the basis is ignored), indices have no volume (VWAP filters
  skip), margin is an estimate, and only ~58 sessions of 5-minute history exist (too few trades to judge)
- First results, one lot, after charges: ORB −0.27R (83 trades), Supertrend −0.06R (60), previous-day breakout −0.60R (31)
- Your ₹2,000 max loss per trade cannot fit one Nifty lot (a typical ORB stop risks ₹4,000–7,000 a lot):
  futures tickets are blocked until you raise it

## Phase 6 status: options (paper)

- [x] NSE option chain without Kite (`backend/app/options/chain.py`): every strike's bid, ask, last, NSE IV, OI and
      volume; real expiries (weekly Nifty, monthly Bank Nifty, holiday-shifted); PCR, max pain, ATM IV; Kite contract
      names (monthly `NIFTY26OCT22600CE`, weekly `NIFTY26N0322600PE`); a MODELED chain (from India VIX) if NSE fails
- [x] Pricing (`backend/app/options/pricing.py`): Black-76 prices, Greeks and implied volatility, priced off the
      forward read from the chain itself (put-call parity), so calls and puts agree
- [x] Structures (`backend/app/options/structures.py`): long call/put, debit and credit spreads, iron condor, iron fly,
      short straddle/strangle; max profit/loss, breakevens, Greeks, margin estimate, charges, payoff curve
- [x] Risk (`check_structure`): separate F&O max loss (migration 0003), naked short options off by default, illiquid
      legs (spread > 10%) blocked, no new positions after 14:30 on expiry day, margin vs free cash, daily limits
- [x] Paper trading: all legs fill together at NSE's ask (buys) and bid (sells); positions valued at what closing
      would get; stop/target in rupees; flat by 15:15; post-mortems know option structures and bid-ask costs
- [x] Modeled backtests (`backend/app/options/backtest.py`): ORB option buy, intraday iron fly; Black-76 from India
      VIX in trading time. INDICATIVE ONLY: no smile, no intraday IV changes. Calendar time vs trading time alone
      moved the iron fly from −0.47R to +0.14R
- [x] **Options** page: chain with OI bars and delta, builder, payoff chart, paper ticket; futures signal cards link
      to "Trade with options" with the signal's index stop and target
- Needs `supabase/migrations/0003_fno_limits.sql` for the separate F&O limit

## Phase 7 status: TradingView alerts and the learning lab

- [x] TradingView webhooks (`backend/app/webhooks/tradingview.py`): one source per TradingView strategy, each with its
      own URL and secret (shown once, stored as a SHA-256 hash). Only TradingView's four addresses are accepted
      (52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7); 30 alerts a minute; duplicates within 5 minutes dropped;
      the endpoint answers at once (TradingView waits only 3 s) and builds the ticket in the background
- [x] Every alert becomes a "why" card (`backend/app/signals/external.py`) and a risk-checked paper ticket in the inbox
      (Signals and TradingView pages). **Nothing is approved automatically**; an unreviewed ticket expires after 15 minutes.
      An alert with no stop is rejected. The card's evidence is your paper record from that alert source
- [x] Learning lab (`backend/app/lab/loop.py`, **Lab** page): a hypothesis is a concrete parameter change (GPT-5 can
      translate a review's suggestion, restricted to the strategy's real parameters). The original and the change are
      backtested on the same days; only the held-back last 30% of sessions decide. To pass: ≥ 30 held-back trades,
      positive after costs, ≥ +0.05R better, and better in enough bootstrap resamples (90% for one test, stricter with
      each test that month). You approve; the version runs on paper; after 20 paper trades a losing one is retired
- [x] Approved versions can be chosen on Signals (scans) and loaded on Backtests
- First real test: ORB with `min_range_pct` 0.3 → 0.6 FAILED (held back +0.03R better, better in 66% of resamples; 90% needed)
- Needs `supabase/migrations/0004_webhooks_lab.sql`

### Receiving TradingView alerts on this Mac

TradingView posts only to https on port 80/443, so the Mac needs a tunnel:

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8100
```

Copy the `https://….trycloudflare.com` address into the **TradingView** page; it builds each source's webhook URL.
The quick-tunnel address changes every time it starts (a named tunnel on your own domain keeps it fixed). TradingView
webhooks need a paid TradingView plan and two-factor authentication on the TradingView account.

## Phase 8 status: live trading through Zerodha Kite (assisted)

- [x] Kite adapter (`backend/app/live/broker.py`, official `kiteconnect` SDK): LIMIT entries, SL stops, marketable LIMIT
      exits, never market orders; every order tagged with our id; at most 4 orders a second; MIS on NSE (stocks) and NFO
      (index futures). **Options stay paper** until stocks and futures run clean live
- [x] Daily Kite login (`session.py`): you log in on kite.zerodha.com; the token is encrypted (Fernet) in Supabase,
      never sent to the browser, and dies at 06:00. Kite prices replace Yahoo for paper trading too while you are logged in
- [x] Gates (`gates.py`), all needed for every live order: `LIVE_TRADING_ENABLED=true` on the server, Kite login today,
      two-factor sign-in to this app, armed today (type LIVE), market open. Exits are never gated
- [x] Live limits (migration 0005) on top of paper ones, starting at ₹500 per trade, ₹1,500 per day, 3 trades, ₹50,000
      order value. The ticket is re-sized with live limits and real Kite funds (F&O margin from Kite) and you confirm that size
- [x] Order life (`service.py`): fill → stop-loss at Zerodha at once; stop refused → position closed; target, 15:15, Close
      and the kill switch turn that same stop order into the exit, so there is only ever one exit order (never an accidental
      short); unconfirmed sends are found by tag, never sent twice; reconciliation with Kite's positions every 3 seconds
- [x] **Live** page: gates, Kite login, two-factor setup, arm/disarm, live limits, positions (with "stop at Zerodha"),
      alerts, kill switch (one switch for paper and live). Tickets get a red "real money" step when every gate is open
- [x] Server: `infra/` (Docker, Caddy HTTPS, one backend worker), `REQUIRE_AUTH` + `ALLOWED_EMAILS` for a public server;
      see `infra/DEPLOY.md`
- Tested against a fake Zerodha (28 tests); **not yet against real Kite**: that needs your Kite Connect app
- Needs `supabase/migrations/0005_live.sql`

## XAUUSD status (separate setup, see [XAUUSD_PLAN.md](XAUUSD_PLAN.md))

- [x] X1 foundation (`backend/app/global_markets/`): XAUUSD instrument and sizing; market clock in New York and London time
      (Sunday 18:00 to Friday 17:00, daily break, sessions, holidays, entry window, flat-by), checked against 2 years of
      `GC=F` hourly bars. Holidays follow the COMEX pattern until a broker's schedule replaces them
- [x] X2 data and chart (**XAUUSD chart** page, `/api/global/...`): real spot from Dukascopy (1-minute bid and ask in
      `backend/data/global/`, downloaded slowly while the app runs, newest first, resumes by itself), live spot from Twelve
      Data when `TWELVEDATA_API_KEY` is set, `GC=F` futures otherwise. Spot and futures are never joined. The page shows how
      much history exists and a check against `GC=F`
- [x] X3 costs and backtester (`global_markets/backtest.py`, `costs.py`, `report.py`): signals on 5-minute mid prices, fills on
      1-minute real bid and ask; stops watch the side you would really exit on; stop before target; gaps fill at the open; targets
      must trade through; flat by 13:00 New York, earlier on short days, never held overnight. Slippage is an assumed $0.10 a fill,
      commission $0, until a broker is chosen. Reports hide held-back sessions unless asked, and also run at double slippage
- [x] X4 strategies (`global_markets/strategies/library.py`, **XAUUSD backtests** page): Asian range breakout, New York open range,
      failed break of yesterday's high or low, settings fixed before any ran. Research days only (`research.py`); held-back days
      start 6 Feb 2025 and the download fetches research days first. **First result, 8 Nov 2026, 150 research days (9 Jul 2024 to
      5 Feb 2025): nothing passes.** Asian breakout -0.16R over 105 trades (loses); New York open range +0.04R over 148 trades,
      PF 1.09 (positive but below the 0.10R and 1.3 gates; +0.02R with double slippage); failed break 30 trades, too few to judge
- [x] X5 risk and paper, built 9 Nov 2026 (`global_markets/risk.py`, `paper_sim.py`, `paper.py`, **XAUUSD paper** page): a $10,000
      USD account in its own tables (migration `0006_global_markets.sql`, run it in the SQL editor), dollar limits ($20 / $60 / $300,
      5 trades, 3 losses in a row, 1 position, $10,000 cap), ticket, risk check, your confirm (paper override recorded as rules
      not followed), working orders, fills on 1-minute bars by bar time, stop / target / flat-by exits, manual close and its own
      kill switch. Prices: Twelve Data spot with the key, otherwise `GC=F` (about 10 minutes late), with an assumed $0.60 spread and
      $0.10 slippage. Replayed sessions pass on a fake database. Against Supabase on 10 Nov: ticket, override, working and expiry
      checked (and a bug fixed: an override of the entry window used to expire at once); a fill and exit still to watch live
- [x] X6 signals and AI, built 10 Nov 2026 (`global_markets/macro.py`, `news.py`, `signals.py`, `journal.py`): a "why this trade"
      card on every ticket, built by code (your reason, the plan, today's, Asian and yesterday's levels, the typical 5-minute move,
      US releases until flat-by, gold headlines, confidence low); US CPI, jobs and Fed dates from the official schedules (through
      Dec 2027) as warnings on tickets, never blocks; a post-mortem per closed trade and daily / weekly reviews, written by GPT-5
      from facts only with the number check, or a plain template. Migration `0007_global_journal.sql`
- [x] Live gold signals, built 10 Nov 2026 at your request (`global_markets/scanner.py`, **XAUUSD signals** page, migration
      `0008_global_signals.sql`): the three strategies at their 7 Nov settings on live closed 5-minute bars, inside the entry
      window, at most two per strategy a day, each with its why card and the fixed-rule confidence from the research-days
      backtest. "Make paper ticket" still needs the risk check and your confirmation. Never used to choose settings
- [x] Gold agent, built 22 Nov 2026 at your request (`global_markets/agent/`, **XAUUSD agent** page, migration
      `0009_global_agent.sql`): a watcher (code) builds facts from closed bars and wakes GPT-5 when something happens; a
      strategist proposes one trade or passes; code checks the stop, reward:risk and that every number is from the facts; a
      reviewer agent can turn ideas down but never approve. An idea becomes an ordinary paper ticket only when you ask, sized by
      the risk check, confirmed by you. A scorekeeper records how every idea would have ended. Untested by nature (GPT-5 has seen
      past prices): graded low, never used to choose settings. Budget: 16 looks a day, one per 15 minutes (`GLOBAL_AGENT_*`)
- [x] Telegram approval, built 23 Nov 2026 (`app/telegram/`, migration `0010_telegram.sql`, `TELEGRAM_BOT_TOKEN`): link one
      private chat with a one-time code from the XAUUSD agent page; live ideas arrive with buttons, a ticket takes two taps to
      confirm and the risk check runs again; fills and exits are sent. Long-polling, so no public URL is needed. Paper only
- First real number, a coin flip on 50 sessions of `GC=F` with a 2 ATR stop: -0.056R ± 0.071, in line with costs of 0.087R
- Twelve Data has not been tried against the real service yet. Signals never trade by themselves

## Setup (once)

### 1. Supabase project

1. Create a free project at https://supabase.com (region: Mumbai, `ap-south-1`).
2. In the project: **SQL Editor → New query**, paste all of
   `supabase/migrations/0001_init.sql`, and run it. Then do the same with `0002_journal_ai.sql` and `0003_fno_limits.sql`.
3. **Project Settings → API**: you need the Project URL, the `anon` (publishable)
   key, and the `service_role` (secret) key.
4. Optional, for local testing: **Authentication → Sign In / Providers → Email**,
   turn off "Confirm email" so new accounts can sign in straight away.

### 2. Environment files

Type the keys into these files yourself; never paste them into a chat.

```bash
cp backend/.env.example backend/.env          # SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY
cp web/.env.local.example web/.env.local      # NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_ANON_KEY
```

The `service_role` key goes **only** in `backend/.env`.

### 3. Dependencies (already installed on this Mac)

```bash
python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -e "backend[dev]"
npm --prefix web install
```

## Run

```bash
./dev.sh
```

Then open http://localhost:3100, create an account and sign in. The page shows the
NSE market clock, your email, PAPER mode and the ₹10,00,000 paper capital (made
for you by a database trigger when the account is created).

## Test

```bash
backend/.venv/bin/python -m pytest backend
npm --prefix web run lint
```

## Ports

TradingBot uses 3100 and 8100 because 3000 and 8000 are taken by another project
on this Mac.
