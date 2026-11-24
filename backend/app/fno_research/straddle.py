"""Intraday Nifty short straddle with per-leg stop-losses, on ESTIMATED option prices.

THIS IS AN ESTIMATE. The app has no intraday option-price history (Kite keeps no expired contracts and free intraday
option data does not exist), so every option price here is MODELLED: Black-76 on the NIFTY 5-minute index bars, with
India VIX (5-minute closes, so intraday IV moves are partly in) as the implied volatility. Known gaps:
  - VIX is a 30-day at-the-money vol. Weekly ATM options, above all near and on expiry day, trade at a different IV.
    Scenarios shift the IV by -2 / 0 / +2 vol points.
  - How time value melts during the day is unknown without real prices. Two clocks bracket it:
      "trading":  all of a day's time value goes while the market is open (sellers' best case);
      "calendar": time value goes evenly round the clock, so only ~6 of 24 hours fall in the session (sellers' worst).
    Both give the same price at 09:15; they differ only in how fast it decays through the session.
  - Spot index, not the future; no smile (both legs are ATM, so this matters less); no real bid/ask.

The rule, fixed before any test:
  At 09:20 IST (close of the 09:15 bar) sell 1 lot of the ATM NIFTY call and put on the nearest weekly expiry.
  Expiry day (0 days to expiry) is skipped unless `expiry_day` is on. Each leg stops at entry premium x (1 + S),
  checked on the estimated premium at 5-minute closes; stop fills pay extra slippage. After one leg stops, the other
  leg's stop optionally moves to its entry price ("move to cost"). Everything left is bought back at 15:15.
  Grid (8): S in {0.25, 0.30} x move_to_cost on/off x expiry_day on/off.

Research discipline: in-sample only. `in_sample_days` drops every bar from the NSE held-back cutoff (70 % of the bar
store's sessions, the Lab's rule, ~2025-02-05) BEFORE any pricing, so held-back results are never computed.
No Lab test may be run on this: it rests on estimated prices.

Run: `.venv/bin/python -m app.fno_research.straddle [out.json]` from backend/.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field

import numpy as np

from app.core.calendar import IST
from app.core.costs import options_charges
from app.options.pricing import DIV_YIELD, RATE

STRIKE_STEP = 50
LOT = 65                     # today's NIFTY lot, used for every year so rupee figures compare (history: 75/50/25/75)
ENTRY_HHMM = "09:15"         # the bar whose close (09:20) is the entry
EXIT_HHMM = "15:10"          # the bar whose close (15:15) is the exit
BARS_PER_DAY = 72            # 09:15 .. 15:10 bar starts
TUESDAY_FROM = dt.date(2025, 9, 1)   # Nifty weeklies moved from Thursday to Tuesday (first Tuesday expiry 2025-09-02)
EXTRA_ELM_FROM = dt.date(2024, 11, 20)   # SEBI: +2 % extreme-loss margin on short options on expiry day

ESTIMATE = "ESTIMATE (modelled option prices, not real ones)"


# ---------------------------------------------------------------- settings and scenarios

@dataclass(frozen=True)
class Setting:
    stop_pct: float = 0.25        # leg stop at entry x (1 + stop_pct)
    move_to_cost: bool = False    # after one leg stops, move the other leg's stop to its entry
    expiry_day: bool = False      # trade on expiry day (0 DTE) too

    @property
    def key(self) -> str:
        return f"S{int(self.stop_pct * 100)}{'_cost' if self.move_to_cost else ''}{'_0dte' if self.expiry_day else ''}"


@dataclass(frozen=True)
class Scenario:
    iv_shift: float = 0.0         # vol points added to VIX
    clock: str = "trading"        # "trading", "mixed" (calibrated) or "calendar"

    @property
    def key(self) -> str:
        return f"{self.clock}_iv{self.iv_shift:+g}"


@dataclass(frozen=True)
class Fills:
    half_spread_pct: float = 0.5  # paid on every fill, % of premium
    stop_slip_pct: float = 2.0    # extra on stop fills (market order into a moving option), % of premium
    trigger: str = "close"        # "close": stop checked at 5-minute closes (the rule); "intrabar": bar high/low


GRID = [Setting(s, m, e) for s in (0.25, 0.30) for m in (False, True) for e in (False, True)]
SCENARIOS = [Scenario(iv, c) for c in ("trading", "mixed", "calendar") for iv in (-2.0, 0.0, 2.0)]


# ---------------------------------------------------------------- calendar

def expiry_weekday(d: dt.date) -> int:
    return 1 if d >= TUESDAY_FROM else 3          # Tuesday from Sep 2025, Thursday before


def weekly_expiry(d: dt.date, sessions: set[dt.date]) -> dt.date:
    """Nearest Nifty weekly expiry on or after session `d`: the week's expiry weekday, or the session before it when
    that day is a holiday. `sessions` is the set of real trading days (from the bar store, which matches NSE's
    holiday lists 2021-2024)."""
    wd = expiry_weekday(d)
    nominal = d + dt.timedelta(days=(wd - d.weekday()) % 7)
    e = nominal
    while e not in sessions and e > d - dt.timedelta(days=7):
        e -= dt.timedelta(days=1)
    if e < d or e not in sessions:
        return weekly_expiry(nominal + dt.timedelta(days=1), sessions)
    return e


def _expiry_close(e: dt.date) -> float:
    return dt.datetime.combine(e, dt.time(15, 30), IST).timestamp()


def calendar_years(ts: float, expiry: dt.date) -> float:
    return max(_expiry_close(expiry) - ts, 0.0) / (365.0 * 86400)


def market_years(ts: float, expiry: dt.date, sessions: set[dt.date], night: float = 0.0, long_gap: float = 0.0) -> float:
    """Variance-weighted time to expiry, in sessions over 252: the rest of today's session, plus for each later
    session one unit and the closed gap before it (`night` after a single night, `long_gap` across a weekend or
    holiday). night = long_gap = 0 is pure open-market time (the "trading" clock)."""
    now = dt.datetime.fromtimestamp(ts, IST)
    day = now.date()
    if day > expiry:
        return 0.0
    close = dt.datetime.combine(day, dt.time(15, 30), IST).timestamp()
    left = min(max(close - ts, 0.0), 375 * 60) / (375 * 60)
    units, prev = left, day
    for k in range(1, (expiry - day).days + 1):
        d = day + dt.timedelta(days=k)
        if d in sessions:
            units += 1.0 + (night if (d - prev).days == 1 else long_gap)
            prev = d
    return units / 252.0


def trading_years(ts: float, expiry: dt.date, sessions: set[dt.date]) -> float:
    return market_years(ts, expiry, sessions)


def calibrate_mixed(days: list["Day"]) -> dict:
    """Where Nifty's variance actually lands, measured on the in-sample days: mean squared log return over a weekday
    night and over a weekend/holiday gap (15:15 close to the 09:20 close), each relative to a 09:20-15:15 session
    (scaled to the full 09:15-15:30 session). The "mixed" clock charges time value in these proportions, i.e. it
    assumes the market prices theta where the variance falls."""
    night, gap, sess = [], [], []
    for a, b in zip(days, days[1:]):
        g = math.log(b.c[0] / a.c[-1]) ** 2
        (night if (b.date - a.date).days == 1 else gap).append(g)
        sess.append(math.log(b.c[-1] / b.c[0]) ** 2)
    s_window = float(np.mean(sess))
    s_full = s_window * 375 / 355
    edge = s_full - s_window                     # 15:15-15:30 and 09:15-09:20 sit inside the measured gaps
    return {"night": round((float(np.mean(night)) - edge) / s_full, 3), "long_gap": round((float(np.mean(gap)) - edge) / s_full, 3),
            "session_var_bp2": round(s_window * 1e4, 4), "nights": len(night), "long_gaps": len(gap)}


# ---------------------------------------------------------------- pricing (Black-76, price only: fast)

def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def b76(call: bool, spot: float, strike: float, t: float, vol: float, t_carry: float | None = None) -> float:
    """Same model and rates as app.options.pricing.black76 (forward from spot), price only. `t` is the variance
    time; `t_carry` (default `t`) the calendar time used for the forward and discounting."""
    if t <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    tc = t if t_carry is None else t_carry
    f = spot * math.exp((RATE - DIV_YIELD) * tc)
    df = math.exp(-RATE * tc)
    sq = vol * math.sqrt(t)
    d1 = (math.log(f / strike) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    if call:
        return df * (f * _cdf(d1) - strike * _cdf(d2))
    return df * (strike * _cdf(-d2) - f * _cdf(-d1))


# ---------------------------------------------------------------- data

@dataclass
class Day:
    date: dt.date
    t: np.ndarray        # bar start, unix seconds; t[0] is 09:15, t[-1] is 15:10
    c: np.ndarray        # NIFTY close
    h: np.ndarray
    lo: np.ndarray
    vix: np.ndarray      # India VIX close at the same bar


def _by_day(bars) -> dict[dt.date, dict[str, object]]:
    out: dict[dt.date, dict[str, object]] = defaultdict(dict)
    for b in bars:
        d = dt.datetime.fromtimestamp(b.t, IST)
        out[d.date()][d.strftime("%H:%M")] = b
    return out


def held_back_cutoff() -> dt.date:
    """The NSE research cutoff: the Lab's 70 % rule over the bar store's sessions up to 2026-11-11, pinned in
    app.fno_research.calendar so new data never moves it (2025-02-05)."""
    from app.fno_research.calendar import CUTOFF
    return CUTOFF


def load(underlying: str = "NIFTY") -> tuple[list[Day], set[dt.date], dt.date, dict]:
    """In-sample days only. Bars on or after the cutoff are dropped here, before anything is priced."""
    from app.market import store
    nifty, vix = _by_day(store.read(underlying, "5m")), _by_day(store.read("INDIAVIX", "5m"))
    cut = held_back_cutoff()
    # the trading calendar: full sessions only (drops muhurat and Saturday special sessions; one 2021 glitch day)
    sessions = {d for d, b in nifty.items() if len(b) >= 60} | {dt.date(2021, 2, 24)}
    times = [(dt.datetime(2000, 1, 1, 9, 15) + dt.timedelta(minutes=5 * i)).strftime("%H:%M") for i in range(BARS_PER_DAY)]
    days, skipped = [], []
    for d in sorted(nifty):
        if d >= cut:
            break
        nb, vb = nifty[d], vix.get(d, {})
        if not all(k in nb and k in vb for k in times):
            skipped.append(d.isoformat())
            continue
        rows = [nb[k] for k in times]
        days.append(Day(d, np.array([r.t for r in rows]), np.array([r.c for r in rows]), np.array([r.h for r in rows]),
                        np.array([r.l for r in rows]), np.array([vb[k].c for k in times])))
    return days, sessions, cut, {"skipped_incomplete_days": skipped}


# ---------------------------------------------------------------- estimated premium paths

@dataclass
class Path:
    """Estimated premiums at each 5-minute close, for one day under one scenario."""
    strike: float
    expiry: dt.date
    dte: int
    ce: np.ndarray
    pe: np.ndarray
    ce_hi: np.ndarray    # call at the bar's high (its worst point for a short call)
    pe_hi: np.ndarray    # put at the bar's low
    spot0: float
    iv0: float           # entry IV in vol points, after the shift


def clock(day: Day, expiry: dt.date, sessions: set[dt.date], sc: Scenario, mixed: dict | None = None):
    """(time-to-expiry function of a timestamp, vol scale) for the scenario's clock. The trading and mixed clocks
    scale the vol so the 09:15 price equals the calendar-time price; only the decay through the day differs."""
    open_ts = float(day.t[0])
    if sc.clock in ("trading", "mixed"):
        w = (0.0, 0.0) if sc.clock == "trading" else (mixed["night"], mixed["long_gap"])

        def tt(ts: float) -> float:
            return market_years(ts, expiry, sessions, *w)
        return tt, math.sqrt(calendar_years(open_ts, expiry) / max(tt(open_ts), 1e-12))
    if sc.clock == "calendar":
        def tc(ts: float) -> float:
            return calendar_years(ts, expiry)
        return tc, 1.0
    raise ValueError(sc.clock)


def premium_path(day: Day, sessions: set[dt.date], sc: Scenario, mixed: dict | None = None) -> Path:
    expiry = weekly_expiry(day.date, sessions)
    spot0 = float(day.c[0])
    k = round(spot0 / STRIKE_STEP) * STRIKE_STEP
    tt, scale = clock(day, expiry, sessions, sc, mixed)
    n = len(day.t)
    ce, pe, ce_hi, pe_hi = (np.empty(n) for _ in range(4))
    for i in range(n):
        ts = float(day.t[i]) + 300                       # the bar's close
        t, tc = tt(ts), calendar_years(ts, expiry)
        vol = max(float(day.vix[i]) + sc.iv_shift, 1.0) / 100 * scale
        ce[i] = b76(True, float(day.c[i]), k, t, vol, tc)
        pe[i] = b76(False, float(day.c[i]), k, t, vol, tc)
        ce_hi[i] = b76(True, float(day.h[i]), k, t, vol, tc)
        pe_hi[i] = b76(False, float(day.lo[i]), k, t, vol, tc)
    return Path(k, expiry, (expiry - day.date).days, ce, pe, ce_hi, pe_hi, spot0, float(day.vix[0]) + sc.iv_shift)


# ---------------------------------------------------------------- margin (rough SPAN-like)

PRICE_SCAN = 0.06         # +-6 % index move
VOL_SCAN = 0.25           # vol up or down by a quarter (relative)
EXPOSURE = 0.02           # exposure margin, % of index notional, per short option leg


def margin_estimate(spot: float, strike: float, t: float, vol: float, qty: int, expiry_day: bool, date: dt.date) -> float:
    """A ROUGH stand-in for NSE SPAN + exposure on a short straddle: the worst loss over +-6 % index moves (15 steps)
    with vol up or down 25 %, plus 2 % of notional per short leg (plus 2 % more per leg on expiry day from
    2024-11-20). Zerodha's margin calculator is the real number; this aims at its order of magnitude only."""
    now = b76(True, spot, strike, t, vol) + b76(False, spot, strike, t, vol)
    worst = 0.0
    for m in np.linspace(-PRICE_SCAN, PRICE_SCAN, 15):
        s = spot * (1 + m)
        for v in (vol * (1 - VOL_SCAN), vol * (1 + VOL_SCAN)):
            worst = max(worst, b76(True, s, strike, t, v) + b76(False, s, strike, t, v) - now)
    exposure = EXPOSURE * spot * qty * 2
    if expiry_day and date >= EXTRA_ELM_FROM:
        exposure += 0.02 * spot * qty * 2
    return worst * qty + exposure


# ---------------------------------------------------------------- one day's trade

@dataclass
class Trade:
    date: str
    expiry: str
    dte: int
    strike: float
    spot: float
    iv: float
    entry_ce: float
    entry_pe: float
    exit_ce: float
    exit_pe: float
    exit_ce_reason: str       # "stop", "cost" (moved stop), "time"
    exit_pe_reason: str
    gross: float              # rupees, after spreads and slippage, before charges
    charges: float
    net: float
    margin: float
    stops: int = 0
    notes: list[str] = field(default_factory=list)


def simulate(day: Day, p: Path, st: Setting, fills: Fills = Fills(), qty: int = LOT, margin: float | None = None) -> Trade | None:
    if p.dte == 0 and not st.expiry_day:
        return None
    hs, slip = fills.half_spread_pct / 100, fills.stop_slip_pct / 100
    entry = {"CE": p.ce[0] * (1 - hs), "PE": p.pe[0] * (1 - hs)}          # we sell: receive a bit less
    stop = {leg: entry[leg] * (1 + st.stop_pct) for leg in entry}
    close_px = {"CE": p.ce, "PE": p.pe}
    worst_px = {"CE": p.ce_hi, "PE": p.pe_hi}
    exit_px: dict[str, float] = {}
    reason: dict[str, str] = {}
    moved: set[str] = set()
    last = len(p.ce) - 1
    for i in range(1, last + 1):
        hit = []
        for leg in ("CE", "PE"):
            if leg in exit_px:
                continue
            if fills.trigger == "close":
                if close_px[leg][i] >= stop[leg]:
                    hit.append((leg, close_px[leg][i] * (1 + hs + slip)))
            elif worst_px[leg][i] >= stop[leg]:
                hit.append((leg, stop[leg] * (1 + hs + slip)))             # an order resting at the stop
        for leg, px in hit:
            exit_px[leg] = px
            reason[leg] = "cost" if leg in moved else "stop"
        if hit and len(exit_px) == 1 and st.move_to_cost:
            other = "PE" if "CE" in exit_px else "CE"
            stop[other] = entry[other]                                      # checked from the next bar on
            moved.add(other)
        if len(exit_px) == 2:
            break
    for leg in ("CE", "PE"):
        if leg not in exit_px:
            exit_px[leg] = close_px[leg][last] * (1 + hs)                   # buy back at 15:15
            reason[leg] = "time"
    gross = sum(entry[leg] - exit_px[leg] for leg in entry) * qty
    ch = options_charges(sum(exit_px.values()) * qty, sum(entry.values()) * qty, buy_orders=2, sell_orders=2)
    return Trade(day.date.isoformat(), p.expiry.isoformat(), p.dte, p.strike, round(p.spot0, 2), round(p.iv0, 2),
                 round(entry["CE"], 2), round(entry["PE"], 2), round(exit_px["CE"], 2), round(exit_px["PE"], 2),
                 reason["CE"], reason["PE"], round(gross, 2), ch.total, round(gross - ch.total, 2),
                 round(margin or 0.0, 0), sum(1 for r in reason.values() if r != "time"))


# ---------------------------------------------------------------- summaries

def _max_dd(x: list[float]) -> float:
    eq, peak, dd = 0.0, 0.0, 0.0
    for v in x:
        eq += v
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    net = [t.net for t in trades]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v <= 0]
    margin = float(np.mean([t.margin for t in trades]))
    years = (dt.date.fromisoformat(trades[-1].date) - dt.date.fromisoformat(trades[0].date)).days / 365.25 or 1.0
    worst = min(trades, key=lambda t: t.net)
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(net), 3),
        "avg_net": round(float(np.mean(net)), 1),
        "avg_gross": round(float(np.mean([t.gross for t in trades])), 1),
        "avg_charges": round(float(np.mean([t.charges for t in trades])), 1),
        "avg_credit_pts": round(float(np.mean([t.entry_ce + t.entry_pe for t in trades])), 1),
        "total_net": round(sum(net), 0),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_drawdown": round(_max_dd(net), 0),
        "worst_day": {"date": worst.date, "net": worst.net, "dte": worst.dte, "exits": f"{worst.exit_ce_reason}/{worst.exit_pe_reason}"},
        "best_day": round(max(net), 0),
        "one_leg_stopped": round(sum(1 for t in trades if t.stops == 1) / len(trades), 3),
        "both_legs_stopped": round(sum(1 for t in trades if t.stops == 2) / len(trades), 3),
        "avg_margin": round(margin, 0),
        "return_on_margin_pa_pct": round(sum(net) / years / margin * 100, 1) if margin else None,
    }


def by_year(trades: list[Trade]) -> dict:
    g: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        g[t.date[:4]].append(t)
    return {y: {"trades": len(v), "avg_net": round(float(np.mean([t.net for t in v])), 1), "total_net": round(sum(t.net for t in v), 0)}
            for y, v in sorted(g.items())}


# ---------------------------------------------------------------- the study

def run(days: list[Day], sessions: set[dt.date], cut: dt.date, scenarios=SCENARIOS, grid=GRID, fills: Fills = Fills()) -> dict:
    assert all(d.date < cut for d in days), "held-back days must never reach the study"
    mixed = calibrate_mixed(days)
    out: dict = {"label": ESTIMATE, "held_back_from": cut.isoformat(), "in_sample_days": len(days),
                 "first_day": days[0].date.isoformat(), "last_day": days[-1].date.isoformat(), "lot": LOT,
                 "fills": asdict(fills), "mixed_clock": mixed, "results": {}}
    for sc in scenarios:
        paths = [premium_path(d, sessions, sc, mixed) for d in days]
        margins = []
        for d, p in zip(days, paths):
            t0 = calendar_years(float(d.t[0]) + 300, p.expiry)
            margins.append(margin_estimate(p.spot0, p.strike, t0, max(p.iv0, 1.0) / 100, LOT, p.dte == 0, d.date))
        for st in grid:
            trades = [t for t in (simulate(d, p, st, fills, LOT, m) for d, p, m in zip(days, paths, margins)) if t]
            out["results"][f"{st.key}|{sc.key}"] = {"setting": asdict(st), "scenario": asdict(sc),
                                                     "summary": summarize(trades), "by_year": by_year(trades),
                                                     "by_dte": {str(k): summarize([t for t in trades if t.dte == k])["avg_net"]
                                                                for k in sorted({t.dte for t in trades})}}
    return out


def table(res: dict) -> str:
    lines = [f"{ESTIMATE}. In-sample {res['first_day']} .. {res['last_day']} ({res['in_sample_days']} days), "
             f"held back from {res['held_back_from']} (never computed). 1 lot = {res['lot']}, rupees per trade.",
             f"{'setting':<16}{'scenario':<18}{'n':>5}{'win':>6}{'avg net':>9}{'gross':>8}{'chg':>6}{'PF':>6}"
             f"{'total':>10}{'maxDD':>10}{'worst':>9}{'margin':>9}{'RoM%pa':>8}"]
    for key, r in res["results"].items():
        s = r["summary"]
        st, sc = key.split("|")
        lines.append(f"{st:<16}{sc:<18}{s['trades']:>5}{s['win_rate']:>6.2f}{s['avg_net']:>9.0f}{s['avg_gross']:>8.0f}"
                     f"{s['avg_charges']:>6.0f}{(s['profit_factor'] or 0):>6.2f}{s['total_net']:>10.0f}{s['max_drawdown']:>10.0f}"
                     f"{s['worst_day']['net']:>9.0f}{s['avg_margin']:>9.0f}{(s['return_on_margin_pa_pct'] or 0):>8.1f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    days, sessions, cut, meta = load()
    res = run(days, sessions, cut)
    res.update(meta)
    # sensitivities on the middle scenarios only: intraday stop triggers, and double slippage
    for name, fills in (("intrabar_trigger", Fills(trigger="intrabar")), ("double_slippage", Fills(1.0, 4.0)),
                        ("low_slippage", Fills(0.1, 1.0))):     # low_slippage added after the first run, as a sensitivity only
        res[name] = run(days, sessions, cut, [Scenario(0.0, c) for c in ("trading", "mixed", "calendar")], GRID, fills)["results"]
    print(table(res))
    if argv:
        with open(argv[0], "w") as fh:
            json.dump(res, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
