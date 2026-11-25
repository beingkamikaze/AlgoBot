# XAUUSD plan of action

Draft 2, 7 November 2026. XAUUSD is the focus and gets its own separate setup, built to take other global instruments later.
MCX gold is parked until XAUUSD is working. Legal questions are with your lawyer. Nothing here is built yet.

The house rules stay: paper first, every signal says why, nothing trades without your confirmation.

---

## 0. The short version

- **Separate setup:** a new `backend/app/global_markets/` package with its own pages, tables and strategies. The NSE code is
  left alone, so NSE cannot break because of this work. Only pure maths is shared.
- **In dollars, around the clock:** a USD paper account, trading in lots and ounces, with a clock that follows London and New
  York time, including daylight saving.
- **Its own strategies:** built for gold's sessions, not copied from NSE.
- **Better history is possible:** real spot data with bid and ask from Dukascopy, if a slow download holds up (section 2).
- **An honest expectation:** every NSE strategy lost after charges, and that says nothing about gold either way. Gold signals
  grade **low** until gold's own evidence says otherwise, using the same confidence rule. "Nothing passes" is a likely first result.

---

## 1. What XAUUSD looks like (measured today)

| | Measured | From |
| --- | --- | --- |
| Price | about $4,075 an ounce | `GC=F`, 7 Nov |
| Median day's range | 1.97 % (about $80); one day in ten above 3.11 % | `GC=F`, 61 sessions |
| Median 5-minute true range | 0.092 % (about $3.75) | `GC=F` |
| Busiest hours (IST) | 18:00 to 21:00, peak at 19:00 | `GC=F` volume |
| Spread, London and New York hours | about $0.55 to $0.60 | Dukascopy, **only 2 days** (1–2 Oct) |
| Spread, Asian hours | about $0.68 to $0.75 | same |
| Spread, around the daily close | median $0.73, one minute in ten above $0.94 | same |

`GC=F` is the futures price, used here only for the shape of the day.

### Costs and sizing

The ₹2,000 per trade NSE limit is about **$20** at today's USD/INR of 93.9. One 0.01 lot is usually 1 ounce, so $1 of movement
is $1 per 0.01 lot.

| Stop | Why that size | Size for $20 risk | Spread cost in R (at $0.60) |
| --- | --- | --- | --- |
| $7.50 | 2 × median 5-minute range | 0.02 lot (2 oz) | 0.08R |
| $14 | 2 × the 19:00 IST range | 0.01 lot (1 oz) | 0.04R |

That compares with 0.13R to 0.22R for a Nifty lot, so gold is cheaper on paper. Three cautions: the spread is from only 2 days of
one provider and your broker's will differ; any commission comes on top; and **slippage and spreads around news are not included**.

---

## 2. Data: the main decision

| Need | Source | Status |
| --- | --- | --- |
| **History for research** | Dukascopy free 1-minute bid and ask candles | Works one file at a time, about 1 to 2 minutes a day of history with its 503 errors and waits. The first 5 days match `GC=F` (5-minute moves correlate 0.996, steady gap about $9.45). Years of history are still unproven |
| History fallback | `GC=F` on Yahoo: 5-minute for 60 days, hourly for 2 years | Works, but it is futures, not spot |
| **Live prices for paper** | Twelve Data XAU/USD | API answers and needs a key. Free tier is reported as 8 calls a minute and 800 a day, possibly delayed. Not tested |
| Live fallback | `GC=F`, shown clearly as futures | Works, and sits at a different price level from spot |
| Not available | Yahoo `XAUUSD=X` and `XAU=X`; Stooq | Both Yahoo symbols return "not found"; Stooq's page was gone |
| Later | Your broker's own prices | These are what you would actually trade on |

**Plan:** a slow, polite Dukascopy download that runs in the background, resumes where it stopped and keeps everything in the
local bar store. It is an unofficial source, so check their terms before relying on it. If it will not hold up, research falls
back to `GC=F` and slower strategies, because 61 sessions is too few to judge a 5-minute strategy.

800 calls a day allows about one price check every 2 minutes around the clock, or every minute during London and New York only.
That is fine for 5-minute signals, but paper fills would be approximate.

---

## 3. The separate setup

```
backend/app/global_markets/
  instruments.py   XAUUSD: 100 oz a lot, 0.01 lot minimum, $0.01 tick. Add a new instrument here
  clock.py         open Sun 18:00 to Fri 17:00 New York, 1-hour daily break, sessions, holidays
  data/            Dukascopy backfill, Twelve Data live, Yahoo fallback, bar store
  costs.py         spread, commission, overnight swap
  strategies/      gold's own strategies
  backtest.py      bid/ask fills, next-bar entries, stop before target
  risk.py          limits in dollars
  paper.py         USD account, orders, fills, daily rollover
  signals.py       "why this trade" cards
  api.py           /api/global/...
```

**Shared without changes:** `indicators/core.py`, `core/manage.py` (stop rules), `domain/signal.py`, the AI client and number check,
and the Lab's pass rule. The report is the package's own `report.py`, because `backtest/metrics.py` loads NSE's clock; it uses the
same pass gates, and a test keeps them equal. **Small additions outside the package:** one router and one loop in `main.py`, and
a nav group in `app-shell.tsx`.

**Database:** migration `0006_global_markets.sql`, safe to re-run, with new tables for account, limits, orders and trades. No
existing table changes. Bars stay in local files.

**Web:** a new **Global** group: Chart, Signals, Backtests, Paper. Prices and P&L in dollars, time shown in IST with the session
name, using the existing design tokens.

### Starting settings

Set now from volume and time of day, **not from results**. All are changeable.

| Setting | Value | In IST today |
| --- | --- | --- |
| Paper capital | $10,000 (about the same as your ₹10 lakh) | |
| Max loss per trade / day / month | $20 / $60 / $300 (your NSE limits converted) | |
| Trades a day, losses in a row | 5, 3 (same as NSE) | |
| Entries from | 08:00 London | 13:30 |
| Last entry | 11:00 New York | 21:30 |
| Flat by | 13:00 New York | 23:30 |
| Overnight holding | None at first, so no swap or weekend gaps | |
| Daily limits reset | 17:00 New York | 03:30 |

Rules are set in London and New York time so a backtest means the same thing all year. IST times shift by an hour when those
clocks change, at different dates.

---

## 4. Strategies

Rules are written and fixed **before any result is seen**. Choose on the first 70 % of sessions, never look at the last 30 %
while choosing, then one Lab test each, verdict reported as is.

| Strategy | Idea | Why gold |
| --- | --- | --- |
| Asian range breakout | Mark the range of the quiet Asian hours, trade a close beyond it after London opens | Asian hours are quiet, then activity picks up |
| New York open range | First 30 minutes from the COMEX open at 08:20 New York, trade a close beyond it | The busiest hour of the day |
| Failed break of yesterday's high or low | Price breaks the level, then closes back inside: trade back into the range | Tests whether breaks of obvious levels tend to fail |

**Fixed on 7 Nov 2026, before any strategy ran:** all three target 2R. The two breakouts use a stop 2 × ATR(14) from the close; the
failed break's stop is 0.5 × ATR beyond the failed move. Research days are 4 Jan 2021 to 5 Feb 2025, and **held-back days start on 6 Feb
2025** (`research_window.py`, 70 % of the 1,510 sessions). Recent `GC=F` data falls in the held-back days and moves almost exactly with
spot, so it is not used for strategy results either.

Possibly later: a slower hourly trend strategy, held overnight, once swap costs are modelled.

The first versions have no filters. Possible later filters are gold volatility `^GVZ`, dollar index `DX-Y.NYB` and session. A fixed
no-entry window around US CPI, jobs and Fed releases comes with the macro calendar in X6; it is set once and never tuned, and adding it
makes a new strategy version.

---

## 5. Phases

| Phase | Work | Done when |
| --- | --- | --- |
| **X0. Your inputs** | Twelve Data key typed into `backend/.env`; your broker choice if you have one | Live prices answer |
| **X1. Foundation (done 7 Nov 2026)** | Package, instrument, clock with tests for daylight saving, Sunday open and daily break | Clock tests pass; all NSE tests still pass |
| **X2. Data and chart (built 7 Nov 2026; long history still downloading, Twelve Data untested)** | Backfill, live feed, fallback, Global chart page | Chart matches a second source; history depth known and reported |
| **X3. Costs and backtester (done 7 Nov 2026)** | Bid/ask fills, spread by hour, slippage, Friday and holiday handling | Known hand-worked trades match; future-data test passes |
| **X4. Strategies and first result (built 7 Nov 2026; first result 8 Nov on 150 research days: nothing passes)** | The three strategies, first 70 % only | Report published as it comes out |
| **X5. Risk and paper (built 9 Nov 2026; checked on Supabase 10 Nov up to expiry; a live fill and exit still to watch)** | Migration, USD account, tickets, confirm step, kill switch, loop | Replayed session runs end to end; test rows removed |
| **X6. Signals and AI (built 10 Nov 2026; live scanner added at your request, needs migration 0008)** | Why cards, news and macro calendar, post-mortems, reviews with no NSE language | A paper trade gets a correct card and post-mortem |
| **X6b. Agent (built 22 Nov 2026, your choice; paper only, needs migration 0009)** | GPT-5 strategist and reviewer on live gold, code checks, ideas become tickets you confirm, a forward track record | Ideas appear with reasons; a ticket from one is sized by the risk check and needs your confirmation |
| **X6c. Telegram approval (built 23 Nov 2026; paper only, needs migration 0010 and a bot token)** | Ideas and tickets on your phone with buttons, two taps to confirm, fills and exits sent, one linked private chat | A tap from your chat confirms a paper ticket after a fresh risk check; taps from other chats do nothing |
| **X7a. MT5 adapter (built 25 Nov 2026 against a fake MT5; demo only)** | Windows bridge + app client/broker, tested end to end with a fake terminal | Connects to a demo account and shows its spread; places, finds, closes and reconciles demo orders. Live use waits for the legal answer |
| **X7b. MT5 demo routing (built 25 Nov 2026 against the fake MT5; needs migration 0011)** | Confirmed tickets can go to the MT5 demo account; a sync records broker fills and exits; reconcile; same limits and kill switch | A confirmed ticket fills at the demo broker and its exit, profit and charges match the terminal |
| **X7. Lab** | One test per strategy on the held-back 30 %. The monthly test count stays shared with NSE, so the bar keeps rising | Verdicts recorded, failures too |
| **X8. Live (your choice of broker)** | Broker adapter behind the same interface as Kite: PASS, your confirmation, stop at the broker, reconciliation, kill switch | Fake-broker tests pass, then a smallest-size real order, when you choose |

X1 to X3 need nothing from you except the key, and `GC=F` can stand in until it arrives.

**Broker note for X8:** this choice shapes the work. MT5's official Python package runs on Windows only, so a Mac would need a
Windows server; a REST or cTrader API would not. I have not checked which brokers accept your account, so tell me when you and
your lawyer have chosen one.

---

## 6. Risks that are special to XAUUSD

- **News jumps:** CPI, jobs and Fed releases can jump past a stop, so the loss can exceed the planned $20.
- **Your broker's prices are its own:** spreads, spikes and stop fills differ from any data feed. Backtest and paper results are
  only an estimate until compared with the broker's feed.
- **Leverage:** CFD brokers offer very high leverage. The app sizes from max loss only and caps capital in use.
- **Long hours:** the backend must be running through 23:30 IST, so your Mac must stay awake or paper exits are missed. A small
  server fixes this, and it matters much more once real money is involved.
- **Late-night decisions:** the journal shows results by hour so this cost is visible.

## 7. MCX gold, later

Parked; nothing here depends on it. Notes for when it returns: GOLDM is 100 g and GOLDTEN 10 g (broker summaries, not MCX's own
specs), Zerodha squares off MCX intraday positions 10 minutes before the close, and it would be priced in rupees through Kite.

## Sources

- Twelve Data free tier (secondary, not their own pricing page): [CodeWords](https://www.codewords.ai/blog/twelve-data-api),
  [LobeHub listing](https://lobehub.com/mcp/sherajdev-twelvedata-mcp-server)
- Measured today by direct requests: Yahoo `GC=F`, `^GVZ`, `DX-Y.NYB`, `USDINR=X`; Dukascopy `XAUUSD` 1-minute bid and ask.
