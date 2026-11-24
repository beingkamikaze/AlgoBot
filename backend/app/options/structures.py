"""Option structures (guide section 3.4, strategies 6 and 7): build, price and measure.

A structure is a set of legs on one underlying and expiry. Entry prices are the
conservative side of the quote (buy at the ask, sell at the bid). For every
structure the code computes, never the model:
  - net premium (credit > 0 when you receive money)
  - max profit and max loss at expiry, and whether the loss is unlimited
  - breakevens, net Greeks, a payoff curve (at expiry and today)
  - a margin estimate and round-trip charges
Naked short options (unlimited loss) are built but marked; the risk engine blocks
them unless you have explicitly allowed them in your limits.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

from app.core.costs import options_charges
from app.options import pricing
from app.options.chain import Chain

NAKED_MARGIN_PCT = 15.0      # SPAN + exposure for a naked short index option, roughly, as % of notional
EXPOSURE_PCT = 2.0           # exposure margin on each short option, % of its notional, even when hedged

TEMPLATES = {
    "long_call": "Buy a call: bullish, loss limited to the premium",
    "long_put": "Buy a put: bearish, loss limited to the premium",
    "bull_call_spread": "Buy a call, sell a higher call: bullish, cheaper, capped both ways",
    "bear_put_spread": "Buy a put, sell a lower put: bearish, cheaper, capped both ways",
    "bull_put_spread": "Sell a put, buy a lower put: credit, profits if the market stays above the short put",
    "bear_call_spread": "Sell a call, buy a higher call: credit, profits if the market stays below the short call",
    "iron_condor": "Sell an OTM call and put, buy wings further out: credit, profits in a range, loss capped",
    "iron_fly": "Sell the ATM call and put, buy wings: bigger credit, narrower profit zone, loss capped",
    "short_straddle": "Sell the ATM call and put with NO wings: unlimited loss (naked)",
    "short_strangle": "Sell an OTM call and put with NO wings: unlimited loss (naked)",
}
DEFAULT_PARAMS = {"lots": 1, "offset": 0, "width": 4, "short_delta": 0.20}


@dataclass
class Leg:
    kind: str            # CE or PE
    strike: float
    side: str            # buy or sell
    lots: int
    price: float         # entry price per unit (ask for buys, bid for sells)
    tradingsymbol: str
    mid: float | None = None
    spread_pct: float | None = None
    oi: int | None = None
    iv: float | None = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1


@dataclass
class Structure:
    template: str
    underlying: str
    expiry: dt.date
    lot_size: int
    lots: int
    spot: float
    forward: float
    source: str                                    # chain source: "kite", "nse" or "modeled"
    as_of: str
    legs: list[Leg] = field(default_factory=list)
    net_premium: float = 0.0                       # per unit; > 0 credit received, < 0 debit paid
    max_profit: float | None = None                # rupees, whole position; None = unlimited
    max_loss: float | None = None                  # rupees, whole position (positive); None = unlimited
    breakevens: list[float] = field(default_factory=list)
    greeks: dict = field(default_factory=dict)
    margin: float = 0.0
    charges_round_trip: float = 0.0
    naked: bool = False
    margin_source: str = "estimate"                # "Kite" once app.paper.options.apply_exact used Kite's basket margin
    payoff: list[dict] = field(default_factory=list)
    label: str = ""

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        d["qty"] = self.qty
        d["description"] = TEMPLATES[self.template]
        return d


def _leg(c: Chain, kind: str, strike: float, side: str, lots: int) -> Leg:
    q = c.quote(strike, kind)
    if q is None:
        raise ValueError(f"{strike:g} {kind} is not in the {c.underlying} {c.expiry} chain")
    price = (q.ask if side == "buy" else q.bid) or q.ltp
    if not price:
        raise ValueError(f"no price for {strike:g} {kind}")
    return Leg(kind=kind, strike=strike, side=side, lots=lots, price=float(price), tradingsymbol=c.tradingsymbol(strike, kind),
               mid=q.mid, spread_pct=q.spread_pct, oi=q.oi, iv=q.iv)


def _step(c: Chain) -> float:
    ks = c.strikes()
    near = [b - a for a, b in zip(ks, ks[1:]) if abs(a - c.spot) < c.spot * 0.03]
    return min(near) if near else (ks[1] - ks[0])


def _by_delta(c: Chain, kind: str, target: float, now: dt.datetime | None) -> float:
    """The OTM strike whose |delta| is closest to `target`."""
    atm = c.atm()
    side = [k for k in c.strikes() if (k > atm if kind == "CE" else k < atm)]
    best, err = None, None
    for k in side:
        g = c.greeks(k, kind, now)
        if g is None:
            continue
        e = abs(abs(g.delta) - target)
        if err is None or e < err:
            best, err = k, e
    if best is None:
        raise ValueError(f"cannot find a {kind} near {target} delta")
    return best


def build(c: Chain, template: str, params: dict | None = None, now: dt.datetime | None = None) -> Structure:
    if template not in TEMPLATES:
        raise ValueError(f"unknown structure {template!r}; known: {', '.join(TEMPLATES)}")
    p = {**DEFAULT_PARAMS, **(params or {})}
    lots, step = int(p["lots"]), _step(c)
    if lots < 1:
        raise ValueError("lots must be 1 or more")
    atm = c.atm()
    width = step * int(p["width"])
    off = step * int(p["offset"])
    legs: list[Leg] = []
    if template == "long_call":
        legs = [_leg(c, "CE", atm + off, "buy", lots)]
    elif template == "long_put":
        legs = [_leg(c, "PE", atm - off, "buy", lots)]
    elif template == "bull_call_spread":
        legs = [_leg(c, "CE", atm + off, "buy", lots), _leg(c, "CE", atm + off + width, "sell", lots)]
    elif template == "bear_put_spread":
        legs = [_leg(c, "PE", atm - off, "buy", lots), _leg(c, "PE", atm - off - width, "sell", lots)]
    elif template == "bull_put_spread":
        k = _by_delta(c, "PE", p["short_delta"], now)
        legs = [_leg(c, "PE", k, "sell", lots), _leg(c, "PE", k - width, "buy", lots)]
    elif template == "bear_call_spread":
        k = _by_delta(c, "CE", p["short_delta"], now)
        legs = [_leg(c, "CE", k, "sell", lots), _leg(c, "CE", k + width, "buy", lots)]
    elif template == "iron_condor":
        kc, kp = _by_delta(c, "CE", p["short_delta"], now), _by_delta(c, "PE", p["short_delta"], now)
        legs = [_leg(c, "CE", kc, "sell", lots), _leg(c, "CE", kc + width, "buy", lots),
                _leg(c, "PE", kp, "sell", lots), _leg(c, "PE", kp - width, "buy", lots)]
    elif template == "iron_fly":
        legs = [_leg(c, "CE", atm, "sell", lots), _leg(c, "PE", atm, "sell", lots),
                _leg(c, "CE", atm + width, "buy", lots), _leg(c, "PE", atm - width, "buy", lots)]
    elif template == "short_straddle":
        legs = [_leg(c, "CE", atm, "sell", lots), _leg(c, "PE", atm, "sell", lots)]
    elif template == "short_strangle":
        legs = [_leg(c, "CE", _by_delta(c, "CE", p["short_delta"], now), "sell", lots),
                _leg(c, "PE", _by_delta(c, "PE", p["short_delta"], now), "sell", lots)]
    return measure(c, template, legs, now)


def _expiry_value(legs: list[Leg], x: float) -> float:
    """P&L per unit at expiry if the index settles at x."""
    total = 0.0
    for lg in legs:
        intrinsic = max(x - lg.strike, 0.0) if lg.kind == "CE" else max(lg.strike - x, 0.0)
        total += lg.sign * (intrinsic - lg.price) * lg.lots
    return total


def measure(c: Chain, template: str, legs: list[Leg], now: dt.datetime | None = None) -> Structure:
    lots = max(lg.lots for lg in legs)
    s = Structure(template=template, underlying=c.underlying, expiry=c.expiry, lot_size=c.lot_size, lots=lots, spot=c.spot,
                  forward=c.forward(now), source=c.source, as_of=c.as_of, legs=legs)
    s.net_premium = round(sum(-lg.sign * lg.price * lg.lots for lg in legs) / lots, 2)
    lo, hi = c.spot * 0.6, c.spot * 1.4
    grid = [lo + (hi - lo) * i / 800 for i in range(801)] + [lg.strike for lg in legs]
    grid.sort()
    vals = [_expiry_value(legs, x) * c.lot_size for x in grid]
    # unlimited if the payoff still slopes downward at either end of a wide grid
    slope_hi = _expiry_value(legs, c.spot * 3) - _expiry_value(legs, c.spot * 2)
    slope_lo = _expiry_value(legs, c.spot * 0.05) - _expiry_value(legs, c.spot * 0.3)
    s.naked = slope_hi < -1e-9 or slope_lo < -1e-9
    upside_open = slope_hi > 1e-9 or slope_lo > 1e-9
    s.max_loss = None if s.naked else round(-min(min(vals), 0.0), 2)
    s.max_profit = None if upside_open else round(max(max(vals), 0.0), 2)
    s.breakevens = []
    for (x0, v0), (x1, v1) in zip(zip(grid, vals), zip(grid[1:], vals[1:])):
        if v0 == 0 or (v0 < 0) != (v1 < 0):
            be = x0 if v0 == 0 else x0 + (x1 - x0) * (-v0) / (v1 - v0)
            if not s.breakevens or abs(be - s.breakevens[-1]) > 1:
                s.breakevens.append(round(be, 1))
    # Greeks of the position (per position, rupees per point / per day / per vol point)
    tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for lg in legs:
        g = c.greeks(lg.strike, lg.kind, now)
        if g:
            for k in tot:
                tot[k] += lg.sign * getattr(g, k) * lg.lots * c.lot_size
    s.greeks = {k: round(v, 2 if k != "gamma" else 4) for k, v in tot.items()}
    # margin: premium for buyers; the max loss for hedged sellers; a % of notional for naked shorts
    shorts = sum(lg.lots for lg in legs if lg.side == "sell")
    if s.naked:
        s.margin = round(shorts * c.lot_size * c.spot * NAKED_MARGIN_PCT / 100, 2)
    elif s.net_premium < 0 and shorts == 0:
        s.margin = round(-s.net_premium * s.qty, 2)
    else:   # hedged: the worst case plus exposure margin on every short leg (an estimate; Kite's basket margin replaces it)
        s.margin = round(max(s.max_loss or 0, -s.net_premium * s.qty, 0)
                         + shorts * c.lot_size * c.spot * EXPOSURE_PCT / 100, 2)
    buy_v = sum(lg.price * lg.lots * c.lot_size for lg in legs if lg.side == "buy")
    sell_v = sum(lg.price * lg.lots * c.lot_size for lg in legs if lg.side == "sell")
    s.charges_round_trip = options_charges(buy_v + sell_v, buy_v + sell_v, len(legs), len(legs)).total
    # payoff curve: at expiry, and today with each leg's own IV
    t = c.years_left(now)
    pts = []
    for i in range(61):
        x = c.spot * (0.94 + 0.12 * i / 60)
        today = 0.0
        for lg in legs:
            g = c.greeks(lg.strike, lg.kind, now)
            iv = None
            if g is not None and lg.mid:
                iv = pricing.implied_vol(lg.kind, lg.mid, c.spot, lg.strike, t, fwd=s.forward)
            price = pricing.black76(lg.kind, x, lg.strike, t, iv or 0.15, fwd=s.forward * x / c.spot).price
            today += lg.sign * (price - lg.price) * lg.lots
        pts.append({"x": round(x, 1), "expiry": round(_expiry_value(legs, x) * c.lot_size, 2), "today": round(today * c.lot_size, 2)})
    s.payoff = pts
    e = c.expiry
    s.label = f"{c.underlying} {e:%d%b}".upper() + " " + template.replace("_", " ").upper()
    return s
