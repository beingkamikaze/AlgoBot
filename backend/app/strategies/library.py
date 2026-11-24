"""The starter strategies (guide section 3.4). Each states, at the bar it fires, the
exact rule and every filter with its numbers: that becomes the explanation card.
They are hypotheses to test, not edges: the backtester decides."""

from __future__ import annotations

import numpy as np

from app.domain.signal import Condition
from app.strategies.base import Candidate, Frame, Strategy


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


class OpeningRangeBreakout(Strategy):
    id = "orb"
    version = "1"
    name = "Opening-range breakout"
    description = ("Trade the first 5-minute close outside the 9:15–9:30 range, in the direction of the break, "
                   "with the stop at the range midpoint.")
    defaults = {"or_minutes": 15, "target_r": 2.0, "min_range_pct": 0.3, "max_range_pct": 2.5,
                "min_volume_x": 1.0, "require_vwap": True, "allow_short": True, "last_entry_minute": 180}
    param_help = {
        "or_minutes": "length of the opening range in minutes",
        "target_r": "target as a multiple of the risk",
        "min_range_pct": "skip ranges narrower than this % of price (too tight to survive noise)",
        "max_range_pct": "skip ranges wider than this % (the stop would be too far)",
        "min_volume_x": "breakout bar volume vs the average of the bars before it",
        "require_vwap": "longs only above VWAP, shorts only below",
        "allow_short": "also take downside breaks (intraday MIS shorting)",
        "last_entry_minute": "no new breakouts this many minutes after 9:15 (180 = 12:15)",
    }

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        m = int(f.minute[i])
        if m < p["or_minutes"] or m > p["last_entry_minute"]:
            return None
        hi, lo = f.ind("or", p["or_minutes"])
        if np.isnan(hi[i]):
            return None
        s0 = int(f.session_start[i])
        # only the FIRST close outside the range counts
        prior = range(s0, i)
        broke_up_before = any(f.c[k] > hi[k] for k in prior if not np.isnan(hi[k]))
        broke_dn_before = any(f.c[k] < lo[k] for k in prior if not np.isnan(lo[k]))
        up, dn = f.c[i] > hi[i] and not broke_up_before, f.c[i] < lo[i] and not broke_dn_before
        if not (up or (dn and p["allow_short"])):
            return None
        side = "buy" if up else "sell"
        mid = (hi[i] + lo[i]) / 2
        range_pct = (hi[i] - lo[i]) / mid * 100
        vol_avg = np.nanmean(f.v[s0:i]) if i > s0 else np.nan
        vol_x = f.v[i] / vol_avg if vol_avg and not np.isnan(vol_avg) and vol_avg > 0 else np.nan
        vwap = f.ind("vwap")[i]
        has_volume = f.v[s0:i + 1].sum() > 0
        conds = [
            Condition(name="Range width", met=p["min_range_pct"] <= range_pct <= p["max_range_pct"],
                      detail=f"{range_pct:.2f}% (allowed {p['min_range_pct']}–{p['max_range_pct']}%)"),
        ]
        if has_volume:
            conds.append(Condition(name="Breakout volume", met=bool(vol_x >= p["min_volume_x"]),
                                   detail=f"{vol_x:.1f}× the session's average bar (need {p['min_volume_x']}×)"))
            if p["require_vwap"]:
                ok = f.c[i] > vwap if side == "buy" else f.c[i] < vwap
                conds.append(Condition(name="VWAP side", met=bool(ok),
                                       detail=f"close {self.fmt(f.c[i])} {'above' if f.c[i] > vwap else 'below'} VWAP {self.fmt(vwap)}"))
        if not all(c.met for c in conds):
            return None
        gap = _pct(f.o[s0], f.prev_close[i]) if not np.isnan(f.prev_close[i]) else 0.0
        level = hi[i] if up else lo[i]
        t = f.local[i]
        close_time = f"{t.hour}:{t.minute + f.bar_minutes:02d}" if t.minute + f.bar_minutes < 60 else f"{t.hour + 1}:{(t.minute + f.bar_minutes) % 60:02d}"
        return Candidate(
            side=side, signal_index=i, reference=float(f.c[i]), stop=float(mid), target_r=p["target_r"],
            rule=(f"ORB {'long' if up else 'short'}: the {t:%H:%M} {f.bar_minutes}-minute bar closed at "
                  f"{self.fmt(f.c[i])}, {'above' if up else 'below'} the 9:15–{9 + (15 + p['or_minutes']) // 60}:"
                  f"{(15 + p['or_minutes']) % 60:02d} range {'high' if up else 'low'} of {self.fmt(level)} "
                  f"({_pct(f.c[i], level):+.2f}%) at {close_time}. First break of the day."),
            conditions=conds,
            invalidation=f"A close back {'below' if up else 'above'} {self.fmt(level)} means the breakout failed.",
            stop_reason=f"opening-range midpoint ({self.fmt(hi[i])} + {self.fmt(lo[i])}) / 2",
            features={"range_pct": round(range_pct, 2), "vol_x": round(float(vol_x), 2) if not np.isnan(vol_x) else None,
                      "gap_pct": round(gap, 2), "minute": m},
        )


class VwapPullback(Strategy):
    id = "vwap_pullback"
    version = "1"
    name = "VWAP pullback in a trend"
    description = ("In an intraday uptrend (EMA 20 above EMA 50, price above VWAP), buy when a pullback touches "
                   "VWAP and the bar closes back above it; the mirror image for shorts.")
    defaults = {"fast": 20, "slow": 50, "target_r": 1.5, "stop_atr": 1.0, "atr_n": 14, "allow_short": True,
                "first_entry_minute": 30, "last_entry_minute": 300}
    param_help = {"fast": "fast EMA", "slow": "slow EMA", "target_r": "target as a multiple of the risk",
                  "stop_atr": "stop this many ATRs beyond the pullback bar", "atr_n": "ATR period",
                  "allow_short": "also take short pullbacks", "first_entry_minute": "no entries before 9:15 + this",
                  "last_entry_minute": "no entries after 9:15 + this (300 = 14:15)"}

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        m = int(f.minute[i])
        if m < p["first_entry_minute"] or m > p["last_entry_minute"] or i < 1:
            return None
        s0 = int(f.session_start[i])
        if f.v[s0:i + 1].sum() == 0:
            return None                       # no volume, no VWAP
        fast, slow = f.ind("ema", p["fast"]), f.ind("ema", p["slow"])
        vwap, a = f.ind("vwap"), f.ind("atr", p["atr_n"])
        if np.isnan(slow[i]) or np.isnan(a[i]):
            return None
        up = fast[i] > slow[i] and f.l[i] <= vwap[i] < f.c[i] and f.c[i - 1] > vwap[i - 1]
        dn = fast[i] < slow[i] and f.h[i] >= vwap[i] > f.c[i] and f.c[i - 1] < vwap[i - 1]
        if not (up or (dn and p["allow_short"])):
            return None
        side = "buy" if up else "sell"
        stop = f.l[i] - p["stop_atr"] * a[i] if up else f.h[i] + p["stop_atr"] * a[i]
        conds = [
            Condition(name="Trend", met=True, detail=f"EMA {p['fast']} {self.fmt(fast[i])} {'>' if up else '<'} EMA {p['slow']} {self.fmt(slow[i])}"),
            Condition(name="Pullback to VWAP", met=True,
                      detail=f"{'low' if up else 'high'} {self.fmt(f.l[i] if up else f.h[i])} touched VWAP {self.fmt(vwap[i])}"),
            Condition(name="Rejection", met=True, detail=f"closed {'back above' if up else 'back below'} VWAP at {self.fmt(f.c[i])}"),
        ]
        return Candidate(
            side=side, signal_index=i, reference=float(f.c[i]), stop=float(stop), target_r=p["target_r"],
            rule=(f"VWAP pullback {'long' if up else 'short'}: in a {'rising' if up else 'falling'} intraday trend the "
                  f"{f.local[i]:%H:%M} bar dipped to VWAP {self.fmt(vwap[i])} and closed {'above' if up else 'below'} it at {self.fmt(f.c[i])}."),
            conditions=conds,
            invalidation=f"A close {'below' if up else 'above'} VWAP ({self.fmt(vwap[i])}) breaks the pullback idea.",
            stop_reason=f"{p['stop_atr']}× ATR {'below the bar low' if up else 'above the bar high'}",
            features={"minute": m, "trend_gap_pct": round(_pct(fast[i], slow[i]), 2)},
        )


class SupertrendFlip(Strategy):
    id = "supertrend"
    version = "1"
    name = "Supertrend flip with trend filter"
    description = ("Enter when Supertrend (ATR 10 × 3) flips, only in the direction of the 50-bar EMA; "
                   "stop at the Supertrend line.")
    timeframe = "15m"
    defaults = {"n": 10, "mult": 3.0, "trend_ema": 50, "target_r": 2.0, "allow_short": True,
                "last_entry_minute": 315}
    param_help = {"n": "ATR period", "mult": "ATR multiple", "trend_ema": "only trade in this EMA's direction",
                  "target_r": "target as a multiple of the risk", "allow_short": "also take short flips",
                  "last_entry_minute": "no entries after 9:15 + this (315 = 14:30)"}

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        if i < 1 or f.minute[i] > p["last_entry_minute"]:
            return None
        line, d = f.ind("supertrend", p["n"], p["mult"])
        ema = f.ind("ema", p["trend_ema"])
        if np.isnan(d[i]) or np.isnan(d[i - 1]) or np.isnan(ema[i]) or d[i] == d[i - 1]:
            return None
        up = d[i] > 0 and f.c[i] > ema[i]
        dn = d[i] < 0 and f.c[i] < ema[i]
        if not (up or (dn and p["allow_short"])):
            return None
        conds = [
            Condition(name="Supertrend flip", met=True, detail=f"turned {'up' if up else 'down'}, line {self.fmt(line[i])}"),
            Condition(name="Trend filter", met=True, detail=f"close {self.fmt(f.c[i])} {'above' if up else 'below'} EMA {p['trend_ema']} {self.fmt(ema[i])}"),
        ]
        return Candidate(
            side="buy" if up else "sell", signal_index=i, reference=float(f.c[i]), stop=float(line[i]),
            target_r=p["target_r"],
            rule=(f"Supertrend {'long' if up else 'short'}: on the {f.local[i]:%H:%M} {f.tf} bar Supertrend({p['n']}, "
                  f"{p['mult']}) flipped {'up' if up else 'down'} with price {'above' if up else 'below'} its {p['trend_ema']}-bar EMA."),
            conditions=conds,
            invalidation=f"Supertrend flipping back ({'below' if up else 'above'} {self.fmt(line[i])}) ends the trend idea.",
            stop_reason="the Supertrend line",
            features={"minute": int(f.minute[i])},
        )


class PrevDayBreakout(Strategy):
    id = "pdh_pdl"
    version = "1"
    name = "Previous-day high/low breakout (CPR filter)"
    description = ("Trade the first 5-minute close beyond yesterday's high (long) or low (short), only on days when "
                   "the central pivot range (CPR) is narrow, a classic sign of a trending day; stop one ATR back "
                   "inside the broken level.")
    defaults = {"max_cpr_pct": 0.25, "stop_atr": 1.0, "atr_n": 14, "target_r": 2.0, "allow_short": True,
                "first_entry_minute": 15, "last_entry_minute": 240}
    param_help = {"max_cpr_pct": "only trade when the CPR width is at most this % of price (narrow = trend day)",
                  "stop_atr": "stop this many ATRs back inside the broken level", "atr_n": "ATR period",
                  "target_r": "target as a multiple of the risk", "allow_short": "also take breaks below yesterday's low",
                  "first_entry_minute": "no entries before 9:15 + this", "last_entry_minute": "no entries after 9:15 + this (240 = 13:15)"}

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        m = int(f.minute[i])
        if m < p["first_entry_minute"] or m > p["last_entry_minute"]:
            return None
        ph, pl, pc = f.ind("pdl")
        a = f.ind("atr", p["atr_n"])
        if np.isnan(ph[i]) or np.isnan(a[i]):
            return None
        s0 = int(f.session_start[i])
        up = f.c[i] > ph[i] and not any(f.c[k] > ph[k] for k in range(s0, i))
        dn = f.c[i] < pl[i] and not any(f.c[k] < pl[k] for k in range(s0, i))
        if not (up or (dn and p["allow_short"])):
            return None
        pivot = (ph[i] + pl[i] + pc[i]) / 3
        bc = (ph[i] + pl[i]) / 2
        tc = 2 * pivot - bc
        cpr_pct = abs(tc - bc) / pivot * 100
        conds = [Condition(name="Narrow CPR", met=cpr_pct <= p["max_cpr_pct"],
                           detail=f"CPR width {cpr_pct:.2f}% of price (need ≤ {p['max_cpr_pct']}%)")]
        if not conds[0].met:
            return None
        level = ph[i] if up else pl[i]
        stop = level - p["stop_atr"] * a[i] if up else level + p["stop_atr"] * a[i]
        if (up and stop >= f.c[i]) or (dn and stop <= f.c[i]):
            return None
        conds.append(Condition(name="First break", met=True,
                               detail=f"first close {'above' if up else 'below'} yesterday's {'high' if up else 'low'} today"))
        return Candidate(
            side="buy" if up else "sell", signal_index=i, reference=float(f.c[i]), stop=float(stop), target_r=p["target_r"],
            rule=(f"Previous-day {'high' if up else 'low'} breakout: the {f.local[i]:%H:%M} bar closed at {self.fmt(f.c[i])}, "
                  f"{'above' if up else 'below'} yesterday's {'high' if up else 'low'} of {self.fmt(level)} ({_pct(f.c[i], level):+.2f}%)."),
            conditions=conds,
            invalidation=f"A close back {'below' if up else 'above'} {self.fmt(level)} means the breakout failed.",
            stop_reason=f"{p['stop_atr']}× ATR ({self.fmt(a[i])}) back inside yesterday's {'high' if up else 'low'}",
            features={"minute": m, "cpr_pct": round(cpr_pct, 3)},
        )


class OpeningRangeFade(Strategy):
    id = "orb_fade"
    version = "1"
    name = "Opening-range fade (failed breakout)"
    description = ("After price breaks outside the 9:15–9:30 range and a 5-minute bar then closes back inside it, "
                   "trade back toward the other side of the range; stop just beyond the failed break's extreme.")
    defaults = {"or_minutes": 15, "break_by_close": True, "stop_buffer_atr": 0.0, "atr_n": 14, "target_frac": 0.5,
                "min_target_r": 0.75, "min_range_pct": 0.3, "max_range_pct": 2.5, "allow_long": True,
                "allow_short": True, "last_entry_minute": 240}
    param_help = {
        "or_minutes": "length of the opening range in minutes",
        "break_by_close": "a break needs a bar CLOSE outside the range (false: any trade outside it counts)",
        "stop_buffer_atr": "stop this many ATRs beyond the failed break's extreme (0 = at the extreme)",
        "atr_n": "ATR period for the stop buffer",
        "target_frac": "target this fraction of the range back across it (0.5 = midpoint, 1 = the opposite edge)",
        "min_target_r": "skip when the target is less than this many R away (the stop is too far for the reward)",
        "min_range_pct": "skip ranges narrower than this % of price",
        "max_range_pct": "skip ranges wider than this % of price",
        "allow_long": "fade failed downside breaks (buy)",
        "allow_short": "fade failed upside breaks (sell, intraday MIS)",
        "last_entry_minute": "no new entries this many minutes after 9:15 (240 = 13:15)",
    }

    @staticmethod
    def _failed_break(f: Frame, start: int, i: int, hi, lo, upside: bool, by_close: bool) -> tuple[int, int] | None:
        """(index of the first close back inside after the session's first break on this side, the break's index)."""
        broke = None
        for k in range(start, i + 1):
            if broke is None:
                if upside:
                    out = f.c[k] > hi[k] if by_close else f.h[k] > hi[k]
                else:
                    out = f.c[k] < lo[k] if by_close else f.l[k] < lo[k]
                if not out:
                    continue
                broke = k
            if lo[k] < f.c[k] < hi[k]:
                return k, broke
        return None

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        m = int(f.minute[i])
        if m < p["or_minutes"] or m > p["last_entry_minute"]:
            return None
        hi, lo = f.ind("or", p["or_minutes"])
        if np.isnan(hi[i]):
            return None
        mid = (hi[i] + lo[i]) / 2
        range_pct = (hi[i] - lo[i]) / mid * 100
        if not p["min_range_pct"] <= range_pct <= p["max_range_pct"]:
            return None
        s0 = int(f.session_start[i])
        start = next(k for k in range(s0, i + 1) if not np.isnan(hi[k]))
        for upside, allowed in ((True, p["allow_short"]), (False, p["allow_long"])):
            if not allowed:
                continue
            hit = self._failed_break(f, start, i, hi, lo, upside, p["break_by_close"])
            if hit is None or hit[0] != i:
                continue
            broke = hit[1]
            a = f.ind("atr", p["atr_n"])[i]
            buffer = p["stop_buffer_atr"] * a if p["stop_buffer_atr"] and not np.isnan(a) else 0.0
            if upside:
                extreme = float(f.h[broke:i + 1].max())
                stop, target = extreme + buffer, hi[i] - p["target_frac"] * (hi[i] - lo[i])
            else:
                extreme = float(f.l[broke:i + 1].min())
                stop, target = extreme - buffer, lo[i] + p["target_frac"] * (hi[i] - lo[i])
            risk, reward = abs(stop - f.c[i]), abs(f.c[i] - target)
            if risk <= 0 or (upside and target >= f.c[i]) or (not upside and target <= f.c[i]):
                continue
            target_r = reward / risk
            if target_r < p["min_target_r"]:
                continue
            side = "sell" if upside else "buy"
            edge = hi[i] if upside else lo[i]
            t0 = f.local[broke]
            conds = [
                Condition(name="Range width", met=True,
                          detail=f"{range_pct:.2f}% (allowed {p['min_range_pct']}–{p['max_range_pct']}%)"),
                Condition(name="Break", met=True,
                          detail=f"{'closed' if p['break_by_close'] else 'traded'} {'above' if upside else 'below'} the range "
                                 f"{'high' if upside else 'low'} {self.fmt(edge)} at {t0:%H:%M}, extreme {self.fmt(extreme)}"),
                Condition(name="Failure", met=True,
                          detail=f"the {f.local[i]:%H:%M} bar closed back inside at {self.fmt(f.c[i])}"),
                Condition(name="Reward", met=True, detail=f"target {self.fmt(target)} is {target_r:.2f}R away "
                                                          f"(need {p['min_target_r']}R)"),
            ]
            return Candidate(
                side=side, signal_index=i, reference=float(f.c[i]), stop=float(stop), target_r=round(target_r, 3),
                rule=(f"Opening-range fade {'short' if upside else 'long'}: price broke {'above' if upside else 'below'} the "
                      f"opening range {'high' if upside else 'low'} of {self.fmt(edge)} at {t0:%H:%M} (extreme "
                      f"{self.fmt(extreme)}), then the {f.local[i]:%H:%M} bar closed back inside at {self.fmt(f.c[i])}: "
                      f"a failed breakout, traded back toward {self.fmt(target)}."),
                conditions=conds,
                invalidation=f"A move back {'above' if upside else 'below'} {self.fmt(extreme)} means the breakout was real after all.",
                stop_reason=(f"{'above' if upside else 'below'} the failed break's extreme {self.fmt(extreme)}"
                             + (f" plus {p['stop_buffer_atr']}× ATR" if buffer else "")),
                features={"range_pct": round(range_pct, 2), "minute": m, "bars_outside": i - broke,
                          "target_r": round(target_r, 2)},
            )
        return None


class IntradayMomentum(Strategy):
    """Market intraday momentum (Gao, Han, Li & Zhou, Journal of Financial Economics 2018): the market's first
    half-hour return, from yesterday's close, predicts the direction of the last half-hour. Baltussen, Da,
    Lammers & Martens (JFE 2021) find the same with the return from yesterday's close up to 30 minutes before
    the close. Meant for index futures. The papers hold to the close; the app must be flat by 15:15, so the
    trade runs from `entry_minute` to the flat-by time. The papers use no stop or target: here the stop is a
    wide disaster stop and the target a far cap, so the time exit normally ends the trade."""

    id = "intraday_momentum"
    version = "1"
    name = "Intraday momentum (first half-hour)"
    description = ("At 14:45, trade in the direction of the session's first half-hour (yesterday's close to 9:45) "
                   "and get out at 15:15. Based on a published index-futures effect; meant for Nifty and Bank Nifty futures.")
    auto_scan = False                       # lost at every in-sample setting (2026-11-11): kept for research only
    defaults = {"signal": "first", "first_minutes": 30, "min_move_pct": 0.0, "entry_minute": 330,
                "stop_atr": 3.0, "atr_n": 14, "target_r": 5.0, "allow_short": True}
    param_help = {
        "signal": "'first': yesterday's close to 9:15 + first_minutes; 'day': yesterday's close to the signal bar's close",
        "first_minutes": "length of the first period in minutes (30 = up to 9:45)",
        "min_move_pct": "trade only when that return is at least this % either way (0 = trade every day on its sign)",
        "entry_minute": "enter this many minutes after 9:15 (330 = 14:45); the signal is the bar that closes then",
        "stop_atr": "disaster stop this many ATRs from the signal close (the papers use none)",
        "atr_n": "ATR period",
        "target_r": "far target cap as a multiple of the risk; normally the 15:15 time exit ends the trade",
        "allow_short": "also sell after a falling first half-hour",
    }

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        m = int(f.minute[i])
        if m + f.bar_minutes != p["entry_minute"] or np.isnan(f.prev_close[i]):
            return None
        s0 = int(f.session_start[i])
        pc = float(f.prev_close[i])
        if p["signal"] == "day":
            end, label = i, f"{f.local[i]:%H:%M} bar's close"
        else:
            last = p["first_minutes"] - f.bar_minutes        # the bar that closes at 9:15 + first_minutes
            hits = [k for k in range(s0, i) if int(f.minute[k]) == last]
            if not hits:
                return None                                  # that bar is missing: no reading, no trade
            end = hits[0]
            close_m = 15 + p["first_minutes"]
            label = f"{9 + close_m // 60}:{close_m % 60:02d}"
        move = _pct(f.c[end], pc)
        if abs(move) < max(p["min_move_pct"], 1e-9):
            return None
        up = move > 0
        if not up and not p["allow_short"]:
            return None
        a = f.ind("atr", p["atr_n"])[i]
        if np.isnan(a) or a <= 0:
            return None
        stop = f.c[i] - p["stop_atr"] * a if up else f.c[i] + p["stop_atr"] * a
        side = "buy" if up else "sell"
        since = "the first half-hour" if p["signal"] == "first" and p["first_minutes"] == 30 else \
            ("the session so far" if p["signal"] == "day" else f"the first {p['first_minutes']} minutes")
        conds = [
            Condition(name="Early move", met=True,
                      detail=f"yesterday's close {self.fmt(pc)} to the {label} {self.fmt(f.c[end])}: {move:+.2f}%"
                             + (f" (need ±{p['min_move_pct']}%)" if p["min_move_pct"] else "")),
            Condition(name="Entry time", met=True, detail=f"the {f.local[i]:%H:%M} bar closed; enter now, out by 15:15"),
        ]
        return Candidate(
            side=side, signal_index=i, reference=float(f.c[i]), stop=float(stop), target_r=p["target_r"],
            rule=(f"Intraday momentum {'long' if up else 'short'}: {since} moved {move:+.2f}% (yesterday's close "
                  f"{self.fmt(pc)} to {self.fmt(f.c[end])}). Published research finds the late session tends to follow "
                  f"the early move, so {'buy' if up else 'sell'} now and get out at 15:15."),
            conditions=conds,
            invalidation="This is a timed trade: it ends at 15:15 whatever happens; the stop is only for a sudden shock.",
            stop_reason=f"{p['stop_atr']}× ATR ({self.fmt(a)}) from the signal close, a disaster stop",
            features={"minute": m, "early_move_pct": round(move, 3)},
        )


STRATEGIES: dict[str, type[Strategy]] = {s.id: s for s in (OpeningRangeBreakout, VwapPullback, SupertrendFlip, PrevDayBreakout,
                                                                  OpeningRangeFade, IntradayMomentum)}


def make(strategy_id: str, params: dict | None = None) -> Strategy:
    if strategy_id not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy_id!r}; known: {', '.join(STRATEGIES)}")
    return STRATEGIES[strategy_id](**(params or {}))


def describe() -> list[dict]:
    return [{"id": s.id, "version": s.version, "name": s.name, "description": s.description, "market": s.market,
             "timeframe": s.timeframe, "defaults": s.defaults, "param_help": s.param_help, "auto_scan": s.auto_scan}
            for s in STRATEGIES.values()]
