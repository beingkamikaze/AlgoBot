# TradingBot: notes for Claude

NSE intraday trading app: stocks, Nifty / Bank Nifty futures and options, and F&O stocks. Paper trading now; live through
Zerodha Kite is built but off. Read `USER_GUIDE.md` (what each screen does), `README.md` (setup, status by phase) and
`IMPLEMENTATION_GUIDE.md` (original design) as needed.

XAUUSD is the current focus, as a separate setup in `backend/app/global_markets/` with its own strategies (`XAUUSD_PLAN.md`).
It must not import NSE modules such as `app.core.calendar` (a test checks this). MCX gold is parked; legal questions are with the
user's lawyer. Gold's held-back days start 6 Feb 2025 (`global_markets/research_window.py`): never move that date, and do not run
strategies on later prices (including `GC=F`) outside the one Lab test. One exception, the user's choice on 10 Nov 2026: the
live signal scanner (`global_markets/scanner.py`) at fixed settings; its signals and the paper trades taken from them are never
used to choose settings. A second exception, the user's choice on 22 Nov 2026: the gold agent (`global_markets/agent/`, paper only). GPT-5 makes
up trades from the live market, a reviewer agent and code checks can veto, the risk engine sizes, the user confirms. Its ideas
cannot be backtested (the model has seen past prices), so they always grade low, their only evidence is the desk's forward
record, and they are never used to choose settings. Agents never size, approve or place orders.

## Run and test

- Run: `./dev.sh`. Web on 3100, backend on 8100. Ports 3000 and 8000 belong to another project (PersonaAI): never kill them.
- Backend tests, from `backend/`: `.venv/bin/python -m pytest -q tests`. Web checks, from `web/`: `npx tsc --noEmit && npx eslint src`.
- Stack: FastAPI (Python 3.12, `backend/.venv`), Next.js 16 + Tailwind 4 (`web/`), Supabase (auth, Postgres, pgvector), Azure OpenAI GPT-5.
- Run only one backend against the Supabase project at a time: the paper and live loops run inside it.

## NSE market data: Kite first

- With a Kite login today, all NSE data is Kite: `market/feed.py` (bars: Kite ticks -> Kite historical -> Yahoo;
  each `Candles.source` is `kite_live` / `kite` / `yahoo`), `market/ticks.py` (KiteTicker 09:00-15:35, bars from ticks,
  after-close correction), `options/kite_chain.py` (chain source `kite`; NSE scrape is the fallback), `risk/margin.py`
  (Kite order/basket margins; estimate is the fallback). Call `feed.candles` / `feed.quotes`, not `yahoo.*`, for NSE.
- Every Kite DATA call goes through `market/kite.py` (`KiteData`, throttled: quote 1/s, historical 3/s, other 10/s;
  500 per quote call; instruments cached daily in `backend/data/kite/`). Orders stay in `live/broker.py`.
- Never automate the Kite login. Tests never reach real Kite (`tests/conftest.py`); use `FakeKiteConnect` /
  `FakeTicker` in `tests/fakes.py`. Backtests and the Lab still use the bar store as before.

## Working rules from the user

- Never ask for or print secrets. The user types keys into `.env` files; `service_role` goes only in `backend/.env`.
- Commit only when asked. As of 2026-11-07 the repo has no commits at all.
- Assisted trading: nothing trades without the user's confirmation, and TradingView alerts never auto-approve.
- Every signal must say why. The user prefers plain language.
- Paper-only risk override exists (hold to confirm, recorded as rules not followed). Live orders always need a PASS.
- Remove test rows created in the user's Supabase account after testing; keep `ai_usage`.
- Migrations are run by the user in the Supabase SQL editor; make them safe to re-run.

## Honest results: do not paper over these

- Every strategy loses after charges in backtest, so all signals grade **low**. Do not change the confidence rule
  (`backend/app/domain/signal.py`) to get better grades.
- 2026-11-03: about 110 parameter settings of ORB, VWAP pullback, Supertrend and PDH/PDL, judged on the first 70% of days:
  none profitable (best: Supertrend 4x ATR, EMA 200, -0.05R).
- Failed Lab tests in November 2026: `orb_fade` (a new mean-reversion strategy, -0.32R held back) and ORB with a 1 ATR trail
  plus a 60-minute time stop (-0.13R held back vs +0.07R default). The Lab's bar rises with each test in a calendar month.
- 2026-11-11: `intraday_momentum` (first half-hour predicts 14:45-15:15, index futures) lost at all 8 in-sample
  settings (default -0.47R, 96 trades); no Lab test spent. Kept for research with `auto_scan = False`.
- 2026-11-13: intraday Nifty short straddle (09:20 ATM, per-leg stop +25/30 %, optional move-to-cost, out 15:15) on
  ESTIMATED option prices (Black-76 + intraday VIX; `backend/app/fno_research/straddle.py`), in-sample 2021 to
  2025-02-04 only, no Lab test allowed. The verdict hangs on an unknown, how fast premium melts during the session:
  sellers' best-case clock +Rs 150-250 a trade, a clock fitted to where Nifty's variance falls -Rs 100-125, calendar
  time -Rs 650. Not profitable under all scenarios; expiry-day trades lose in every one. Real bhavcopy closes
  (`straddle_check.py`, 2021 days so far) put weekly ATM IV about 1 point below VIX. Only intraday chain snapshots can settle it.

- 2026-11-13: weekly Nifty iron condor on real end-of-day option prices (NSE bhavcopy, `backend/app/fno_research/`;
  rule fixed in `condor.py`, today's charges and 65 lot for every year, slippage on each leg). In-sample = trades entering
  AND settling before 2025-02-05 (212 weeks, all Thursday expiries). Entering 1 day before expiry made money at all 4
  settings (+0.35% to +0.57% of margin per week, +₹178 to +₹327 per lot), but almost all of it came in 2021-23: 2024 and
  Jan 2025 were about flat, and the worst drawdown was about 60-70% of one week's margin. Entering 2 or 3 days before lost
  at 8 of 8 settings. So the edge is fragile, sensitive to the holding period and may have faded. Tuesday expiries and
  SEBI's Nov 2024 changes are all inside the held-back window. No Lab test spent yet: the user decides
  (`fno_research/lab.py`, scored on % of margin vs cash).

## Strategy research discipline

Since 2026-11-11 the NSE bar store holds Kite history from 2021-01-01 (5m and 15m, Nifty 100 + NIFTY, BANKNIFTY,
INDIAVIX; `backend/app/market/backfill.py`), so the held-back 30% now starts around 2025-02-05. Research before that
date only saw Jul-Oct 2026, which now lies inside the held-back window: say so in write-ups.

Swing (multi-day, delivery) research lives in `backend/app/swing/` on daily bars from 2010 for all NSE stocks, with a
point-in-time "liquid 100" universe (`swing/universe.py`), a fixed held-back cutoff of 2021-10-20 (never move it), and
its own Lab path (`swing/lab.py`, scored in % per trade vs holding the universe). In-sample (2011 to Oct 2021) only
12-1 momentum beat holding the universe (+1.15% per trade, thin evidence); the user chose ONE Lab test of momentum
at its published default in December 2026.

Choose settings only on trades before the held-back cutoff (first 70% of sessions); never look at held-back results while
choosing; then run exactly one Lab test (`backend/app/lab/loop.py`) and report its verdict as is. Never tune parameters at
scan time: that fits noise.

## Web UI conventions

- Dark terminal design tokens in `web/src/app/globals.css`; do not use raw zinc / emerald / red classes. Teal = actions,
  green / red = gains and losses only.
- Shared components: `components/ui/kit.tsx` (Page, Panel, Stat, Pnl, RValue, HoldButton, `px()` for prices),
  `ui/feedback.tsx` (toasts, confirm dialogs), `ui/icons.tsx`. Chart colours: `lib/theme.ts`.
- In `globals.css`, `@apply` in a rule with a selector list is silently dropped: write plain CSS there.

## Not done yet

- Real Kite has never been tested (only a fake broker): it needs the user's Kite Connect subscription. Docker images are also untested.
