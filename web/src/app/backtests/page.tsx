"use client";

import { useCallback, useEffect, useState } from "react";

import { BacktestReport } from "@/components/backtest-report";
import { IconHistory } from "@/components/ui/icons";
import { Empty, Notice, Page, PageHeader, Panel, RValue, StateBadge } from "@/components/ui/kit";
import { api, inr, type Backtest, type BacktestBrief, type LabData, type StrategyInfo, type StrategyVersion } from "@/lib/api";

const UNIVERSES = [
  { id: "NIFTY50", label: "Nifty 50" },
  { id: "NIFTY100", label: "Nifty 100" },
  { id: "NIFTYBANK", label: "Nifty Bank" },
  { id: "INDEXFUT", label: "Index futures (Nifty, Bank Nifty)" },
  { id: "INDEXOPT", label: "Index options (modeled prices)" },
  { id: "custom", label: "Custom list" },
];

export default function BacktestsPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [strategyId, setStrategyId] = useState("orb");
  const [params, setParams] = useState<Record<string, number | boolean>>({});
  const [universe, setUniverse] = useState("NIFTY50");
  const [custom, setCustom] = useState("RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS");
  const [risk, setRisk] = useState(2000);
  const [slippage, setSlippage] = useState(2);
  const [runs, setRuns] = useState<BacktestBrief[]>([]);
  const [selected, setSelected] = useState<Backtest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersion[]>([]);

  const strategy = strategies.find((s) => s.id === strategyId);

  const refreshRuns = useCallback(() => api<BacktestBrief[]>("/backtests").then(setRuns).catch(() => {}), []);

  useEffect(() => {
    api<StrategyInfo[]>("/strategies")
      .then((s) => {
        setStrategies(s);
        setParams(s.find((x) => x.id === "orb")?.defaults ?? {});
      })
      .catch((e: Error) => setError(e.message));
    api<LabData>("/lab").then((d) => setVersions(d.variants.filter((v) => v.user_id))).catch(() => {});   // signed out: none
    refreshRuns();
  }, [refreshRuns]);

  // while a run is going, poll it
  useEffect(() => {
    if (selected?.status !== "running") return;
    const id = setInterval(async () => {
      const b = await api<Backtest>(`/backtests/${selected.id}`);
      setSelected(b);
      if (b.status !== "running") refreshRuns();
    }, 2000);
    return () => clearInterval(id);
  }, [selected, refreshRuns]);

  async function start() {
    setError(null);
    if (!strategy) return;
    const changed = Object.fromEntries(Object.entries(params).filter(([k, v]) => strategy.defaults[k] !== v));
    try {
      const b = await api<Backtest>("/backtests", {
        method: "POST",
        body: {
          strategy: strategyId,
          params: changed,
          universe,
          symbols: universe === "custom" ? custom.split(/[\s,]+/).filter(Boolean) : undefined,
          config: { risk_per_trade: risk, slippage_bps: slippage },
        },
      });
      setSelected(b);
      refreshRuns();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function open(id: string) {
    setSelected(await api<Backtest>(`/backtests/${id}`));
  }

  return (
    <Page wide>
      <PageHeader title="Backtests" subtitle="Every result is after Zerodha charges and slippage, entered at the next bar's open, with the last 30% of days held back as a check." />
      <div className="flex flex-col gap-4 lg:flex-row">
        <div className="flex w-full shrink-0 flex-col gap-4 lg:w-80">
          <Panel title="New run">
            <div className="flex flex-col gap-3">
              <label>
                <span className="field-label">Strategy</span>
                <select
                  value={strategyId}
                  onChange={(e) => {
                    setStrategyId(e.target.value);
                    setParams(strategies.find((s) => s.id === e.target.value)?.defaults ?? {});
                  }}
                  className="field"
                >
                  {strategies.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.timeframe})</option>)}
                </select>
              </label>
              {strategy && <p className="text-xs text-muted">{strategy.description}</p>}
              {strategy && versions.some((v) => v.strategy_id === strategyId) && (
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="text-subtle">Load a lab version:</span>
                  <button onClick={() => setParams(strategy.defaults)} className="chip h-6">default</button>
                  {versions.filter((v) => v.strategy_id === strategyId).map((v) => (
                    <button key={v.id} onClick={() => setParams({ ...strategy.defaults, ...(v.params as Record<string, number | boolean>) })} className="chip num h-6">
                      v{v.version}{v.status === "retired" ? " (retired)" : ""}
                    </button>
                  ))}
                </div>
              )}

              {strategy && (
                <details className="group rounded-md border border-line bg-canvas/50">
                  <summary className="flex cursor-pointer items-center justify-between px-2.5 py-2 text-xs text-muted hover:text-fg">
                    Parameters <span className="text-subtle">a change makes a new version</span>
                  </summary>
                  <div className="flex flex-col gap-2 border-t border-line p-2.5">
                    {Object.entries(params).map(([k, v]) => (
                      <label key={k} className="flex items-center justify-between gap-2" title={strategy.param_help[k]}>
                        <span className={`num text-xs ${strategy.defaults[k] !== v ? "text-accent" : "text-muted"}`}>{k}</span>
                        {typeof v === "boolean" ? (
                          <input type="checkbox" checked={v} onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.checked }))} />
                        ) : (
                          <input type="number" step="any" value={v} onChange={(e) => setParams((p) => ({ ...p, [k]: Number(e.target.value) }))}
                            className="field field-num h-7 w-24" />
                        )}
                      </label>
                    ))}
                  </div>
                </details>
              )}

              <label>
                <span className="field-label">Universe</span>
                <select value={universe} onChange={(e) => setUniverse(e.target.value)} className="field">
                  {UNIVERSES.map((u) => <option key={u.id} value={u.id}>{u.label}</option>)}
                </select>
              </label>
              {universe === "custom" && (
                <textarea value={custom} onChange={(e) => setCustom(e.target.value)} rows={3} className="field num text-xs" aria-label="Custom symbols" />
              )}
              <div className="grid grid-cols-2 gap-2">
                <label>
                  <span className="field-label">Risk a trade (₹)</span>
                  <input type="number" value={risk} onChange={(e) => setRisk(Number(e.target.value))} className="field field-num" />
                </label>
                <label>
                  <span className="field-label">Slippage (bps)</span>
                  <input type="number" value={slippage} onChange={(e) => setSlippage(Number(e.target.value))} className="field field-num" />
                </label>
              </div>
              <button onClick={start} disabled={selected?.status === "running"} className="btn btn-primary btn-lg">
                {selected?.status === "running" ? "Running…" : "Run backtest"}
              </button>
              <p className="text-2xs leading-relaxed text-subtle">
                Data: Yahoo 5-minute bars (about 60 sessions) and 15-minute bars, kept and extended locally every run. Costs: Zerodha intraday charges.
                {strategyId.includes("option") || strategyId === "iron_fly_intraday" ? " Option strategies: prices are MODELED with Black-76 from India VIX (no free option history), in trading time; indicative only." : null}
                {universe === "INDEXFUT" && " Index futures: one lot per trade, futures charges, priced from the index until Kite data is connected (no volume, so VWAP filters are skipped)."}
              </p>
              {error && <Notice tone="error">{error}</Notice>}
            </div>
          </Panel>

          <Panel title="Past runs" flush>
            {runs.length === 0 ? <Empty title="No runs yet" /> : (
              <ul className="max-h-[420px] divide-y divide-line/70 overflow-y-auto">
                {runs.map((b) => (
                  <li key={b.id}>
                    <button onClick={() => open(b.id)} aria-current={selected?.id === b.id ? "true" : undefined}
                      className={`relative w-full cursor-pointer px-3 py-2 text-left transition-colors ${selected?.id === b.id ? "bg-raised" : "hover:bg-hover"}`}>
                      {selected?.id === b.id && <span className="absolute top-1.5 bottom-1.5 left-0 w-0.5 rounded-full bg-accent" />}
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="truncate text-[13px] font-medium">{b.strategy.name}</span>
                        {b.status === "done" ? <RValue v={b.expectancy_r} className="text-xs" /> : <StateBadge state={b.status} />}
                      </div>
                      <div className="num mt-0.5 truncate text-2xs text-subtle">
                        {b.universe} · {b.trades ?? 0} trades{b.net_pnl != null ? ` · ${inr(b.net_pnl)}` : ""} · v{b.strategy.version_key}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>

        <div className="min-w-0 flex-1">
          {!selected ? (
            <div className="panel"><Empty title="No run selected" icon={<IconHistory size={16} />}>Run a backtest, or open one of your past runs.</Empty></div>
          ) : selected.status === "running" ? (
            <div className="panel space-y-3 p-4">
              <p className="text-[13px]">Running <span className="font-medium">{selected.strategy.name}</span> on <span className="num">{selected.progress.total}</span> {selected.progress.total === 1 ? "symbol" : "symbols"}…</p>
              <div className="h-1 overflow-hidden rounded-full bg-raised"><div className="h-full rounded-full bg-accent transition-[width] duration-500" style={{ width: `${selected.progress.total ? (selected.progress.done / selected.progress.total) * 100 : 0}%` }} /></div>
              <p className="num text-xs text-muted">{selected.progress.done} / {selected.progress.total} done</p>
            </div>
          ) : selected.status === "failed" ? (
            <Notice tone="error" title="The backtest failed">{selected.errors.join("; ")}</Notice>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 className="text-[16px] font-semibold tracking-tight">{selected.strategy.name}</h2>
                <span className="num text-xs text-muted">v{selected.strategy.version_key} · {selected.universe} ({selected.symbols.length} {selected.symbols.length === 1 ? "symbol" : "symbols"})</span>
              </div>
              <BacktestReport bt={selected} />
              {selected.errors.length > 0 && <p className="text-xs text-subtle">Skipped: {selected.errors.join("; ")}</p>}
            </div>
          )}
        </div>
      </div>
    </Page>
  );
}
