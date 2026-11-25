"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { CLOSE_BODY, closeToast } from "@/components/global-paper";
import { PriceChart, type ChartLevel, type ChartMarker } from "@/components/price-chart";
import { useFeedback } from "@/components/ui/feedback";
import { IconAlert, IconBell, IconPower, IconRadio } from "@/components/ui/icons";
import {
  Badge, Check, Empty, Notice, PageHeader, PageLoading, Panel, RValue, Side, SignInRequired, Skeleton, cx, px,
} from "@/components/ui/kit";
import { api, type GlobalChartData, type GlobalMonitorSummary, type GlobalOrder, type GlobalMonitorTrade, type MonitorHealth } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import { C } from "@/lib/theme";

const HEALTH_TONE: Record<MonitorHealth, "up" | "warn" | "down" | "live" | "neutral"> = {
  "on track": "up", watch: "warn", "at risk": "down", "thesis broken": "live", "no data": "neutral", closed: "neutral",
};
const INDICATORS = "ema:20,asia,pdl";
const when = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const usd = (v: number | null | undefined) => v == null ? "–" : `${v < 0 ? "−" : "+"}$${Math.abs(v).toFixed(2)}`;

/** Stop ←→ target track with the entry and the price now. */
function RTrack({ m }: { m: GlobalMonitorTrade }) {
  const { stop, target, entry } = m.levels;
  const p = m.progress;
  if (target == null || !p) return null;
  const lo = Math.min(stop, target), hi = Math.max(stop, target);
  const pos = (x: number) => `${Math.min(100, Math.max(0, ((x - lo) / (hi - lo)) * 100))}%`;
  const long = m.direction === "long";
  return (
    <div className="space-y-1.5">
      <div className="relative h-2 rounded-full" style={{ background: `linear-gradient(${long ? "90deg" : "270deg"}, ${C.down}55 0%, ${C.line} ${pos(entry)}, ${C.up}55 100%)` }}>
        <span className="absolute top-1/2 h-4 w-px -translate-y-1/2 bg-fg/60" style={{ left: pos(entry) }} title={`entry ${px(entry)}`} />
        <span className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-canvas bg-fg shadow" style={{ left: pos(p.price) }} title={`now ${px(p.price)}`} />
      </div>
      <div className="num flex justify-between text-2xs">
        <span className={long ? "text-down" : "text-up"}>{long ? "stop" : "target"} {px(lo)}</span>
        <span className="text-subtle">entry {px(entry)}</span>
        <span className={long ? "text-up" : "text-down"}>{long ? "target" : "stop"} {px(hi)}</span>
      </div>
    </div>
  );
}

export function GlobalMonitor() {
  const sb = supabase();
  const { toast, confirm } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [list, setList] = useState<GlobalMonitorSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [m, setM] = useState<GlobalMonitorTrade | null>(null);
  const [chart, setChart] = useState<GlobalChartData | null>(null);
  const [tf, setTf] = useState<"1m" | "5m">("1m");
  const [error, setError] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    sb?.auth.getSession().then(({ data }) => setSession(data.session));
    const q = new URLSearchParams(window.location.search).get("trade");
    if (q) queueMicrotask(() => setSelected(q));
  }, [sb]);

  useEffect(() => {
    if (!session) return;
    let stop = false;
    const load = () => api<GlobalMonitorSummary[]>("/global/monitor").then((l) => {
      if (stop) return;
      setList(l);
      setSelected((cur) => cur ?? l[0]?.trade_id ?? null);
    }).catch((e: Error) => !stop && setError(e.message));
    load();
    const id = setInterval(load, 20_000);
    return () => { stop = true; clearInterval(id); };
  }, [session]);

  const loadOne = useCallback(async (id: string) => {
    try { setM(await api<GlobalMonitorTrade>(`/global/monitor/${id}`)); setError(null); }
    catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => {
    if (!session || !selected) return;
    let stop = false;
    const run = () => { if (!stop) loadOne(selected); };
    run();
    const id = setInterval(run, 10_000);
    window.history.replaceState(null, "", `/global/monitor?trade=${selected}`);
    return () => { stop = true; clearInterval(id); };
  }, [session, selected, loadOne]);

  const source = m?.trade.price_source;
  useEffect(() => {
    if (!source) return;
    let stop = false;
    const load = () => api<GlobalChartData>(`/global/chart?symbol=XAUUSD&tf=${tf}&source=${source}&ind=${encodeURIComponent(INDICATORS)}`)
      .then((d) => !stop && setChart(d)).catch(() => {});
    load();
    const id = setInterval(load, 30_000);
    return () => { stop = true; clearInterval(id); };
  }, [source, tf]);

  const levels: ChartLevel[] = useMemo(() => {
    if (!m) return [];
    const out: ChartLevel[] = [
      { price: m.levels.entry, label: "entry", color: C.info, width: 2, keepInView: true },
      { price: m.levels.stop, label: "stop", color: C.down, width: 2, keepInView: true },
    ];
    if (m.levels.target != null) out.push({ price: m.levels.target, label: "target", color: C.up, width: 2, keepInView: true });
    for (const lv of m.sr) out.push({ price: lv.price, label: lv.label, color: lv.kind === "support" ? "#5d7a8c" : "#8c6f5d", style: "dashed" });
    return out;
  }, [m]);
  const markers: ChartMarker[] = useMemo(() => {
    if (!m) return [];
    const long = m.direction === "long";
    return [{ t: m.levels.entry_t, text: `entry ${px(m.levels.entry)}`, color: C.info, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown" }];
  }, [m]);

  async function close() {
    if (!m) return;
    const demo = m.trade.mode === "demo";
    if (!(await confirm({ title: `Close ${m.trade.symbol} ${m.trade.side}?`, tone: "primary", confirm: demo ? "Close at MT5 demo" : "Close position",
      body: demo ? "Closes at your MT5 demo account at the market, now." : CLOSE_BODY }))) return;
    setClosing(true);
    try {
      const o = await api<GlobalOrder>(`/global/paper/positions/${m.trade.id}/close`, { method: "POST" });
      toast("success", ...closeToast(o, m.trade.symbol));
      await loadOne(m.trade.id);
    } catch (e) { toast("error", "Could not close", (e as Error).message); }
    finally { setClosing(false); }
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="The XAUUSD trade monitor" />;

  const failing = m?.checks.filter((c) => !c.ok) ?? [];
  const passing = m?.checks.filter((c) => c.ok) ?? [];
  return (
    <>
      <PageHeader title="XAUUSD trade monitor"
        subtitle="Each open gold trade, paper or MT5 demo, on its live chart, re-read every 10 seconds. Alerts go to Telegram too, even with this page closed." />
      {error && <Notice tone="error">{error}</Notice>}

      {list && list.length === 0 && (
        <div className="panel"><Empty title="No open gold trades" icon={<IconRadio size={16} />} action={<Link href="/global/agent" className="btn btn-sm">XAUUSD agent</Link>}>
          Trades appear here as soon as a paper or MT5 demo order fills.
        </Empty></div>
      )}

      {!!list?.length && (
        <div className="flex flex-wrap gap-2">
          {list.map((t) => (
            <button key={t.trade_id} onClick={() => { setSelected(t.trade_id); setM(null); setChart(null); }} aria-pressed={selected === t.trade_id}
              className={cx("flex items-center gap-2 rounded-md border px-3 py-2 text-left transition-colors",
                selected === t.trade_id ? "border-accent/60 bg-accent/[0.07]" : "border-line bg-surface hover:bg-hover")}>
              <span className={cx("size-2 rounded-full", { up: "bg-up", warn: "bg-warn", down: "bg-down", live: "bg-live", neutral: "bg-subtle" }[HEALTH_TONE[t.health]])} />
              <span className="num text-[13px] font-medium">{t.symbol}</span>
              <Side side={t.side} />
              {t.mode === "demo" && <Badge tone="info">MT5 demo</Badge>}
              <span className="text-2xs text-muted">{t.health}</span>
              {t.r != null && <RValue v={t.r} className="text-xs" />}
            </button>
          ))}
        </div>
      )}

      {selected && (
        <div className="mt-4 grid gap-4 xl:grid-cols-12">
          <Panel className="xl:col-span-8" flush
            title={<div className="flex flex-wrap items-baseline gap-2">
              <h2 className="panel-title num">XAUUSD</h2>
              {m?.progress && <span className="num text-[15px]">{px(m.progress.price)}</span>}
              <span className="text-2xs text-subtle">entry, stop, target and key levels · EMA 20, Asian range, yesterday&apos;s levels · {source === "futures" ? "GC=F futures" : "spot"}</span>
            </div>}
            actions={<div className="seg" role="group" aria-label="Timeframe">{(["1m", "5m"] as const).map((x) => <button key={x} aria-pressed={tf === x} onClick={() => setTf(x)} className="num">{x}</button>)}</div>}>
            <div className="h-[62vh] min-h-[420px]">
              {chart && m ? <PriceChart data={chart} levels={levels} markers={markers} /> : <div className="p-3"><Skeleton className="h-[58vh]" /></div>}
            </div>
          </Panel>

          <div className="flex min-w-0 flex-col gap-4 xl:col-span-4">
            <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Live read</h2>{m && <Badge tone={HEALTH_TONE[m.health]}>{m.health}</Badge>}</div>}
              actions={m && <span className="num text-2xs text-subtle">{when(m.as_of)}</span>}>
              {!m ? <div className="space-y-2"><Skeleton className="h-4 w-3/4" /><Skeleton className="h-2" /><Skeleton className="h-16" /></div> : (
                <div className="space-y-3">
                  <p className={cx("text-[13px] font-medium", m.health === "on track" ? "text-up" : m.health === "watch" ? "text-warn" : m.health === "no data" ? "text-muted" : "text-down")}>{m.headline}</p>
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Side side={m.trade.side} />
                    <span className="num font-medium">{m.trade.lots} lots</span>
                    {m.trade.mode === "demo" && <Badge tone="info">MT5 demo</Badge>}
                    <span className="text-muted">{m.trade.strategy_name ?? "no strategy linked: general checks only"}</span>
                  </div>
                  <RTrack m={m} />
                  {m.progress && (
                    <dl className="grid grid-cols-3 gap-px overflow-hidden rounded-md border border-line bg-line text-xs">
                      {([["Now", <RValue key="n" v={m.progress.r} />], ["Best", <RValue key="b" v={m.progress.mfe_r} />], ["Worst", <RValue key="w" v={m.progress.mae_r} />],
                        ["Unrealized", <span key="u"><span className={(m.unrealized ?? 0) >= 0 ? "text-up" : "text-down"}>{usd(m.unrealized)}</span>
                          {m.progress.price_age_seconds != null && <span className="block text-2xs text-subtle">price {m.progress.price_age_seconds < 90 ? `${m.progress.price_age_seconds} s` : `${Math.round(m.progress.price_age_seconds / 60)} min`} old</span>}</span>],
                        ["In trade", `${m.time?.minutes_in ?? 0} min`], ["To flat-by", m.time?.minutes_to_flat != null ? `${m.time.minutes_to_flat} min` : "–"]] as [string, React.ReactNode][]).map(([k, v]) => (
                        <div key={k} className="bg-surface px-2.5 py-1.5"><dt className="eyebrow">{k}</dt><dd className="num mt-0.5 text-[13px]">{v}</dd></div>
                      ))}
                    </dl>
                  )}
                  {m.agent_wrong_if && (
                    <div className="rounded-md border border-line bg-canvas/60 p-2.5 text-xs">
                      <span className="eyebrow">The agent said it is wrong if</span>
                      <p className="mt-1 text-fg">{m.agent_wrong_if}</p>
                      <p className="mt-1 text-2xs text-subtle">Code cannot check the agent&apos;s own words: compare them with the chart yourself.</p>
                    </div>
                  )}
                  {!m.trade.exit_ts && (
                    <button onClick={close} disabled={closing || m.trade.closing} className="btn btn-sm w-full">
                      <IconPower size={13} />{closing ? "Closing…" : m.trade.closing ? "Closing at the next 1-minute bar…" : "Close this position"}
                    </button>)}
                </div>
              )}
            </Panel>

            <Panel title="Is it following the plan?">
              {!m ? <Skeleton className="h-32" /> : !m.checks.length ? <p className="text-xs text-muted">{m.headline}</p> : (
                <ul className="space-y-2 text-[12.5px]">
                  {[...failing, ...passing].map((c) => (
                    <Check key={c.key} ok={c.ok}>
                      <span className={cx("font-medium", c.ok ? "text-fg" : c.severity === "high" ? "text-down" : "text-warn")}>{c.name}</span>
                      <span className="block text-xs text-muted">{c.detail}</span>
                    </Check>
                  ))}
                </ul>
              )}
            </Panel>

            <Panel title={<div className="flex items-center gap-2"><IconBell size={14} className="text-subtle" /><h2 className="panel-title">Alerts</h2>{!!m?.alerts.length && <Badge tone="warn">{m.alerts.length}</Badge>}</div>}>
              {!m?.alerts.length ? <p className="text-xs text-muted">None yet. You get one, here and on Telegram, the moment a check turns against the trade or its health gets worse.</p> : (
                <ul className="space-y-2">
                  {[...m.alerts].reverse().map((a) => (
                    <li key={a.id} className="flex gap-2 text-xs">
                      <IconAlert size={13} className={cx("mt-0.5 shrink-0", a.level === "high" ? "text-down" : "text-warn")} />
                      <div><p className="text-fg"><span className="num text-subtle">{when(a.at)}</span> {a.title}</p><p className="text-muted">{a.detail}</p></div>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>

            {m && m.sr.length > 0 && (
              <Panel title="Key levels" flush>
                <table className="dtable dtable-compact">
                  <tbody>
                    {[...m.sr].sort((a, b) => b.price - a.price).map((lv) => (
                      <tr key={lv.price}><td className={lv.kind === "resistance" ? "text-[#c79a7d]" : "text-[#7da3b8]"}>{lv.kind}</td><td>{lv.label}</td><td className="r">{px(lv.price)}</td></tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            )}
          </div>
        </div>
      )}
    </>
  );
}
