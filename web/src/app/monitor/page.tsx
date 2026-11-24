"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { PriceChart, type ChartLevel, type ChartMarker } from "@/components/price-chart";
import { useFeedback } from "@/components/ui/feedback";
import { IconAlert, IconBell, IconPower, IconRadio } from "@/components/ui/icons";
import {
  Badge, Check, DataSourceBadge, Empty, Notice, Page, PageHeader, PageLoading, Panel, Pnl, RValue, Side, SignInRequired, Skeleton, cx, px,
} from "@/components/ui/kit";
import { api, inr, type ChartData, type MonitorHealth, type MonitorSummary, type MonitorTrade } from "@/lib/api";
import { C } from "@/lib/theme";
import { supabase } from "@/lib/supabase";

const HEALTH_TONE: Record<MonitorHealth, "up" | "warn" | "down" | "live" | "neutral"> = {
  "on track": "up", watch: "warn", "at risk": "down", "thesis broken": "live", "no data": "neutral", closed: "neutral",
};
const INDICATORS = "ema:20,supertrend:10:3,or:15,vwap";
const when = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** Stop ←→ target track with the entry, the price now, and the best / worst reached. */
function RTrack({ m }: { m: MonitorTrade }) {
  const { stop, target, entry } = m.levels;
  const p = m.progress;
  if (stop == null || target == null || !p) return null;
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

export default function MonitorPage() {
  const sb = supabase();
  const { toast, confirm } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [list, setList] = useState<MonitorSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [m, setM] = useState<MonitorTrade | null>(null);
  const [chart, setChart] = useState<ChartData | null>(null);
  const [tf, setTf] = useState<"1m" | "5m">("1m");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    sb?.auth.getSession().then(({ data }) => setSession(data.session));
    const q = new URLSearchParams(window.location.search).get("trade");
    if (q) queueMicrotask(() => setSelected(q));
  }, [sb]);

  // the open trades, with health
  useEffect(() => {
    if (!session) return;
    let stop = false;
    const load = () => api<MonitorSummary[]>("/monitor").then((l) => {
      if (stop) return;
      setList(l);
      setSelected((cur) => cur ?? l[0]?.trade_id ?? null);
    }).catch((e: Error) => !stop && setError(e.message));
    load();
    const id = setInterval(load, 20_000);
    return () => { stop = true; clearInterval(id); };
  }, [session]);

  // the selected trade's analysis, every 10 s
  const loadOne = useCallback(async (id: string) => {
    try { setM(await api<MonitorTrade>(`/monitor/${id}`)); setError(null); }
    catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => {
    if (!session || !selected) return;
    let stop = false;
    const run = () => { if (!stop) loadOne(selected); };
    run();
    const id = setInterval(run, 10_000);
    window.history.replaceState(null, "", `/monitor?trade=${selected}`);
    return () => { stop = true; clearInterval(id); };
  }, [session, selected, loadOne]);

  // the live chart of the underlying, every 30 s
  const underlying = m?.underlying;
  useEffect(() => {
    if (!underlying) return;
    let stop = false;
    const load = () => api<ChartData>(`/market/chart?symbol=${encodeURIComponent(underlying)}&tf=${tf}&ind=${encodeURIComponent(INDICATORS)}`)
      .then((d) => !stop && setChart(d)).catch(() => {});
    load();
    const id = setInterval(load, 30_000);
    return () => { stop = true; clearInterval(id); };
  }, [underlying, tf]);

  const levels: ChartLevel[] = useMemo(() => {
    if (!m) return [];
    const out: ChartLevel[] = [{ price: m.levels.entry, label: "entry", color: C.info, width: 2, keepInView: true }];
    const moved = m.levels.stop != null && m.levels.initial_stop != null && m.levels.stop !== m.levels.initial_stop;
    if (m.levels.stop != null) out.push({ price: m.levels.stop, label: moved ? "stop (moved)" : "stop", color: C.down, width: 2, keepInView: true });
    if (moved) out.push({ price: m.levels.initial_stop!, label: "first stop", color: C.down, style: "dotted" });
    if (m.management?.partial) out.push({ price: m.management.partial.price, label: `partial ${m.management.partial.qty}`, color: C.up, style: "dashed" });
    if (m.levels.target != null) out.push({ price: m.levels.target, label: "target", color: C.up, width: 2, keepInView: true });
    for (const lv of m.sr) out.push({ price: lv.price, label: lv.label, color: lv.kind === "support" ? "#5d7a8c" : "#8c6f5d", style: "dashed" });
    return out;
  }, [m]);
  const markers: ChartMarker[] = useMemo(() => {
    if (!m) return [];
    const long = m.direction === "long";
    const out: ChartMarker[] = [{
      t: m.levels.entry_t, text: `entry ${px(m.levels.entry)}`, color: C.info,
      position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
    }];
    // a stop move takes effect after the bar that triggered it: mark the bar it starts on
    for (const mv of m.management?.moves ?? []) out.push({ t: mv.t, text: `stop ${px(mv.to)}`, color: C.down, position: long ? "belowBar" : "aboveBar", shape: "circle" });
    const pt = m.management?.partial;
    if (pt) out.push({ t: pt.t, text: `partial ${pt.qty} @ ${px(pt.price)}`, color: C.up, position: long ? "aboveBar" : "belowBar", shape: "circle" });
    return out.sort((a, b) => a.t - b.t);
  }, [m]);

  async function close() {
    if (!m) return;
    const live = m.trade.mode === "live";
    if (!(await confirm({ title: `Close ${m.trade.symbol}?`, tone: live ? "live" : "primary", confirm: live ? "Close at Zerodha" : "Close position",
      body: live ? "Its stop order at Zerodha becomes a marketable exit." : "A paper exit fills at the next price. Exits are never blocked." }))) return;
    try {
      await api(live ? `/live/positions/${m.trade.id}/close` : `/paper/positions/${m.trade.id}/close`, { method: "POST" });
      toast("success", `Closing ${m.trade.symbol}`);
    } catch (e) { toast("error", "Could not close", (e as Error).message); }
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="The trade monitor" />;

  const failing = m?.checks.filter((c) => !c.ok) ?? [];
  const passing = m?.checks.filter((c) => c.ok) ?? [];
  return (
    <Page wide>
      <PageHeader title="Trade monitor"
        subtitle="Each open trade on its live chart, re-read every 10 seconds against the strategy that took it. Alerts appear on any page the moment something turns against the trade."
        actions={<NotifyToggle />} />
      {error && <Notice tone="error">{error}</Notice>}

      {list && list.length === 0 && (
        <div className="panel"><Empty title="No open trades" icon={<IconRadio size={16} />} action={<Link href="/signals" className="btn btn-sm">Find a signal</Link>}>
          Positions appear here as soon as a paper or live order fills.
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
              {t.mode === "live" && <Badge tone="live">live</Badge>}
              <span className="text-2xs text-muted">{t.health}</span>
              {t.r != null && <RValue v={t.r} className="text-xs" />}
            </button>
          ))}
        </div>
      )}

      {selected && (
        <div className="grid gap-4 xl:grid-cols-12">
          <Panel className="xl:col-span-8" flush
            title={<div className="flex flex-wrap items-baseline gap-2">
              <h2 className="panel-title num">{m?.underlying ?? "…"}</h2>
              {m?.progress && <span className="num text-[15px]">{px(m.progress.price)}</span>}
              <span className="text-2xs text-subtle">entry, stop, target and nearby support / resistance · EMA 20, Supertrend, opening range{chart?.notes.length ? "" : ", VWAP"}</span>
            </div>}
            actions={<div className="seg" role="group" aria-label="Timeframe">{(["1m", "5m"] as const).map((x) => <button key={x} aria-pressed={tf === x} onClick={() => setTf(x)} className="num">{x}</button>)}</div>}>
            <div className="h-[62vh] min-h-[420px]">
              {chart && m ? <PriceChart data={chart} levels={levels} markers={markers} /> : <div className="p-3"><Skeleton className="h-[58vh]" /></div>}
            </div>
          </Panel>

          <div className="flex min-w-0 flex-col gap-4 xl:col-span-4">
            <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Live read</h2>{m && <Badge tone={HEALTH_TONE[m.health]}>{m.health}</Badge>}</div>}
              actions={m && <span className="flex items-center gap-2 text-2xs text-subtle"><DataSourceBadge source={m.data_source} /><span className="num">{when(m.as_of)}</span></span>}>
              {!m ? <div className="space-y-2"><Skeleton className="h-4 w-3/4" /><Skeleton className="h-2" /><Skeleton className="h-16" /></div> : (
                <div className="space-y-3">
                  <p className={cx("text-[13px] font-medium", m.health === "on track" ? "text-up" : m.health === "watch" ? "text-warn" : m.health === "no data" ? "text-muted" : "text-down")}>{m.headline}</p>
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Side side={m.direction === "long" ? "buy" : "sell"} />
                    <span className="num font-medium">{m.trade.symbol}</span>
                    <StrategyLabel m={m} onLinked={() => loadOne(m.trade.id)} />
                  </div>
                  <RTrack m={m} />
                  {m.progress && (
                    <dl className="grid grid-cols-3 gap-px overflow-hidden rounded-md border border-line bg-line text-xs">
                      {([["Now", <RValue key="n" v={m.progress.r} />], ["Best", <RValue key="b" v={m.progress.mfe_r} />], ["Worst", <RValue key="w" v={m.progress.mae_r} />],
                        ["In trade", `${m.time?.minutes_in ?? 0} min`], ["To 15:15", `${m.time?.minutes_to_flat ?? 0} min`],
                        ["Usual win", m.expected?.median_minutes_to_target ? `${m.expected.median_minutes_to_target} min` : "–"]] as [string, React.ReactNode][]).map(([k, v]) => (
                        <div key={k} className="bg-surface px-2.5 py-1.5"><dt className="eyebrow">{k}</dt><dd className="num mt-0.5 text-[13px]">{v}</dd></div>
                      ))}
                    </dl>
                  )}
                  {m.expected?.exit_mix && (
                    <p className="text-xs text-muted">
                      In this strategy&apos;s backtest ({m.expected.trades} trades): <span className="num text-fg">{Math.round(m.expected.exit_mix.target * 100)}%</span> reached target,{" "}
                      <span className="num text-fg">{Math.round(m.expected.exit_mix.stop * 100)}%</span> hit the stop{m.expected.median_minutes_to_stop ? ` (usually after ${m.expected.median_minutes_to_stop} min)` : ""},{" "}
                      <span className="num text-fg">{Math.round(m.expected.exit_mix.time * 100)}%</span> ended at 15:15.
                      {m.expected.exit_mix.target === 0 && <span className="text-warn"> The target has never been reached: most of the outcome is decided by the stop or the 15:15 exit.</span>}
                    </p>
                  )}
                  {m.management && (
                    <div className="rounded-md border border-line bg-canvas/60 p-2.5 text-xs">
                      <div className="flex items-center justify-between"><span className="eyebrow">Trade management</span>
                        {m.progress?.stop_r != null && <span className="num">stop locks in <RValue v={m.progress.stop_r} /></span>}</div>
                      {m.management.description && <p className="mt-1 text-muted">Fixed before entry: {m.management.description}.</p>}
                      {m.management.moves.length > 0 && (
                        <ul className="num mt-1 space-y-0.5 text-muted">
                          {m.management.moves.slice(-6).map((mv) => (
                            <li key={`${mv.t}-${mv.to}`}><span className="text-subtle">{when(new Date(mv.t * 1000).toISOString())}</span> stop {px(mv.from)} → <span className="text-fg">{px(mv.to)}</span> · {mv.why}</li>
                          ))}
                          {m.management.moves.length > 6 && <li className="text-subtle">and {m.management.moves.length - 6} earlier moves</li>}
                        </ul>
                      )}
                      {m.management.partial && <p className="num mt-1 text-up">Booked {m.management.partial.qty} at {px(m.management.partial.price)} (+{m.management.partial.at_r}R){m.management.open_qty != null ? `; ${m.management.open_qty} still open` : ""}.</p>}
                      {m.management.notes.map((n) => <p key={n} className="mt-1 text-2xs text-subtle">{n}</p>)}
                    </div>
                  )}
                  {m.option && (
                    <div className="rounded-md border border-line bg-canvas/60 p-2.5 text-xs">
                      <div className="flex items-center justify-between"><span className="eyebrow">Option</span><span className="num">P&amp;L <Pnl v={m.option.pnl} /></span></div>
                      {m.option.legs.map((lg) => (
                        <p key={lg.contract} className="num mt-1 text-muted">
                          <span className="text-fg">{lg.contract}</span> · in {px(lg.entry)} · bid {px(lg.bid)} · Δ {lg.delta ?? "–"} · θ {lg.theta ?? "–"}/day · IV {lg.iv ?? "–"}%
                        </p>
                      ))}
                      <p className="num mt-1 text-2xs text-subtle">exits at {m.option.stop_pnl != null ? inr(m.option.stop_pnl) : "–"} / {m.option.target_pnl != null ? inr(m.option.target_pnl) : "–"} or the levels above · {m.option.source === "kite" ? "Kite" : "NSE"} {m.option.as_of}</p>
                    </div>
                  )}
                  {!m.trade.exit_ts && <button onClick={close} className="btn btn-sm w-full"><IconPower size={13} />Close this position</button>}
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
              {!m?.alerts.length ? <p className="text-xs text-muted">None yet. You get one the moment a check turns against the trade, its health gets worse, its stop moves or a partial exit fills.</p> : (
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
              <Panel title="Support and resistance" flush>
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
    </Page>
  );
}

function NotifyToggle() {
  const [perm, setPerm] = useState<string>(() => (typeof Notification === "undefined" ? "unsupported" : Notification.permission));
  if (perm === "unsupported") return null;
  if (perm === "granted") return <span className="badge badge-up h-6">Desktop alerts on</span>;
  return (
    <button className="btn btn-sm" onClick={() => Notification.requestPermission().then(setPerm)} disabled={perm === "denied"}
      title={perm === "denied" ? "Blocked in your browser settings" : "Also show alerts as desktop notifications"}>
      <IconBell size={13} />{perm === "denied" ? "Desktop alerts blocked" : "Turn on desktop alerts"}
    </button>
  );
}


const STRATEGY_NAMES: Record<string, string> = { orb: "Opening-range breakout", vwap_pullback: "VWAP pullback", supertrend: "Supertrend flip", pdh_pdl: "Previous-day high/low breakout" };

/** Which strategy took this trade, and how the app knows; a picker when it could not tell. */
function StrategyLabel({ m, onLinked }: { m: MonitorTrade; onLinked: () => void }) {
  const { toast } = useFeedback();
  const [busy, setBusy] = useState(false);
  const st = m.trade.strategy, link = m.trade.strategy_link;
  if (st) {
    const how = link?.by === "matched" ? `matched to its ${link.signal_time ? new Date(link.signal_time).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" }) + " " : ""}signal by ${link.on}`
      : link?.by === "you" ? "chosen by you" : "from its signal";
    return <span className="text-muted"><span className="text-fg">{STRATEGY_NAMES[st] ?? st}</span> · {how}</span>;
  }
  async function choose(id: string) {
    if (!id) return;
    setBusy(true);
    try { await api(`/monitor/${m.trade.id}/strategy`, { method: "POST", body: { strategy: id } }); toast("success", "Strategy linked"); onLinked(); }
    catch (e) { toast("error", "Could not link", (e as Error).message); } finally { setBusy(false); }
  }
  return (
    <span className="flex flex-wrap items-center gap-2 text-muted">
      <span title="No signal on today's chart has this trade's direction, stop and target, so the strategy-specific checks are off">no strategy found: general checks only</span>
      <select disabled={busy} defaultValue="" onChange={(e) => choose(e.target.value)} className="field h-7 w-auto text-xs" aria-label="Link a strategy">
        <option value="">Link a strategy…</option>
        {Object.entries(STRATEGY_NAMES).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
      </select>
    </span>
  );
}
