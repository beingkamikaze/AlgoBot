"""The XAUUSD backtester (plan phase X3). Every expected number below was worked by hand from these rules: at a mid of 4000.00
and a $0.60 spread, the bid is 3999.70 and the ask 4000.30; a long enters at the ask plus $0.10 slippage, so 4000.40."""

import datetime as dt
import subprocess
import sys
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.domain.signal import Condition
from app.global_markets import report
from app.global_markets.backtest import BacktestConfig, from_bars, from_columns, run
from app.global_markets.clock import LONDON, NEW_YORK
from app.global_markets.costs import CostModel, spread_by_hour
from app.global_markets.data import store
from app.global_markets.strategies.base import Candidate, Frame, Strategy, lookahead_violations
from app.main import app
from app.market.models import Bar

SPREAD = 0.60
T0 = int(dt.datetime(2026, 11, 11, 9, 0, tzinfo=NEW_YORK).timestamp())      # a Wednesday, inside the entry window


def book(start, rows, spread=SPREAD):
    """1-minute columns from (open, high, low, close) mid prices, or one number for a flat minute."""
    rows = [(r, r, r, r) if isinstance(r, (int, float)) else r for r in rows]
    a = np.array(rows, dtype=float)
    half = spread / 2
    return {"t": start + 60 * np.arange(len(rows), dtype=np.int64),
            "bo": a[:, 0] - half, "bh": a[:, 1] - half, "bl": a[:, 2] - half, "bc": a[:, 3] - half,
            "ao": a[:, 0] + half, "ah": a[:, 1] + half, "al": a[:, 2] + half, "ac": a[:, 3] + half, "v": np.ones(len(rows))}


def join(*books):
    return {k: np.concatenate([b[k] for b in books]) for k in books[0]}


class Fixed(Strategy):
    """Fires on the 5-minute bars starting at chosen times, with a chosen stop: for testing fills."""
    id, version, name, description = "fixed", "1", "Fixed", "test"
    defaults = {"at": (T0,), "side": "buy", "stop": 3992.90, "target_r": 2.0}

    def check(self, f, i):
        if int(f.t[i]) not in self.params["at"]:
            return None
        return Candidate(side=self.params["side"], signal_index=i, stop=self.params["stop"], target_r=self.params["target_r"],
                         rule="test", conditions=[Condition(name="x", met=True, detail="x")], invalidation="x", stop_reason="x")


def trades(columns, cfg=BacktestConfig(), **params):
    f, ex = from_columns("XAUUSD", "5m", columns)
    return run(Fixed(**params), f, ex, cfg)


def flat(n, price=4000.0):
    return [price] * n


# ---------------------------------------------------------------- entries and targets

def test_long_enters_next_open_at_the_ask_and_reaches_its_target():
    t = trades(book(T0, flat(7) + [(4000, 4016, 4000, 4016)]))[0]
    assert t.entry_t == T0 + 300 and t.entry == 4000.40 and t.side == "buy"
    assert t.lots == 0.02 and t.risk_per_unit == 7.5            # $20 / ($7.50 + $0.20 slippage both ways) = 2.59 oz, rounded down to 2
    assert t.target == 4015.40 and t.exit == 4015.40 and t.exit_reason == "target" and t.exit_t == T0 + 420
    assert t.net == 30.00 and t.r == 2.0
    assert t.costs == 1.40 and t.gross == 31.40                 # (half spread + slippage in, half spread out) x 2 oz


def test_touching_the_target_is_not_a_fill():
    columns = book(T0, flat(8))
    columns["bh"][7] = 4000.40 + 2 * (4000.40 - 3992.90)        # the bid reaches the target exactly, to the last digit
    assert trades(columns)[0].exit_reason != "target"


def test_the_bid_must_pass_the_target_not_the_mid():
    # mid 4015.50 is above the target, but the bid at 4015.20 is not
    assert trades(book(T0, flat(7) + [(4000, 4015.50, 4000, 4000)]))[0].exit_reason != "target"


def test_no_entry_until_the_signal_bar_has_closed():
    t = trades(book(T0, [(4000, 4000, 4000, 4000)] * 4 + [4050.0] + flat(3, 4050.0)), stop=4040.0)
    assert t[0].entry_t == T0 + 300 and t[0].entry == 4050.40    # the bar's last minute is seen, and the fill is after it


# ---------------------------------------------------------------- stops

def test_stop_hit_loses_one_r_plus_slippage():
    t = trades(book(T0, flat(7) + [(4000, 4000, 3993.00, 4000)]))[0]    # bid low 3992.70 passes the 3992.90 stop
    assert t.exit_reason == "stop" and t.exit == 3992.80
    assert t.net == -15.20 and t.r == round(-15.20 / 15, 4)
    assert t.costs == 1.60                                      # slippage on the way out too


def test_long_stop_watches_the_bid_not_the_mid():
    assert trades(book(T0, flat(7) + [(4000, 4000, 3993.10, 4000)]))[0].exit_reason == "stop"   # mid above the stop, bid 3992.80 below it
    assert trades(book(T0, flat(7) + [(4000, 4000, 3993.30, 4000)]))[0].exit_reason != "stop"   # bid 3993.00 stays above it


def test_stop_wins_when_both_are_hit_in_one_bar():
    assert trades(book(T0, flat(7) + [(4000, 4020, 3990, 4000)]))[0].exit_reason == "stop"


def test_gap_through_the_stop_fills_at_the_open():
    t = trades(book(T0, flat(7) + [(3980, 3980, 3980, 3980)]))[0]
    assert t.exit == 3979.60 and t.net == round((3979.60 - 4000.40) * 2, 2) and t.r == round(-41.60 / 15, 4)


def test_no_entry_if_the_open_is_already_past_the_stop():
    assert trades(book(T0, flat(5) + flat(3, 3990.0))) == []


# ---------------------------------------------------------------- shorts

SHORT = {"side": "sell", "stop": 4007.10}


def test_short_enters_at_the_bid_and_reaches_its_target_on_the_ask():
    t = trades(book(T0, flat(7) + [(4000, 4000, 3984.20, 3985)]), **SHORT)[0]       # ask low 3984.50 passes the 3984.60 target
    assert t.entry == 3999.60 and t.target == 3984.60 and t.exit == 3984.60 and t.exit_reason == "target"
    assert t.lots == 0.02 and t.net == 30.00 and t.r == 2.0


def test_short_stop_watches_the_ask():
    t = trades(book(T0, flat(7) + [(4000, 4006.85, 4000, 4000)]), **SHORT)[0]       # ask high 4007.15 passes the 4007.10 stop
    assert t.exit_reason == "stop" and t.exit == 4007.20 and t.net == -15.20


def test_short_gap_fills_at_the_ask_open():
    t = trades(book(T0, flat(7) + [(4020, 4020, 4020, 4020)]), **SHORT)[0]
    assert t.exit == 4020.40 and t.net == round((3999.60 - 4020.40) * 2, 2)


# ---------------------------------------------------------------- sizing

def test_stop_too_close_is_skipped():
    assert trades(book(T0, flat(10)), stop=3999.0) == []        # $1.40 risk is under 0.05 % of the price


def test_stop_too_far_for_the_max_loss_is_refused_not_shrunk():
    assert trades(book(T0, flat(10)), stop=3970.0) == []        # $30.40 an ounce: even 1 oz breaks the $20 limit


def test_position_value_is_capped_with_no_leverage():
    assert trades(book(T0, flat(10)), stop=3997.40)[0].lots == 0.02     # the risk allows 6 oz; $10,000 buys 2 oz


def test_smaller_size_when_the_stop_is_wider():
    assert trades(book(T0, flat(10)), stop=3986.40)[0].lots == 0.01     # $14 stop


def test_commission_comes_off():
    cfg = BacktestConfig(costs=CostModel(commission_per_lot=7.0))
    t = trades(book(T0, flat(7) + [(4000, 4016, 4000, 4016)]), cfg)[0]
    assert t.net == 29.86 and t.costs == 1.54


# ---------------------------------------------------------------- time: flat by, Fridays, holidays, the entry window

def test_flat_by_13_00_new_york_at_the_bid_close():
    t = trades(book(T0, flat(240), spread=SPREAD))[0]
    assert t.exit_reason == "time" and t.exit_t == T0 + 239 * 60        # the 12:59 bar ends at 13:00
    assert t.exit == 3999.60 and t.net == -1.60


def test_friday_exits_on_friday_and_is_never_held_over_the_weekend():
    fri = int(dt.datetime(2026, 11, 6, 9, 0, tzinfo=NEW_YORK).timestamp())
    mon = int(dt.datetime(2026, 11, 9, 9, 0, tzinfo=NEW_YORK).timestamp())
    t = trades(join(book(fri, flat(240)), book(mon, flat(30))), at=(fri,))
    assert len(t) == 1 and t[0].exit_t == fri + 239 * 60 and t[0].session == "2026-11-06"


@pytest.mark.parametrize("day,last_bar", [((2026, 11, 26), (12, 44)), ((2026, 12, 24), (12, 44)), ((2026, 11, 27), (12, 44))])
def test_short_days_exit_before_the_early_close(day, last_bar):
    start = int(dt.datetime(*day, 9, 0, tzinfo=NEW_YORK).timestamp())
    t = trades(book(start, flat(235)), at=(start,))[0]
    assert t.exit_reason == "time"
    assert t.exit_t == int(dt.datetime(*day, *last_bar, tzinfo=NEW_YORK).timestamp())


def test_bars_on_a_closed_day_are_not_traded():
    good_friday = int(dt.datetime(2026, 4, 3, 9, 0, tzinfo=NEW_YORK).timestamp())
    assert trades(book(good_friday, flat(30)), at=(good_friday,)) == []


def test_weekend_prints_are_dropped():
    sat = int(dt.datetime(2026, 11, 7, 9, 0, tzinfo=NEW_YORK).timestamp())
    f, ex = from_columns("XAUUSD", "5m", book(sat, flat(30)))
    assert len(f) == 0 and len(ex) == 0


def test_daily_break_prints_are_dropped():
    brk = int(dt.datetime(2026, 11, 11, 17, 10, tzinfo=NEW_YORK).timestamp())
    assert len(from_columns("XAUUSD", "5m", book(brk, flat(10)))[1]) == 0


def test_last_signal_is_at_11_00_new_york():
    t1055 = int(dt.datetime(2026, 11, 11, 10, 55, tzinfo=NEW_YORK).timestamp())   # closes 11:00: allowed
    t1100 = t1055 + 300                                                           # closes 11:05: too late
    assert trades(book(t1055, flat(20)), at=(t1055,))[0].entry_t == t1100
    assert trades(book(t1055, flat(20)), at=(t1100,)) == []


def test_first_signal_is_at_08_00_london():
    open_ = int(dt.datetime(2026, 11, 11, 8, 0, tzinfo=LONDON).timestamp())
    assert trades(book(open_ - 600, flat(20)), at=(open_ - 300,))[0].entry_t == open_     # closes exactly at 08:00
    assert trades(book(open_ - 600, flat(20)), at=(open_ - 600,)) == []


def test_a_late_signal_after_a_hole_in_the_data_is_dropped():
    assert trades(join(book(T0, flat(5)), book(T0 + 1500, flat(5)))) == []       # next data 20 minutes after the signal


def test_data_ending_early_closes_the_trade():
    t = trades(book(T0, flat(10)))[0]
    assert t.exit_reason == "session-end" and t.exit_t == T0 + 540


def test_at_most_two_trades_a_day():
    ats = tuple(T0 + 600 * i for i in range(4))
    # each trade stops out in the minute after it enters
    rows = []
    for _ in range(4):
        rows += flat(5) + [4000.0, (4000, 4000, 3990, 4000)] + flat(3)
    t = trades(book(T0, rows), at=ats)
    assert len(t) == 2 and {x.session for x in t} == {"2026-11-11"}


# ---------------------------------------------------------------- the shared stop rules

def test_breakeven_moves_the_stop_for_the_next_bar():
    rows = flat(7) + [(4000, 4008.50, 4000, 4005)] + [(4005, 4005, 3999.00, 4000)]
    t = trades(book(T0, rows), breakeven_at_r=1.0)[0]
    # bid 4008.20 passes +1R (4007.90), then the stop is the 4000.40 entry; bid 3998.70 stops it, less $0.10
    assert t.exit_reason == "stop" and t.exit == 4000.30 and t.net == -0.20
    assert t.management["final_stop"] == 4000.40


def test_partial_is_booked_at_its_level_and_the_rest_runs():
    rows = flat(7) + [(4000, 4008.50, 4000, 4005)] + [(4005, 4016, 4005, 4016)]
    t = trades(book(T0, rows), partial_at_r=1.0)[0]
    assert t.exit_reason == "target" and t.management["partial"]["price"] == 4007.90
    assert t.net == 22.50 and t.r == 1.5                        # 1 oz x $7.50 + 1 oz x $15.00, on $15 at risk
    assert t.costs == 1.40


# ---------------------------------------------------------------- bars with no bid or ask (GC=F)

def test_futures_bars_use_an_assumed_spread_and_say_so():
    bars = [Bar(t=T0 + 300 * i, o=4000.0, h=4000.0, l=4000.0, c=4000.0, v=1, complete=True) for i in range(3)]
    bars.append(Bar(t=T0 + 900, o=4000.0, h=4016.0, l=4000.0, c=4016.0, v=1, complete=True))
    f, ex = from_bars("XAUUSD", "5m", bars)
    assert "assumed" in ex.data_kind and ex.seconds == 300
    t = run(Fixed(), f, ex)[0]
    assert t.entry == 4000.40 and t.entry_t == T0 + 300 and t.exit == 4015.40 and t.exit_t == T0 + 900 and t.net == 30.00


def test_forming_bars_are_not_used():
    bars = [Bar(t=T0, o=1.0, h=1.0, l=1.0, c=1.0, v=1, complete=False)]
    assert len(from_bars("XAUUSD", "5m", bars)[1]) == 0


# ---------------------------------------------------------------- the future stays hidden

def walk_columns(days=8, seed=5):
    """Random 1-minute prices over several trading days, all sessions."""
    rng = np.random.default_rng(seed)
    parts, price = [], 4000.0
    day = dt.date(2026, 10, 5)
    for _ in range(days):
        start = int(dt.datetime(day.year, day.month, day.day, 2, 0, tzinfo=NEW_YORK).timestamp())
        rows = []
        for _ in range(600):
            o = price
            c = o + rng.normal(0, 0.7)
            rows.append((o, max(o, c) + abs(rng.normal(0, 0.3)), min(o, c) - abs(rng.normal(0, 0.3)), c))
            price = c
        parts.append(book(start, rows))
        day += dt.timedelta(days=1)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
    return join(*parts)


class EmaCross(Strategy):
    """An honest strategy: reads only bars up to i."""
    id, version, name, description = "ema-cross", "1", "EMA cross", "test"
    defaults = {"n": 10}

    def check(self, f, i):
        e = f.ind("ema", self.params["n"])
        if i < 1 or np.isnan(e[i - 1]) or not (f.c[i - 1] <= e[i - 1] and f.c[i] > e[i]):
            return None
        return Candidate(side="buy", signal_index=i, stop=round(float(f.c[i]) - 8.0, 2), target_r=2.0, rule="cross",
                         conditions=[], invalidation="x", stop_reason="x")


class Cheat(EmaCross):
    """Reads the next bar, which it could not know."""
    id = "cheat"

    def check(self, f, i):
        if i + 1 < len(f) and f.c[i + 1] > f.c[i] + 1.0:
            return Candidate(side="buy", signal_index=i, stop=float(f.c[i]) - 8.0, target_r=2.0, rule="peek", conditions=[], invalidation="x", stop_reason="x")
        return None


class RandomEntry(Strategy):
    """A coin flip: it has no edge, so its average result must be about minus the costs."""
    id, version, name, description = "coin", "1", "Coin", "test"
    defaults = {"seed": 1}

    def check(self, f, i):
        rng = np.random.default_rng([self.params["seed"], int(f.t[i])])
        if rng.random() > 0.05:
            return None
        side = "buy" if rng.random() < 0.5 else "sell"
        stop = float(f.c[i]) - 10.0 if side == "buy" else float(f.c[i]) + 10.0
        return Candidate(side=side, signal_index=i, stop=stop, target_r=1.0, rule="coin", conditions=[], invalidation="x", stop_reason="x")


def test_an_honest_strategy_passes_the_future_check():
    f, _ = from_columns("XAUUSD", "5m", walk_columns())
    assert lookahead_violations(EmaCross(), f) == []


def test_a_cheating_strategy_is_caught():
    f, _ = from_columns("XAUUSD", "5m", walk_columns())
    assert lookahead_violations(Cheat(), f) != []


def test_later_data_does_not_change_earlier_trades():
    columns = walk_columns()
    f, ex = from_columns("XAUUSD", "5m", columns)
    full = run(EmaCross(), f, ex)
    assert len(full) >= 4
    last_day = sorted(set(ex.day.tolist()))[-3]
    cut = columns["t"] < int(dt.datetime.fromisoformat(f"{last_day}T00:00:00+00:00").timestamp())
    f2, ex2 = from_columns("XAUUSD", "5m", {k: v[cut] for k, v in columns.items()})
    short = run(EmaCross(), f2, ex2)
    closed_days = sorted({t.session for t in short})[:-1]
    assert closed_days
    before = [t.to_dict() for t in full if t.session in closed_days]
    assert before == [t.to_dict() for t in short if t.session in closed_days]


def test_every_trade_adds_up():
    f, ex = from_columns("XAUUSD", "5m", walk_columns())
    ts = run(RandomEntry(), f, ex)
    assert len(ts) > 5
    for t in ts:
        assert t.gross - t.costs == pytest.approx(t.net, abs=0.011) and t.costs > 0
        assert t.entry_t > t.signal_t and t.exit_t >= t.entry_t and t.market_session in ("london", "new_york")


def test_a_coin_flip_loses_about_its_costs():
    many = []
    columns = walk_columns(days=40, seed=9)
    f, ex = from_columns("XAUUSD", "5m", columns)
    for seed in range(12):
        many += run(RandomEntry(seed=seed), f, ex)
    cost_r = np.mean([t.costs / (t.risk_per_unit * t.lots * 100) for t in many])
    mean_r = np.mean([t.r for t in many])
    stderr = np.std([t.r for t in many]) / np.sqrt(len(many))
    assert len(many) > 200 and cost_r > 0.05
    assert abs(mean_r + cost_r) < 3 * stderr + 0.02


# ---------------------------------------------------------------- the report keeps the held-back days hidden

def report_columns():
    return walk_columns(days=20, seed=4)


def test_held_back_results_stay_hidden_by_default():
    f, ex = from_columns("XAUUSD", "5m", report_columns())
    out = report.report(EmaCross(), f, ex)
    cut = out["held_back_from"]
    days = sorted(set(ex.day.tolist()))
    assert out["sessions_in_data"] == 20 and cut == days[14]    # 70 % of the sessions in the data
    assert out["held_back"] == "hidden: settings are still being chosen"
    assert all(k <= cut[:7] for k in out["by_month"])
    assert out["summary"]["sessions"] <= 14
    assert all(p["t"] < int(dt.datetime.fromisoformat(f"{cut}T00:00:00+00:00").timestamp()) + 86400 for p in out["equity_curve"])


def test_held_back_results_only_when_asked():
    f, ex = from_columns("XAUUSD", "5m", report_columns())
    hidden = report.report(EmaCross(), f, ex)
    shown = report.report(EmaCross(), f, ex, reveal_held_back=True)
    assert shown["held_back"]["summary"]["trades"] > 0
    assert hidden["summary"] == shown["summary"]
    every = run(EmaCross(), f, ex)
    assert hidden["summary"]["trades"] + shown["held_back"]["summary"]["trades"] == len(every)


def test_cutoff_counts_sessions_not_trades():
    assert report.cutoff(np.array(["d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8", "d9", "e0"])) == "d8"
    assert report.cutoff(np.array(["d1", "d2"])) is None


def test_double_slippage_is_reported_and_is_worse():
    f, ex = from_columns("XAUUSD", "5m", report_columns())
    out = report.report(EmaCross(), f, ex)
    assert out["double_slippage"]["trades"] > 0
    assert out["double_slippage"]["expectancy_r"] < out["summary"]["expectancy_r"]


def test_report_states_what_data_it_used():
    f, ex = from_columns("XAUUSD", "5m", report_columns())
    assert report.report(EmaCross(), f, ex)["data_kind"] == "spot, real bid and ask"


@pytest.mark.parametrize("s,word", [
    ({"trades": 10, "expectancy_r": 1.0, "profit_factor": 3.0}, "Too few"),
    ({"trades": 150, "expectancy_r": -0.1, "profit_factor": 0.8}, "Loses money"),
    ({"trades": 150, "expectancy_r": 0.05, "profit_factor": 1.1}, "below the gates"),
    ({"trades": 150, "expectancy_r": 0.2, "profit_factor": 1.5}, "Passes"),
])
def test_verdicts(s, word):
    assert word in report.verdict(s)["text"]


def test_gates_match_nse():
    from app.backtest.metrics import GATES
    assert report.GATES == GATES


def test_empty_report_does_not_crash():
    f, ex = from_columns("XAUUSD", "5m", book(T0, flat(10)))
    out = report.report(Fixed(at=()), f, ex)
    assert out["summary"] == {"trades": 0}


def test_frame_rejects_daily_bars():
    with pytest.raises(ValueError, match="intraday"):
        Frame("XAUUSD", "1d", replace_ohlcv())


def replace_ohlcv():
    from app.global_markets.data.bars import Ohlcv
    z = np.array([], dtype=float)
    return Ohlcv(np.array([], dtype=np.int64), z, z, z, z, z)


def test_unknown_parameter_is_refused():
    with pytest.raises(ValueError, match="unknown"):
        Fixed(nope=1)


# ---------------------------------------------------------------- costs

def test_cost_model():
    assert CostModel().slippage == 0.10
    assert CostModel(slippage=0.1, commission_per_lot=7).round_trip_per_unit(100) == pytest.approx(0.27)
    with pytest.raises(ValueError):
        CostModel(slippage=-1)


def test_spread_by_new_york_hour():
    a = book(T0, flat(60))                                                  # 09:00 New York, $0.60
    b = book(T0 + 3600, flat(60), spread=1.0)                               # 10:00 New York, $1.00
    rows = spread_by_hour(join(a, b))
    assert [(r["new_york_hour"], r["median"], r["minutes"]) for r in rows] == [(9, 0.6, 60), (10, 1.0, 60)]


def test_spread_route(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    client = TestClient(app)
    assert client.get("/api/global/data/spread").json()["by_new_york_hour"] == []
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    start = now - now % 86400 - 86400
    columns = book(start, flat(5))
    store.save_day("XAUUSD", dt.datetime.fromtimestamp(start, dt.timezone.utc).date(), columns)
    out = client.get("/api/global/data/spread").json()
    assert out["minutes"] == 5 and out["by_new_york_hour"][0]["median"] == 0.6


# ---------------------------------------------------------------- kept apart from NSE

def test_backtester_does_not_load_nse_market_code():
    code = ("import sys; import app.global_markets.report; "
            "bad = ['app.core.calendar', 'app.core.instruments', 'app.market.yahoo', 'app.backtest.engine', 'app.backtest.metrics', "
            "'app.strategies.base', 'app.risk.engine', 'app.paper.service']; print([m for m in bad if m in sys.modules])")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip() == "[]"


def test_config_is_a_plain_replaceable_value():
    assert replace(BacktestConfig(), risk_per_trade=50).risk_per_trade == 50
