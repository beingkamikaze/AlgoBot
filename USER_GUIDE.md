# TradingBot user guide

TradingBot helps you trade NSE stocks (intraday) and Nifty / Bank Nifty futures and options with fixed rules.
Every signal says **why**, every order passes a **risk check**, and nothing is ever placed without **your
confirmation**. You start on **paper** (simulated money). Real money through Zerodha comes later, when you choose.

This guide explains what each screen is for and the order to use them in.

---

## 1. Starting the app

In a terminal, inside the `TradingBot` folder:

```bash
./dev.sh
```

Open **http://localhost:3100** and sign in. The backend runs on port 8100; leave the terminal open.

The top bar always shows:

| Item | Meaning |
| --- | --- |
| **NSE open / closed** | Market status, and when it next opens |
| **Clock (IST)** | India time; every time in the app is IST |
| **PAPER** (amber) | Orders are simulated. No real money moves |
| **LIVE ARMED** (red) | Live trading is armed for today: tickets can send real orders to Zerodha |
| **Backend offline** (red) | The backend is not running: start `./dev.sh` |

---

## 2. The screens, and what each one is for

The sidebar groups the screens by what you are doing.

### Overview

**Dashboard**: your morning and end-of-day view.
- Capital, today's and this month's profit after charges, open risk, free cash.
- Equity curve and daily P&L from your closed paper trades.
- Risk limits used today (bars turn amber at 70%, red at 100%).
- Open positions, recent trades, strategy results (backtest vs. your own paper record).
- Index and watchlist quotes (from Kite when you are logged in to Kite today, else Yahoo, delayed: the badge says which).
- **Pre-market brief**: press *Write today's brief* before 9:15 for an AI summary of index levels, India VIX,
  results dates, NSE filings and headlines for your watchlist. Headlines are leads, not facts.

### Markets

**Chart**: look at a stock or index before you trade it.
- Search any NSE symbol; pick a timeframe (1m, 5m, 15m, 1h, 1d).
- Toggle indicators: EMA, VWAP, Supertrend, Bollinger, opening range, previous-day levels, RSI, MACD, ATR.
  Hover an indicator name for what it does.
- The **watchlist** on the right is also used by the morning brief. Add a symbol by searching it; remove with ×.

**Options**: option chain and a position builder for Nifty, Bank Nifty or any NSE F&O stock (type it, e.g. TITAN) (paper only).
- Top strip: spot, forward, PCR, max pain, ATM IV, lot size (hover the dotted terms for explanations).
- Chain: shaded rows are in the money; bars show open interest; the highlighted row is at the money.
- **Build a position**: pick a structure (buy call/put, spreads, iron condor, iron fly, straddle, strangle),
  lots and strikes, then *Preview position* for legs, max profit/loss, breakevens, margin, Greeks and a payoff chart.
- *Paper trade this* creates a risk-checked ticket.
- Safety rules: naked short options are blocked unless you allow them in your limits; legs with a bid-ask spread over
  10% are blocked; no new option positions after 14:30 on expiry day.
- **Chain recorder** (the small line under the page title): while the backend runs, it saves a snapshot of the NIFTY
  option chain every 5 minutes during market hours (9:15 to 15:30 IST on trading days): the nearest two expiries,
  every strike within 10% of spot, with bid, ask, last price, NSE's IV, open interest and volume, plus spot and the
  forward. It is research data for later (Kite keeps no history of expired options), saved on this computer in
  `backend/data/chain_snapshots/`, one file per day; nothing goes to Supabase. The line shows how many snapshots were
  saved today and the time of the last one. A failed try is skipped and shown in amber; if NSE refuses, it waits
  longer before asking again. Turn it off with `CHAIN_RECORDER_ENABLED=false` in `backend/.env`. It only records
  while the backend is running, so days the app was closed have no snapshots.

### Trade

**Signals**: find trades. This is where most trades start. It has two modes (top right).

**All strategies** (the default): opens with a ranked list, no choices needed.
1. When you open the page it scans **every strategy** on the **Nifty 100** stocks and the **Nifty / Bank Nifty
   futures**, with each strategy's default parameters plus every Lab version you approved. It takes about a minute;
   *Scan everything* runs it again, *Replay today at* shows an earlier moment, and *Refresh every 5 min* keeps it
   current while the market is open.
2. Signals become **trade ideas**: one row per stock and direction, listing the strategies that agree.
   Repeats of the same signal are dropped.
3. The list is **ranked**: confidence first (high, medium, low, no track record), then how many strategies agree,
   then fresh signals, then the strategy's record after costs.
   A ⚠ / **conflict** mark means another strategy says the opposite on the same stock.
4. Filter by confidence, side, fresh only, hide conflicts, or one strategy.
5. Click a row to open each strategy's full **signal card** (below) and paper-trade from it.

Why parameters are not chosen automatically at scan time: picking whatever settings fitted recent bars best would
always look good and then fail on new days (overfitting). Better parameters come only through the **Lab**, which
tests them on days they were not tuned on; once you approve a version there, the automatic scan includes it.

**Nifty and Bank Nifty options today** (top of the ranked list): for every index signal of the session, the exact option
to trade (and the future), worked out as below. If no strategy fired on Nifty or Bank Nifty, it says so.

**Futures and options for any signal.** Stocks with an **F&O** badge have futures and options on NSE. Open a row (or press
*Futures & options* on a signal card) to see:
- **Option**: *BUY* the at-the-money **call** for a buy signal, the at-the-money **put** for a sell signal, e.g.
  `BUY TITAN26NOV4450CE`. Expiry: the nearest one more than 2 days away (weekly for Nifty, monthly for Bank Nifty and
  stocks). It shows the premium, the cost of one lot, the estimated result if the stock reaches the signal's stop or
  target, delta, IV, time decay per day, open interest, and liquidity warnings. The most you can lose is the premium.
- **Future**: the current month's contract (e.g. `TITAN26NOVFUT`), one lot, its exposure, margin, and the loss at the stop
  and gain at the target after charges.
- Whether each fits your **F&O max loss**, and if not, the limit it would need.
- *Paper trade* buttons that create a normal risk-checked ticket. The option ticket exits at the signal's stop or target
  on the stock or index, at 15:15, or if the premium halves.
- *Other strikes and spreads* opens the Options page for that stock or index.

Keep in mind: the confidence on the card is for the stock or index signal. Futures are leveraged (one TITAN lot is
175 shares, about ₹7.8 lakh), and options also lose to time decay and the spread, so their results differ. If the price is
already past the signal's stop, the panel says the setup has failed and offers nothing to trade.

**One strategy**: the manual scan. Pick a strategy, a Lab version, a universe and *Scan now*.

Each **signal card** shows:
- entry, stop, target and reward : risk;
- **why**: the rule that fired, with each condition ticked;
- **the plan**: size, max loss including charges, exit-by time;
- **the evidence**: the strategy's backtest record after costs, and similar past setups;
- **risks** and what cancels the trade;
- a **confidence** grade from a fixed rule (high / medium / low / no track record).

Buttons on a card:
- **Paper buy / Paper sell**: makes an order ticket (nothing is placed yet).
- **Explain in plain words**: AI summary written only from the card's facts.
- **Similar trades I took**: your own past trades that looked like this one.
- **Chart**, and for futures signals **Trade with options**.

The **TradingView alerts** inbox (beside or below the list) shows tickets created by your TradingView alerts.

> A signal that fires is not a recommendation. If the evidence is negative after costs, the card says so.
> As of October 2026, every stock strategy backtests negative after costs: treat their signals as practice until one passes.

**Trade monitor**: watch an open trade live (open it from *Monitor* on a position, or from the sidebar).
- A live chart of the stock or index with your **entry** (arrow), **stop** and **target** lines, nearby **support and
  resistance** (yesterday's high / low / close, pivots, today's opening range, today's swing highs and lows), EMA 20,
  Supertrend and the opening range. Entry, stop and target always fit on the chart. Switch 1-minute / 5-minute.
- **Live read**, re-checked every 10 seconds: health (*on track*, *watch*, *at risk*, *thesis broken*), where price is in R
  (now, best and worst since entry), a stop-to-target track, time in the trade and to 15:15, and for options the premium,
  P&L, delta and time decay.
- **Is it following the plan?** Fixed rules, each with its numbers:
  - **thesis**: the strategy's own reason, re-checked (ORB: price stays outside the opening range; PDH/PDL: beyond
    yesterday's high / low; VWAP: on its side of VWAP; Supertrend: has not flipped);
  - **trend**: EMA 20 and Supertrend on 5-minute bars agree with the trade;
  - **momentum**: the last 15 minutes went with the trade;
  - **sudden move**: no 1-minute bar against the trade bigger than 2.5× the usual range;
  - **near the stop**: price is not within 0.4R of the stop;
  - **room to target**: no support or resistance level close in the way;
  - **usual path**: not behind the strategy's winners' usual time to target (from its backtest);
  - **time**: more than 20 minutes left before 15:15.
- **Alerts**: the moment a check turns against the trade or its health gets worse, a notification appears on whatever page
  you are on (and on your desktop if you press *Turn on desktop alerts*). Click it to open the monitor.
- It never closes a trade by itself: you decide, with *Close this position*.

**Paper trading**: your simulated account.
- Open positions (with *Monitor* and *Close*), working orders (with *Review* / *Cancel*), closed trades.
- A ticket you do not confirm within 15 minutes **expires** (its price is stale by then); pressing *Paper trade* again on
  the same order shows the waiting ticket instead of making a copy.
- **Order ticket**: type a symbol, buy or sell, a stop (required) and an optional target, then *Get ticket*.
  The size is calculated from your max loss per trade.
- **Risk usage** bars and **Your limits** (edit and *Save limits*). Defaults: ₹2,000 max loss per trade,
  ₹6,000 per day, ₹30,000 per month, 5 trades a day, stop after 3 losses in a row, 3 open positions,
  50% of capital in use, entries 09:30–14:45, flat by 15:15. A separate F&O max loss is optional.
- **Kill switch**: cancels every working order, closes every position (paper and live) and blocks new entries
  until tomorrow.
- *Check fills now* runs the simulator immediately (it also runs by itself every 20 seconds in market hours).

**Live**: real money through Zerodha. Off until you complete every step on this page (see section 5).

### Research

**Backtests**: test a strategy on history before trusting it.
- Pick a strategy, adjust parameters if you like (a change makes a new version), a universe, risk per trade and
  slippage, then *Run backtest*.
- Results are after Zerodha charges, entering at the next bar's open. The last 30% of days are held back as a check.
- A strategy **passes** only with at least 100 trades, an average of +0.10R or more per trade, and a profit factor
  of 1.3 or more.
- Read the equity curve, the in-sample vs. held-back table (if they disagree, it is probably luck), and the
  breakdowns by time, volatility, gap, side, month and symbol.

**Lab**: improve a strategy safely.
1. Write a hypothesis ("ORB works better with wider opening ranges") and change one or more parameters.
   Weekly reviews also suggest hypotheses; *Make it testable* turns a written idea into a parameter change.
2. *Test it*: the original and the change are backtested on the same days; only the held-back 30% decides.
3. A change **passes** only with ≥ 30 held-back trades, a positive average there, at least +0.05R better than the
   original, and better in enough random resamples (90% for the first test of the month, stricter with each test).
4. *Approve* a passed change: it becomes a new version you can choose on Signals and Backtests.
   After 20 paper trades, a version that loses is retired automatically.

**TradingView alerts**: turn your TradingView alerts into tickets (needs a paid TradingView plan).
1. Run a Cloudflare tunnel so TradingView can reach your Mac (the page shows the commands).
2. Create a **source** for each TradingView strategy; copy its webhook URL and message (the secret is shown once).
3. Paste both into the TradingView alert. Every alert must include a stop.
4. Alerts arrive as risk-checked tickets in the inbox (Signals and this page). **Nothing trades by itself**;
   a ticket you do not confirm expires after 15 minutes.
- *Send a test alert* checks the setup without TradingView.

### Review

**Journal**: learn from your trades.
- Every closed trade gets an automatic **post-mortem** from its facts. Add your own note and how you felt.
- **Ask the analyst**: questions about your trades, results, backtests or indicators ("Why did I lose this week?").
  It answers from your data and does not give buy or sell calls. Numbers it cannot verify are flagged.
- **Reviews**: *Today's review* after the close; *This week's review* on the weekend (it suggests hypotheses for the
  Lab once you have 20+ trades).

**Forward results**: each strategy's closed paper trades next to what its backtest expected. Forward trades are the
honest test: a strategy whose backtest looks good but fails here should not go live.
- One card per strategy, version and instrument (stocks, index futures, options): trades, trades a month, win rate,
  average R and total P&L after charges, worst losing streak and largest drawdown in R, a cumulative-R line, and the
  backtest's expected average R, win rate and trades a month beside them. Lab versions get their own cards.
- **Rules not followed** (trades taken with the paper risk override) and **No strategy linked** are counted on their own
  and never mix into a strategy's numbers. Link a trade to its strategy on the Trade monitor.
- The verdict only describes. Under 30 trades it says "too few trades to judge" and nothing more. From 30 trades it says
  whether the forward average R is inside a rough 95% range of what the backtest's own trades give over that many
  trades (a bootstrap), or above or below it. No grades and no setting suggestions.
- Options trades on a signal strategy are compared with that strategy's index-futures backtest, so R is not like for
  like; the card says so. Backtest trades a month count every signal across the backtest's symbols.
- The XAUUSD section does the same for gold paper trades, against the stored research-day reports (a rougher range,
  from the win rate and average win and loss). Pick a start date at the top; the default is your first paper trade.

**Indicator guide**: what each indicator measures, how traders use it, and where it fails.

---

## 3. A trading day, start to finish

| Time (IST) | Where | What to do |
| --- | --- | --- |
| Before 9:15 | Dashboard | *Write today's brief*. Check yesterday's results and that the kill switch is off |
| Before 9:15 | Chart | Look at Nifty, Bank Nifty, India VIX and your watchlist |
| 9:15–9:30 | | Wait: entries open at 09:30 (the opening range forms first) |
| 9:30–14:45 | Signals | Open the ranked list (tick *Refresh every 5 min*). Start from the top; open a row and read the cards: the why, the evidence, the risks |
| | Signal card | *Paper buy / sell* → read the **ticket** (size, max loss, risk check) → *Confirm paper order* or *Cancel* |
| | Trade monitor | Watch the trade live: chart with entry / stop / target, the live read and alerts. Stops, targets and the 15:15 exit are handled automatically |
| Any time | Paper trading | *Close* a position yourself, or the **Kill switch** if something is wrong |
| 15:15 | (automatic) | Every open intraday position is closed |
| After 15:30 | Journal | Read post-mortems, add your notes, *Today's review* |
| Weekend | Journal → Lab → Backtests | *This week's review*, test its hypotheses in the Lab, re-run backtests |

---

## 4. The life of one trade

```
Signal (Signals page, TradingView alert, manual ticket, or options builder)
  → Ticket: size from your max loss, then the RISK CHECK (PASS or BLOCK, with reasons)
  → You confirm (or cancel; an unconfirmed entry expires)
  → Working order → fills at a realistic price, Zerodha charges included
  → Open position: stop, target and 15:15 exit watched automatically
  → Closed: net P&L and R recorded, post-mortem written
  → Journal, reviews, Lab: what to change next
```

The **risk check** blocks a trade when, for example:
- there is no stop, or the loss at the stop (including charges) is over your max loss per trade;
- today's loss plus open risk would pass your daily limit, or you hit the trades-per-day or losses-in-a-row limit;
- it is outside the entry window, the market is closed, or the kill switch is on;
- there is not enough free cash or capital in use would pass your limit.

Getting **out** is never blocked.

**R** is the money at risk on a trade (entry to stop). +2R means you made twice what you risked; −1R means the stop
was hit. Results are always shown after charges.

---

## 5. Going live (real money)

Paper trade for a while first, and only go live with a strategy that has passed its backtest and Lab checks.

What you need once:
1. A **Kite Connect** subscription (₹500 a month) and a small **server with a fixed IP**: SEBI's rules require
   API orders to come from a registered static IP. Follow `infra/DEPLOY.md`.
2. Run `supabase/migrations/0005_live.sql` in Supabase.

Every trading day, on the **Live** page:
1. **Log in to Kite** (on kite.zerodha.com; SEBI requires a fresh login each day; the session ends at 06:00).
2. **Two-factor** for this app (set up once with an authenticator app, then enter the code when asked).
3. **Arm for today** (type LIVE). Arming ends at midnight, with *Disarm*, or with the kill switch.

Then a ticket shows a red **Real money · Zerodha** section:
*Check this as a live order* (re-sized with your live limits and real Kite funds) → *Send to Zerodha*.

Live safety, built in:
- Live limits start small: ₹500 per trade, ₹1,500 per day, 3 trades, ₹50,000 per order. The smaller of your paper
  and live limits always wins.
- After the entry fills, a **stop-loss order is placed at Zerodha immediately**. If Zerodha refuses it, the position
  is closed at once.
- Targets, 15:15, *Close* and the kill switch all use that same stop order, so there is never more than one exit
  order per position.
- The app compares its positions with Zerodha's every few seconds and alerts you on any difference.
- Options stay paper only for now.

Start with a few shares, watch the stop appear in Kite's order book, and compare the app's P&L with Zerodha's contract
note that evening.

---

## 6. Things to know

- **Market data**: log in to Kite each morning (Live page) and the app uses Kite for everything. A small badge on the
  chart, signals, monitor and dashboard says where prices came from:
  - **Kite live**: bars built from Kite's real-time ticks. From 09:00 to 15:35 the app streams NIFTY, BANKNIFTY,
    India VIX, the Nifty 100 and every instrument you hold (paper or live). After 15:40 it re-downloads the day's
    bars from Kite so a dropped tick never stays in the history.
  - **Kite**: Kite's own candles (real time), for anything not streaming (hourly and daily bars, other stocks).
  - **Yahoo, delayed**: the free fallback, used only when you have not logged in to Kite today. It can lag.
  The app never logs in for you: no password or TOTP is stored. Futures are still priced from their index on paper.
- **Option prices**: from Kite's real-time chain (bid, ask, depth, open interest; IV and Greeks are the app's own)
  when you are logged in, else NSE's public chain (delayed). Kite does not give the change in open interest, so that
  column is blank on Kite chains. If neither answers, the app shows **MODELED** prices: do not trade on those.
- **Margin and charges**: with a Kite login, futures and option tickets show Kite's exact margin (hedges count) and
  Kite's charges, labelled "(Kite)"; without one they show the app's estimate, labelled "(est.)". Stocks always need
  their full value in cash: the app never uses intraday leverage on stocks.
- **AI**: the analyst, briefs, summaries and post-mortems use GPT-5. They only state numbers they can check against
  your data, and flag any they cannot. There is a daily token cap (shown on the Journal page).
- **Run one backend only**: do not run the app on your Mac and on a server at the same time against the same
  Supabase project.

## 7. If something looks wrong

| You see | Do this |
| --- | --- |
| "Backend offline" in the top bar | Start `./dev.sh` in the TradingBot folder |
| "Sign in required" | Sign in on the Dashboard |
| "Database temporarily unavailable" | A short Supabase hiccup: try again in a few seconds |
| A ticket says **BLOCK** | Read the reasons on the ticket; change the stop or your limits only if it makes sense |
| No signals | The rule did not fire on that universe today. Try another universe or *Replay today at* |
| Live page shows red steps | Each red line says exactly what is missing; fix them top to bottom |

---

## XAUUSD chart (Global)

Gold has its own setup, separate from NSE: a chart, backtests and a paper account. No signals yet.

- **Spot or Futures:** Spot is the real gold price. Futures is COMEX gold from Yahoo: always current and free, but at a slightly different price, and it can jump when the contract rolls. The app never joins the two.
- **Live spot:** needs a free key from twelvedata.com. Type it into `backend/.env` as `TWELVEDATA_API_KEY=...` and restart. Without it, Spot stops at the last downloaded day.
- **Spot data panel:** how many days of history are stored and how many are left. The download is slow on purpose, only runs while the app is running, and picks up where it stopped.
- **Times** are in IST. Gold's hours follow New York, so they shift by an hour when US clocks change.

## XAUUSD paper (Global)

A separate $10,000 paper account in dollars. NSE paper, its limits and its kill switch are not affected.

- **First time:** run `supabase/migrations/0006_global_markets.sql` in the Supabase SQL editor. Until then the page says the tables are missing.
- **Order ticket:** choose Buy or Sell, type the stop (and a target if you want one), and say why. Leave lots empty and the app sizes the trade from your max loss per trade ($20) and the $10,000 position cap. 0.01 lot is 1 ounce, so $1 of movement is $1.
- **Stop and target** are on the price you would close at: the bid for a long, the ask for a short.
- **Risk check:** every ticket says PASS or BLOCK with the reasons, and runs again when you confirm. On paper you can hold to take a blocked ticket anyway; it is recorded as rules not followed. Market closed, kill switch, no fresh price and a stop already passed can never be taken.
- **Fills:** on 1-minute bars, at the ask for a buy and the bid for a sell, with an assumed $0.60 spread and $0.10 slippage. An entry not filled within 10 minutes of bar time, or after the entry window, expires.
- **Exits:** stop, target, your Close button, or the flat-by time (13:00 New York, earlier on short days). Nothing is held overnight.
- **Prices:** spot from Twelve Data when `TWELVEDATA_API_KEY` is set; otherwise COMEX futures (`GC=F`), about 10 minutes late and a few dollars away from spot. Each trade keeps one source for its whole life.
- **Times:** entries 08:00 London to 11:00 New York (13:30 to 21:30 IST in winter). The trading day, and the daily limits, roll at 17:00 New York (03:30 IST in winter).
- **Kill switch:** cancels tickets and working entries, closes open positions at the next bar and blocks entries until the next trading day. Only for the global account.
- **Why this trade:** every ticket shows a card built by the app from facts: your reason (it asks you to write one), the plan in dollars, where the entry sits against today's range, the Asian range and yesterday's high and low, gold's typical 5-minute move against your stop, US releases before flat-by and the latest gold headlines. Confidence is always low: a manual trade has no tested strategy behind it.
- **US releases:** CPI, the jobs report and Fed decisions come from the official BLS and Federal Reserve schedules (listed through December 2027). A ticket within 30 minutes before or 15 minutes after one, or that could stay open through one, gets a warning. They warn and never block.
- **Post-mortem and notes:** click a closed trade to see its card, the post-mortem (written in the background when it closes: GPT-5 from the facts only, or a plain version if the model is off or quotes a number that is not in the facts) and your notes. Needs `supabase/migrations/0007_global_journal.sql` run once.
- **Signals (XAUUSD signals page):** the three gold strategies, at the settings fixed on 7 Nov, checked on every closed 5-minute bar between 08:00 London and 11:00 New York. Each signal shows its rule, conditions, stop, target in R, what cancels it, its why card and its confidence from the fixed rule on the research-days backtest. With `GC=F` a signal is seen about 10 minutes after its bar closed, later than the backtest entered, and it says so. You have 5 minutes to make a paper ticket from it; the ticket still goes through the risk check and your confirmation. None of these strategies has passed its backtest. Needs `supabase/migrations/0008_global_signals.sql` run once.
- **Agent (XAUUSD agent page):** GPT-5 watches live gold inside the entry window and looks when something happens: price crosses the Asian or yesterday's high or low, a big 5-minute bar, a 15-minute or 1-hour trend turns, a session opens, a headline lands, a US release comes out, or an hour passes. It proposes one trade (side, a stop where the idea is wrong, a target, plain reasons) or passes, which is its normal answer. App checks turn down a stop on the wrong side or too close or far (0.8 to 4 typical 5-minute moves), a reward under 1.5 times the risk, or reasons quoting numbers not in the facts. A reviewer agent argues against each idea and can turn it down; it cannot approve. An idea lasts 10 minutes; "Make paper ticket" prices it now (refused if price has moved more than 0.3 of its risk), the risk check sizes it from your max loss, and you confirm. "Ask the agent now" makes it look straight away. At most 16 looks a day, one every 15 minutes, set in `backend/.env`. The track record shows how every idea would have ended if taken, with the paper spread and slippage. These ideas are untested and grade low. Needs `supabase/migrations/0009_global_agent.sql` run once.
- **Approve from Telegram (XAUUSD agent page, right column):** make a bot with @BotFather, put its token in `backend/.env` as `TELEGRAM_BOT_TOKEN`, restart the backend, and press Connect Telegram. The app gives a one-time code (10 minutes); the link opens your bot with `/start <code>`, which links that private chat only. Live trade ideas then arrive with **Make paper ticket** and **Skip**. A ticket shows the size, max loss and the risk check; when it passes you tap **Confirm paper trade**, then **Yes, take it (paper)**: two taps, and the risk check runs again on a fresh price. A blocked ticket can only be rejected on the phone; a paper override stays on the app. Fills, exits and expired orders are sent too. `/stop` in the chat unlinks it. Taps from any other chat do nothing. Needs `supabase/migrations/0010_telegram.sql` run once. Run only one backend with the token at a time.
- **Reviews:** "Review today" and "Review the week" sum up your closed gold trades, by the trading day that rolls at 17:00 New York. They never suggest strategy changes: that goes through the Lab.
