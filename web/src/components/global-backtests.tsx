"use client";

import { useEffect, useState } from "react";

import { Badge, Notice, PageHeader, Panel, Stat, StatStrip } from "@/components/ui/kit";
import { api, type GlobalBacktestOverview, type GlobalBacktestReport } from "@/lib/api";

const SESSION_NAME: Record<string, string> = { asia: "Asia", london: "London", new_york: "New York" };
const r = (x: number | null | undefined) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(2)}R`);
const tone = (x: number | null | undefined) => (x == null ? null : x > 0 ? "up" as const : x < 0 ? "down" as const : null);
const usd = (x: number | null | undefined) => (x == null ? "–" : `${x < 0 ? "−" : ""}$${Math.abs(x).toLocaleString("en-US", { maximumFractionDigits: 0 })}`);

function StrategyReport({ name, description, rep }: { name: string; description: string; rep?: GlobalBacktestReport }) {
  const s = rep?.summary;
  return (
    <Panel title={name} actions={rep && <span className="text-2xs text-subtle">{rep.first_session} to {rep.last_session} · {rep.sessions_in_data} sessions</span>}>
      <p className="text-xs text-muted">{description}</p>
      {!rep ? <p className="mt-3 text-xs text-subtle">Not run yet.</p> : (
        <div className="mt-3 flex flex-col gap-3">
          <p className="text-[13px] font-medium text-fg">{rep.verdict.text}</p>
          <StatStrip cols={5}>
            <Stat label="Trades" value={<span className="num">{s?.trades ?? 0}</span>} sub={`of ${rep.verdict.gates.min_trades} needed`} />
            <Stat label="Average per trade" value={<span className="num">{r(s?.expectancy_r)}</span>} tone={tone(s?.expectancy_r)} sub="after costs" />
            <Stat label="At double slippage" value={<span className="num">{r(rep.double_slippage.expectancy_r)}</span>} tone={tone(rep.double_slippage.expectancy_r)} sub="slippage is an assumption" />
            <Stat label="Win rate" value={<span className="num">{s?.win_rate != null ? `${(s.win_rate * 100).toFixed(0)}%` : "–"}</span>} sub={s?.profit_factor != null ? `profit factor ${s.profit_factor.toFixed(2)}` : undefined} />
            <Stat label="Net result" value={<span className="num">{usd(s?.net_pnl)}</span>} tone={tone(s?.net_pnl)} sub={s?.costs != null ? `costs ${usd(s.costs)}` : undefined} />
          </StatStrip>
          {(s?.trades ?? 0) > 0 && (
            <div className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
              {Object.entries({ ...rep.by_market_session, ...rep.by_side }).map(([k, v]) => (
                <div key={k} className="flex justify-between border-b border-line py-1">
                  <span className="text-muted">{SESSION_NAME[k] ?? (k === "buy" ? "Longs" : k === "sell" ? "Shorts" : k)}</span>
                  <span className="num text-fg">{v.trades} trades · {r(v.expectancy_r)}</span>
                </div>
              ))}
            </div>
          )}
          <p className="text-2xs text-subtle">Data: {rep.data_kind}. Run {new Date(rep.ran_at * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} IST.</p>
        </div>
      )}
    </Panel>
  );
}

export function GlobalBacktests() {
  const [data, setData] = useState<GlobalBacktestOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    api<GlobalBacktestOverview>("/global/backtests").then(setData).catch((e: Error) => setError(e.message));
  }, []);

  function run() {
    setRunning(true);
    setError(null);
    api<GlobalBacktestOverview>("/global/backtests/run", { method: "POST" })
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setRunning(false));
  }

  return (
    <>
      <PageHeader title="XAUUSD backtests" subtitle="Gold's own strategies at fixed settings, after spread and slippage."
        meta={<Badge tone="neutral">research days only</Badge>}
        actions={<button className="btn btn-primary" onClick={run} disabled={running || !data?.research_days_stored}>{running ? "Running" : "Run on stored days"}</button>} />
      {error && <Notice tone="error">{error}</Notice>}
      {data && (
        <Notice title={`${data.research_days_stored} of ${data.research_days_possible} research days stored`}>
          Results use days from {data.research_start} up to {data.held_back_from}. Everything from {data.held_back_from} on stays hidden for the one Lab test. The history download fills in research days first while the app runs.
        </Notice>
      )}
      {data?.strategies.map((s) => <StrategyReport key={s.id} name={s.name} description={s.description} rep={data.reports[s.id]} />)}
    </>
  );
}
