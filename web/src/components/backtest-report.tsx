"use client";

import { useState } from "react";

import { EquityChart } from "@/components/equity-chart";
import { IconCheck, IconX } from "@/components/ui/icons";
import { Notice, Panel, Pnl, RValue, Side, Stat, StatStrip, TableWrap, Term, TERMS, cx, px } from "@/components/ui/kit";
import { inr, pct, r, type Backtest, type Group, type Stats } from "@/lib/api";

function GroupTable({ title, g, limit }: { title: string; g: Group; limit?: number }) {
  let rows = Object.entries(g);
  if (limit) rows = rows.sort((a, b) => b[1].net_pnl - a[1].net_pnl);
  const shown = limit && rows.length > limit * 2 ? [...rows.slice(0, limit), ...rows.slice(-limit)] : rows;
  if (!rows.length) return null;
  return (
    <Panel title={title} flush actions={limit && rows.length > limit * 2 ? <span className="text-2xs text-subtle">best and worst {limit} of {rows.length}</span> : undefined}>
      <TableWrap>
        <table className="dtable dtable-compact">
          <thead><tr><th></th><th className="r">Trades</th><th className="r">Win</th><th className="r">Avg</th><th className="r">Net</th></tr></thead>
          <tbody>
            {shown.map(([k, v]) => (
              <tr key={k}>
                <td className="max-w-44 truncate">{k.replace(/^\d\. /, "")}</td>
                <td className="r text-muted">{v.trades}</td>
                <td className="r text-muted">{pct(v.win_rate)}</td>
                <td className="r"><RValue v={v.expectancy_r} /></td>
                <td className="r"><Pnl v={v.net_pnl} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>
    </Panel>
  );
}

function Compare({ a, b, split }: { a: Stats; b: Stats; split: string | null }) {
  const rows: [string, (s: Stats) => React.ReactNode][] = [
    ["Trades", (s) => String(s.trades ?? 0)],
    ["Win rate", (s) => pct(s.win_rate)],
    ["Average R", (s) => <RValue v={s.expectancy_r} />],
    ["Profit factor", (s) => s.profit_factor?.toFixed(2) ?? "–"],
    ["Net", (s) => <Pnl v={s.net_pnl} />],
  ];
  return (
    <Panel title="In-sample vs held back" flush actions={split && <span className="num text-2xs text-subtle">split on {split}</span>}>
      <table className="dtable dtable-compact">
        <thead><tr><th></th><th className="r">First 70%</th><th className="r">Last 30% (held back)</th></tr></thead>
        <tbody>
          {rows.map(([k, f]) => (
            <tr key={k}><td className="text-muted">{k}</td><td className="r">{f(a)}</td><td className="r">{f(b)}</td></tr>
          ))}
        </tbody>
      </table>
      <p className="border-t border-line px-3 py-2 text-2xs text-subtle">If the held-back period disagrees with the first, the result is probably luck or overfitting.</p>
    </Panel>
  );
}

export function BacktestReport({ bt }: { bt: Backtest }) {
  const [showTrades, setShowTrades] = useState(50);
  const rep = bt.report;
  if (!rep) return null;
  const s = rep.summary;
  if (!s.trades) return <div className="panel p-6 text-center text-sm text-muted">No trades: the strategy never fired on this data.</div>;
  const passed = Object.values(rep.verdict.checks).every(Boolean);
  const trades = bt.trades ?? [];
  const gate = (ok: boolean, label: string) => (
    <span className={cx("inline-flex items-center gap-1", ok ? "text-up" : "text-down")}>{ok ? <IconCheck size={12} /> : <IconX size={12} />}{label}</span>
  );
  return (
    <div className="flex flex-col gap-4">
      <Notice tone={passed ? "success" : "warn"} title={rep.verdict.text}>
        <span className="num flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {gate(rep.verdict.checks.enough_trades, `≥ ${rep.verdict.gates.min_trades} trades`)}
          {gate(rep.verdict.checks.positive_expectancy, `average ≥ +${rep.verdict.gates.min_expectancy_r}R`)}
          {gate(rep.verdict.checks.profit_factor, `profit factor ≥ ${rep.verdict.gates.min_profit_factor}`)}
          {bt.period?.from && <span className="text-muted">{bt.period.from} → {bt.period.to}</span>}
        </span>
      </Notice>

      <StatStrip cols={4}>
        <Stat label="Trades" value={s.trades} sub={`${s.sessions ?? "–"} sessions`} />
        <Stat label={<Term tip={TERMS.expectancy}>Average per trade</Term>} value={r(s.expectancy_r)} tone={(s.expectancy_r ?? 0) > 0 ? "up" : "down"} sub={`win ${r(s.avg_win_r)} · loss ${r(s.avg_loss_r)}`} />
        <Stat label={<Term tip={TERMS.winRate}>Win rate</Term>} value={pct(s.win_rate)} sub={`worst streak ${s.worst_losing_streak ?? "–"} losses`} />
        <Stat label={<Term tip={TERMS.pf}>Profit factor</Term>} value={s.profit_factor?.toFixed(2) ?? "–"} tone={s.profit_factor != null ? (s.profit_factor >= 1 ? "up" : "down") : null} />
      </StatStrip>
      <StatStrip cols={4}>
        <Stat label="Net P&L" value={<Pnl v={s.net_pnl ?? 0} plain />} tone={(s.net_pnl ?? 0) > 0 ? "up" : "down"} />
        <Stat label="Charges paid" value={inr(s.costs ?? 0)} />
        <Stat label={<Term tip={TERMS.drawdown}>Max drawdown</Term>} value={inr(s.max_drawdown ?? 0)} />
        <Stat label="Bad-luck drawdown (95%)" value={rep.monte_carlo.max_drawdown_95 != null ? inr(rep.monte_carlo.max_drawdown_95) : "–"} hint="95th percentile of the max drawdown over random re-orderings of these trades" />
      </StatStrip>

      <Panel title="Equity curve" actions={<span className="text-2xs text-subtle">after charges</span>}>
        <EquityChart points={rep.equity_curve} height="h-64" />
        <p className="mt-2 text-2xs text-subtle">
          Exits: {Object.entries(s.exit_reasons ?? {}).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ")}.{" "}
          {bt.config.fixed_lots ? `Sized at ${bt.config.fixed_lots} lot a trade` : `Sized at ${inr(bt.config.risk_per_trade)} risk a trade`}; R = net result ÷ money at risk.
        </p>
      </Panel>

      <div className="grid gap-4 md:grid-cols-2">
        <Compare a={rep.in_sample} b={rep.out_of_sample} split={rep.split_date} />
        <GroupTable title="By entry time" g={rep.by_time} />
        <GroupTable title="By volatility (yesterday's India VIX)" g={rep.by_vix} />
        <GroupTable title="By Nifty's opening gap" g={rep.by_nifty_gap} />
        <GroupTable title="By side" g={rep.by_side} />
        <GroupTable title="By month" g={rep.by_month} />
        <GroupTable title="By symbol" g={rep.by_symbol} limit={5} />
      </div>

      <Panel title={`Trades (${trades.length})`} flush actions={<span className="text-2xs text-subtle">hover a row for the rule that fired</span>}>
        <TableWrap max="560px">
          <table className="dtable dtable-compact">
            <thead>
              <tr><th>Date</th><th>Symbol</th><th>Side</th><th className="r">Entry</th><th className="r">Stop</th><th className="r">Target</th><th className="r">Exit</th><th>Why out</th><th className="r">Qty</th><th className="r">Charges</th><th className="r">Net</th><th className="r">R</th></tr>
            </thead>
            <tbody>
              {trades.slice(0, showTrades).map((t, i) => (
                <tr key={i} title={t.rule}>
                  <td className="num text-muted">{t.session.slice(5)} {new Date(t.entry_t * 1000).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })}</td>
                  <td className="font-medium">{t.symbol}</td>
                  <td><Side side={t.side} /></td>
                  <td className="r">{px(t.entry)}</td><td className="r text-muted">{px(t.stop)}</td><td className="r text-muted">{px(t.target)}</td><td className="r">{px(t.exit)}</td>
                  <td className="text-muted">{t.exit_reason}</td><td className="r text-muted">{t.qty}</td><td className="r text-muted">{t.costs.toFixed(0)}</td>
                  <td className="r"><Pnl v={Math.round(t.net)} /></td>
                  <td className="r"><RValue v={t.r} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
        {trades.length > showTrades && (
          <div className="border-t border-line p-2 text-center"><button onClick={() => setShowTrades((n) => n + 100)} className="btn btn-ghost btn-sm">Show 100 more</button></div>
        )}
      </Panel>
    </div>
  );
}
