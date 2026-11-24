"""MODELED option backtests (guide strategies 6 and 7).

Free historical option prices do not exist, so these backtests PRICE every option
with Black-76 from the index's own 5-minute bars, using the previous day's India
VIX as the volatility (x1.2 for Bank Nifty). That ignores the volatility smile,
intraday swings in implied volatility (the "IV crush" after a move, the spikes in
a sell-off) and real spreads, so the results are INDICATIVE ONLY. Time is measured
in trading days (252 a year, decay only while the market is open). Every report
says "modeled". Costs are not ignored: a half-spread is paid on every fill and
Zerodha's option charges come off every trade.

Expiries are rebuilt from NSE's rules: Nifty weekly (Tuesday), Bank Nifty monthly
(last Tuesday); a holiday moves the expiry to the trading day before.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from app.backtest.engine import Trade
from app.core.calendar import CLOSE, IST, is_trading_day
from app.core.costs import options_charges
from app.core.instruments import INDEX_FUTURES, monthly_expiry
from app.options import pricing
from app.strategies.base import Frame
from app.strategies.library import OpeningRangeBreakout

STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
VOL_MULT = {"NIFTY": 1.0, "BANKNIFTY": 1.2}


def weekly_expiry_on_or_after(d: dt.date) -> dt.date:
    """The Nifty weekly expiry for the week of `d` (Tuesday, or the trading day before it if a holiday)."""
    tue = d + dt.timedelta(days=(1 - d.weekday()) % 7)
    e = tue
    while not is_trading_day(e)[0]:
        e -= dt.timedelta(days=1)
    if e < d:                                   # the holiday-shifted expiry already passed: next week's
        return weekly_expiry_on_or_after(tue + dt.timedelta(days=1))
    return e


def expiry_for(underlying: str, d: dt.date, min_days: int) -> dt.date:
    if underlying == "NIFTY":
        e = weekly_expiry_on_or_after(d)
        while (e - d).days < min_days:
            e = weekly_expiry_on_or_after(e + dt.timedelta(days=1))
        return e
    e = monthly_expiry(d.year, d.month)
    if e < d or (e - d).days < min_days:
        y, m = (d.year + (d.month == 12), d.month % 12 + 1)
        e = monthly_expiry(y, m)
    return e


@dataclass
class Cfg:
    lots: int = 1
    half_spread_pct: float = 0.5        # paid on every fill, % of the option price
    flat_by_minute: int = 360           # 15:15


SESSION_SECONDS = 375 * 60         # 9:15 to 15:30


def _t(ts: int, expiry: dt.date) -> float:
    """Time to expiry in TRADING years: each session counts as one day of 252, and time only passes while the
    market is open. Calendar time would put most of each day's decay overnight, while prices move in the day."""
    now = dt.datetime.fromtimestamp(ts, IST)
    day = now.date()
    if day > expiry:
        return 0.0
    close_today = dt.datetime.combine(day, CLOSE, IST).timestamp()
    left_today = min(max(close_today - ts, 0.0), SESSION_SECONDS) / SESSION_SECONDS if is_trading_day(day)[0] else 0.0
    days, d = 0, day
    while d < expiry:
        d += dt.timedelta(days=1)
        days += is_trading_day(d)[0]
    return (left_today + days) / 252.0


def _price(kind, spot, strike, ts, expiry, vol) -> float:
    return pricing.black76(kind, spot, strike, _t(ts, expiry), vol).price


def _fill(p: float, buying: bool, cfg: Cfg) -> float:
    return p * (1 + cfg.half_spread_pct / 100) if buying else p * (1 - cfg.half_spread_pct / 100)


class OptionStrategy:
    id: ClassVar[str]
    version: ClassVar[str] = "1"
    name: ClassVar[str]
    description: ClassVar[str]
    timeframe: ClassVar[str] = "5m"
    market: ClassVar[str] = "index_options"
    defaults: ClassVar[dict[str, Any]] = {}
    param_help: ClassVar[dict[str, str]] = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"unknown parameter(s) for {self.id}: {', '.join(sorted(unknown))}")
        self.params = {**self.defaults, **params}

    @property
    def version_key(self) -> str:
        changed = {k: v for k, v in self.params.items() if self.defaults.get(k) != v}
        return self.version + ("" if not changed else "+" + ",".join(f"{k}={v}" for k, v in sorted(changed.items())))

    def run(self, f: Frame, underlying: str, vix_prev: dict[str, float], cfg: Cfg, regime_for) -> list[Trade]:
        raise NotImplementedError


class OrbOptionBuy(OptionStrategy):
    id = "orb_option_buy"
    name = "ORB option buy (modeled)"
    description = ("When the opening-range breakout fires on the index, buy the ATM call (up) or put (down) of the "
                   "nearest expiry at least `min_days` away; exit when the index hits the ORB stop or target, or at 15:15.")
    defaults = {"min_days": 1, "strike_offset": 0, "target_r": 2.0}
    param_help = {"min_days": "skip expiries closer than this many days (1 = never buy on expiry day)",
                  "strike_offset": "0 = ATM, 1 = one strike in the money", "target_r": "index target as a multiple of the ORB risk"}

    def run(self, f, underlying, vix_prev, cfg, regime_for):
        orb = OpeningRangeBreakout(target_r=self.params["target_r"])
        lot = INDEX_FUTURES[underlying]["lot_size"]
        step = STRIKE_STEP[underlying]
        trades, i, n = [], 0, len(f)
        taken: set[str] = set()
        while i < n - 1:
            sess = str(f.session[i])
            if sess in taken or sess not in vix_prev:
                i += 1
                continue
            cand = orb.check(f, i)
            if cand is None or f.session[i + 1] != f.session[i]:
                i += 1
                continue
            taken.add(sess)
            vol = vix_prev[sess] / 100 * VOL_MULT[underlying]
            day = dt.date.fromisoformat(sess)
            exp = expiry_for(underlying, day, self.params["min_days"])
            long = cand.side == "buy"
            kind = "CE" if long else "PE"
            atm = round(f.o[i + 1] / step) * step
            strike = atm - self.params["strike_offset"] * step if long else atm + self.params["strike_offset"] * step
            j = i + 1
            entry = _fill(_price(kind, f.o[j], strike, int(f.t[j]), exp, vol), True, cfg)
            risk_idx = abs(cand.reference - cand.stop)
            idx_target = cand.reference + cand.target_r * risk_idx if long else cand.reference - cand.target_r * risk_idx
            exit_px, reason, k = None, None, j
            for k in range(j, n):
                if f.session[k] != f.session[j]:
                    k -= 1
                    break
                hit_stop = f.l[k] <= cand.stop if long else f.h[k] >= cand.stop
                hit_tgt = f.h[k] > idx_target if long else f.l[k] < idx_target
                bar_end = int(f.t[k]) + f.bar_minutes * 60
                if hit_stop:
                    exit_px, reason = _price(kind, cand.stop, strike, bar_end, exp, vol), "stop"
                elif hit_tgt:
                    exit_px, reason = _price(kind, idx_target, strike, bar_end, exp, vol), "target"
                elif f.minute[k] + f.bar_minutes >= cfg.flat_by_minute or k == n - 1 or f.session[k + 1] != f.session[k]:
                    exit_px, reason = _price(kind, f.c[k], strike, bar_end, exp, vol), "time"
                if reason:
                    break
            exit_px = _fill(exit_px, False, cfg)
            qty = cfg.lots * lot
            gross = (exit_px - entry) * qty
            costs = options_charges(entry * qty, exit_px * qty).total
            net = gross - costs
            risk = entry * qty                                   # the most a bought option can lose
            trades.append(Trade(symbol=f"{underlying}-OPT", side="buy", session=sess, signal_t=int(f.t[i]),
                                entry_t=int(f.t[j]), exit_t=int(f.t[k]), entry=round(entry, 2), stop=round(cand.stop, 2),
                                target=round(idx_target, 2), exit=round(exit_px, 2), exit_reason=reason, qty=qty,
                                risk_per_share=round(entry, 2), gross=round(gross, 2), costs=costs, net=round(net, 2),
                                r=round(net / risk, 4), rule=f"{cand.rule} Bought {strike:g} {kind} expiring {exp} (modeled).",
                                features={"strike": strike, "kind": kind, "expiry": exp.isoformat(), "days_to_expiry": (exp - day).days},
                                regime=regime_for(sess) if regime_for else {}))
            i = k + 1
        return trades


class IronFlyIntraday(OptionStrategy):
    id = "iron_fly_intraday"
    name = "Intraday iron fly (modeled)"
    description = ("At 9:20 sell the ATM call and put of the nearest expiry and buy wings `width` strikes away; exit "
                   "when the loss reaches `stop_mult` x the credit, when `target_pct` of the credit is captured, or at 15:15.")
    defaults = {"entry_minute": 5, "width": 4, "stop_mult": 1.0, "target_pct": 0.5, "min_days": 0, "max_days": 1,
                "max_vix": 25.0}
    param_help = {"entry_minute": "enter on the bar that closes this many minutes after 9:15", "width": "wings this many strikes out",
                  "stop_mult": "stop when the loss is this multiple of the credit", "target_pct": "take profit at this share of the credit",
                  "min_days": "0 = trade expiry day too", "max_days": "skip days further than this from the expiry",
                  "max_vix": "skip days when yesterday's VIX was above this"}

    def run(self, f, underlying, vix_prev, cfg, regime_for):
        p = self.params
        lot, step = INDEX_FUTURES[underlying]["lot_size"], STRIKE_STEP[underlying]
        qty = cfg.lots * lot
        trades = []
        for sess in sorted(set(f.session.tolist())):
            if sess not in vix_prev or vix_prev[sess] > p["max_vix"]:
                continue
            idx = np.where(f.session == sess)[0]
            sig = [k for k in idx if f.minute[k] + f.bar_minutes == p["entry_minute"]]   # the bar that closes at 9:15 + entry_minute
            if not sig:
                continue
            j = sig[0] + 1
            if j >= len(f) or f.session[j] != sess:
                continue
            vol = vix_prev[sess] / 100 * VOL_MULT[underlying]
            day = dt.date.fromisoformat(sess)
            exp = expiry_for(underlying, day, p["min_days"])
            if (exp - day).days > p["max_days"]:
                continue
            atm = round(f.o[j] / step) * step
            w = p["width"] * step
            legs = [("CE", atm, -1), ("PE", atm, -1), ("CE", atm + w, 1), ("PE", atm - w, 1)]

            def value(spot, ts):     # per unit, what the four legs are worth (long +, short -)
                return sum(sgn * _price(kind, spot, k, ts, exp, vol) for kind, k, sgn in legs)

            entry_prices = {(kind, k): _fill(_price(kind, f.o[j], k, int(f.t[j]), exp, vol), sgn > 0, cfg) for kind, k, sgn in legs}
            credit = sum(-sgn * entry_prices[(kind, k)] for kind, k, sgn in legs)
            if credit <= 0:
                continue
            max_loss = (w - credit) * qty
            stop_amt = min(credit * qty * p["stop_mult"], max_loss)
            target_amt = credit * qty * p["target_pct"]
            reason, k_exit, exit_vals = None, j, None
            for k in idx[idx >= j]:
                ts = int(f.t[k]) + f.bar_minutes * 60
                # the worse of the bar's high and low for a short fly: whichever is further from the strike
                worst_spot = f.h[k] if abs(f.h[k] - atm) > abs(f.l[k] - atm) else f.l[k]
                pnl_worst = (value(worst_spot, ts) + credit) * qty
                pnl_close = (value(f.c[k], ts) + credit) * qty
                if pnl_worst <= -stop_amt:
                    reason, k_exit, exit_spot = "stop", k, worst_spot
                elif pnl_close >= target_amt:
                    reason, k_exit, exit_spot = "target", k, f.c[k]
                elif f.minute[k] + f.bar_minutes >= cfg.flat_by_minute or k == idx[-1]:
                    reason, k_exit, exit_spot = "time", k, f.c[k]
                if reason:
                    exit_vals = {(kind, kk): _fill(_price(kind, exit_spot, kk, ts, exp, vol), sgn < 0, cfg) for kind, kk, sgn in legs}
                    break
            if exit_vals is None:
                continue
            pnl = sum(sgn * (exit_vals[(kind, k)] - entry_prices[(kind, k)]) for kind, k, sgn in legs) * qty
            buy_v = sum(entry_prices[(kd, kk)] for kd, kk, sg in legs if sg > 0) * qty + sum(exit_vals[(kd, kk)] for kd, kk, sg in legs if sg < 0) * qty
            sell_v = sum(entry_prices[(kd, kk)] for kd, kk, sg in legs if sg < 0) * qty + sum(exit_vals[(kd, kk)] for kd, kk, sg in legs if sg > 0) * qty
            costs = options_charges(buy_v, sell_v, 4, 4).total
            net = pnl - costs
            trades.append(Trade(symbol=f"{underlying}-OPT", side="sell", session=sess, signal_t=int(f.t[j - 1]),
                                entry_t=int(f.t[j]), exit_t=int(f.t[k_exit]), entry=round(credit, 2), stop=round(stop_amt, 2),
                                target=round(target_amt, 2), exit=round(-sum(sgn * exit_vals[(kd, kk)] for kd, kk, sgn in legs), 2),
                                exit_reason=reason, qty=qty, risk_per_share=round(stop_amt / qty, 4), gross=round(pnl, 2),
                                costs=costs, net=round(net, 2), r=round(net / stop_amt, 4) if stop_amt else 0.0,
                                rule=f"Iron fly at {atm:g} with {w:g}-point wings, {exp} expiry, credit {credit:.2f} (modeled).",
                                features={"atm": atm, "expiry": exp.isoformat(), "days_to_expiry": (exp - day).days,
                                          "vix_prev": vix_prev[sess]},
                                regime=regime_for(sess) if regime_for else {}))
        return trades


OPTION_STRATEGIES: dict[str, type[OptionStrategy]] = {s.id: s for s in (OrbOptionBuy, IronFlyIntraday)}


def describe() -> list[dict]:
    return [{"id": s.id, "version": s.version, "name": s.name, "description": s.description, "market": s.market,
             "timeframe": s.timeframe, "defaults": s.defaults, "param_help": s.param_help, "modeled": True}
            for s in OPTION_STRATEGIES.values()]
