"use client";

import { useEffect, useState } from "react";

import { AutoSignals } from "@/components/auto-signals";
import { SignalCard } from "@/components/signal-card";
import { IconRefresh, IconSearch } from "@/components/ui/icons";
import { DataSourceBadge, Empty, Notice, Page, PageHeader, SkeletonRows } from "@/components/ui/kit";
import { TvInbox } from "@/components/tv-inbox";
import { api, type LabData, type ScanResult, type StrategyInfo, type StrategyVersion } from "@/lib/api";

const UNIVERSES = [
  { id: "NIFTY50", label: "Nifty 50" },
  { id: "NIFTY100", label: "Nifty 100" },
  { id: "NIFTYBANK", label: "Nifty Bank" },
  { id: "INDEXFUT", label: "Index futures (Nifty, Bank Nifty)" },
];

export default function SignalsPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [strategy, setStrategy] = useState("orb");
  const [universe, setUniverse] = useState("NIFTY50");
  const [at, setAt] = useState("");
  const [versions, setVersions] = useState<StrategyVersion[]>([]);
  const [version, setVersion] = useState("");
  const [result, setResult] = useState<ScanResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"auto" | "one">("auto");

  useEffect(() => {
    api<StrategyInfo[]>("/strategies").then(setStrategies).catch((e: Error) => setError(e.message));
    api<LabData>("/lab").then((d) => setVersions(d.variants.filter((v) => v.user_id && v.status !== "retired"))).catch(() => {});   // signed out: defaults only
  }, []);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const q = new URLSearchParams({ strategy, universe });
      if (at) q.set("at", at);
      if (version) q.set("version", version);
      setResult(await api<ScanResult>(`/signals/scan?${q}`));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const current = strategies.find((s) => s.id === strategy);
  return (
    <Page wide>
      <PageHeader
        title="Signals"
        subtitle="Every signal says why: the rule with its numbers, the plan, the strategy's record after costs, similar past setups and the risks."
        actions={
          <div className="seg" role="group" aria-label="Scan mode">
            <button aria-pressed={mode === "auto"} onClick={() => setMode("auto")}>All strategies</button>
            <button aria-pressed={mode === "one"} onClick={() => setMode("one")}>One strategy</button>
          </div>
        }
      />

      {mode === "auto" ? (
        <div className="grid gap-4 2xl:grid-cols-12">
          <div className="min-w-0 2xl:col-span-9"><AutoSignals /></div>
          <div className="flex min-w-0 flex-col gap-4 2xl:col-span-3"><TvInbox /></div>
        </div>
      ) : (<>

      <div className="panel flex flex-wrap items-end gap-3 p-3">
        <label className="min-w-52 flex-1 sm:flex-none">
          <span className="field-label">Strategy</span>
          <select value={strategy} onChange={(e) => { setStrategy(e.target.value); setVersion(""); }} className="field">
            {strategies.filter((s) => s.market !== "index_options").map((s) => <option key={s.id} value={s.id}>{s.name} ({s.timeframe})</option>)}
          </select>
        </label>
        {versions.some((v) => v.strategy_id === strategy) && (
          <label className="min-w-44 flex-1 sm:flex-none">
            <span className="field-label">Version</span>
            <select value={version} onChange={(e) => setVersion(e.target.value)} className="field">
              <option value="">default parameters</option>
              {versions.filter((v) => v.strategy_id === strategy).map((v) => <option key={v.id} value={v.version}>v{v.version} (lab, {v.status})</option>)}
            </select>
          </label>
        )}
        <label className="min-w-44 flex-1 sm:flex-none">
          <span className="field-label">Universe</span>
          <select value={universe} onChange={(e) => setUniverse(e.target.value)} className="field">
            {UNIVERSES.map((u) => <option key={u.id} value={u.id}>{u.label}</option>)}
          </select>
        </label>
        <label className="w-36">
          <span className="field-label">Replay today at <span className="text-subtle">(IST)</span></span>
          <input type="time" value={at} onChange={(e) => setAt(e.target.value)} className="field num" />
        </label>
        <button onClick={run} disabled={busy} className="btn btn-primary min-w-28">
          {busy ? <><IconRefresh size={14} className="animate-spin" />Scanning…</> : <><IconSearch size={14} />Scan now</>}
        </button>
        {current && <p className="w-full text-xs text-muted">{current.description}</p>}
      </div>

      {error && <Notice tone="error" title="Scan failed">{error}</Notice>}

      <div className="grid gap-4 xl:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-3 xl:col-span-8">
          {busy && !result && <div className="panel"><SkeletonRows rows={6} /></div>}
          {!busy && !result && (
            <div className="panel">
              <Empty title="Run a scan" icon={<IconSearch size={16} />}>Pick a strategy and a universe, then scan. During market hours it reads the live session; after the close it reads the last one.</Empty>
            </div>
          )}
          {result && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="badge badge-neutral h-6 normal-case">
                  {result.replay ? "Replay of " : result.market_open_now ? "Live · " : "Last session · "}
                  {new Date(result.as_of).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" })} IST
                </span>
                <span className="badge badge-neutral h-6 normal-case num">{result.scanned} {universe === "INDEXFUT" ? "contracts" : "stocks"} scanned</span>
                <DataSourceBadge source={result.data_source} />
                {result.regime.vix && <span className="badge badge-neutral h-6 normal-case">{result.regime.vix}, yesterday <span className="num">{result.regime.vix_prev_close?.toFixed(2)}</span></span>}
                {result.regime.nifty_gap && <span className="badge badge-neutral h-6 normal-case">{result.regime.nifty_gap} (<span className="num">{result.regime.nifty_gap_pct?.toFixed(2)}%</span>)</span>}
                <span className="ml-auto text-muted">{result.signals.length} signal{result.signals.length === 1 ? "" : "s"}</span>
              </div>
              {result.signals.length === 0 ? (
                <div className="panel"><Empty title="No signals in this session so far">The rule did not fire on any stock in this universe.</Empty></div>
              ) : (
                <div className="flex flex-col gap-3">
                  {result.signals.map((s) => <SignalCard key={`${s.symbol}-${s.created_at}`} s={s} />)}
                </div>
              )}
              {result.errors.length > 0 && <p className="text-xs text-subtle">Skipped: {result.errors.join("; ")}</p>}
            </>
          )}
        </div>
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-4">
          <TvInbox />
        </div>
      </div>
      </>)}
    </Page>
  );
}
