"use client";

import { useCallback, useEffect, useState } from "react";

import { PriceChart } from "@/components/price-chart";
import { Badge, Notice, Panel, Stat, StatStrip } from "@/components/ui/kit";
import { api, type GlobalChartData, type GlobalDataCheck, type GlobalDataStatus, type GlobalMarketStatus } from "@/lib/api";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "1d"] as const;
type Tf = (typeof TIMEFRAMES)[number];
type Source = "spot" | "futures";

const INDICATORS: { id: string; label: string; help: string }[] = [
  { id: "ema:20", label: "EMA 20", help: "Exponential moving average, 20 bars: the short-term trend" },
  { id: "ema:50", label: "EMA 50", help: "Exponential moving average, 50 bars: the medium-term trend" },
  { id: "vwap", label: "VWAP", help: "Volume-weighted average price, reset each trading day at 17:00 New York" },
  { id: "supertrend:10:3", label: "Supertrend", help: "ATR(10) x 3 trailing line: green below price in an uptrend, red above in a downtrend" },
  { id: "bb:20:2", label: "Bollinger", help: "20-bar average with bands 2 standard deviations away" },
  { id: "asia", label: "Asia range", help: "High and low of the Asian session, drawn once London opens" },
  { id: "pdl", label: "Prev day H/L/C", help: "The last trading day's high, low and close" },
  { id: "rsi:14", label: "RSI 14", help: "Relative strength index (Wilder): above 70 overbought, below 30 oversold" },
  { id: "macd:12:26:9", label: "MACD", help: "EMA 12 minus EMA 26, with a 9-bar signal line and histogram" },
  { id: "atr:14", label: "ATR 14", help: "Average true range: how far price typically moves in one bar" },
];
const DEFAULT_INDICATORS = ["ema:20", "asia", "pdl"];
const SESSION_NAME: Record<string, string> = { asia: "Asia", london: "London", new_york: "New York" };

function load<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}

function save(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private window or storage blocked: settings just do not persist */
  }
}

const usd = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const istTime = (iso: string) => new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit" });

function MarketPill({ m }: { m: GlobalMarketStatus | null }) {
  if (!m) return <span className="skeleton h-4 w-24" />;
  return m.is_open ? (
    <span className="flex items-center gap-1.5 text-xs" title={`Closes ${istTime(m.next_close)} IST`}>
      <span className="size-1.5 rounded-full bg-up" />
      <span className="font-medium text-fg">Open</span>
      {m.session && <span className="text-subtle">· {SESSION_NAME[m.session]} session</span>}
    </span>
  ) : (
    <span className="flex items-center gap-1.5 text-xs" title={m.reason ?? undefined}>
      <span className="size-1.5 rounded-full bg-subtle" />
      <span className="font-medium text-fg">Closed</span>
      <span className="text-subtle">· opens {istTime(m.next_open)} IST</span>
    </span>
  );
}

function DataPanel({ status, check }: { status: GlobalDataStatus | null; check: GlobalDataCheck | null }) {
  const h = status?.history;
  const d = status?.download;
  return (
    <Panel title="Spot data" flush>
      <StatStrip cols={4}>
        <Stat label="Spot history" loading={!status} value={h ? `${h.trading_days_with_bars} days` : ""}
          sub={h?.first_day ? `${h.first_day} to ${h.last_day}` : "none yet"} hint="Trading days of 1-minute Dukascopy bid and ask stored on this Mac" />
        <Stat label="Still to download" loading={!status} value={h ? `${h.days_left} days` : ""} sub={h ? `back to ${h.start_target}` : undefined}
          hint="The download is slow on purpose. It resumes by itself whenever the app is running." />
        <Stat label="Live spot" loading={!status} value={status?.live.twelvedata_configured ? "Twelve Data" : "No key"}
          sub={status?.live.twelvedata_configured ? `${status.live.calls_today} of ${status.live.daily_limit} calls today` : "using GC=F futures for now"} />
        <Stat label="Check against GC=F" loading={!check} value={check?.ok == null ? "Not yet" : check.ok ? "Agrees" : "Disagrees"}
          tone={check?.ok === false ? "down" : null} sub={check?.move_correlation != null ? `moves together ${check.move_correlation}, gap $${check.median_gap}` : check?.detail} />
      </StatStrip>
      {d?.last_error && <p className="border-t border-line px-3 py-2 text-xs text-warn">Download waiting: {d.last_error}{d.backoff_seconds ? `, next try in up to ${Math.round(d.backoff_seconds)} s` : ""}</p>}
      {(h?.gap_days ?? 0) > 0 && <p className="border-t border-line px-3 py-2 text-xs text-muted">{h!.gap_days} trading days have no bars, latest: {h!.gap_examples.join(", ")}</p>}
    </Panel>
  );
}

export default function GlobalChartWorkspace() {
  const [tf, setTf] = useState<Tf>(() => load<Tf>("tb.global.tf", "5m"));
  const [source, setSource] = useState<Source | null>(() => load<Source | null>("tb.global.source", null));
  const [selected, setSelected] = useState<string[]>(() => load("tb.global.indicators", DEFAULT_INDICATORS));
  const [data, setData] = useState<GlobalChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [market, setMarket] = useState<GlobalMarketStatus | null>(null);
  const [status, setStatus] = useState<GlobalDataStatus | null>(null);
  const [check, setCheck] = useState<GlobalDataCheck | null>(null);

  const fetchChart = useCallback(() => {
    const params = new URLSearchParams({ symbol: "XAUUSD", tf, ind: selected.join(",") });
    if (source) params.set("source", source);
    return api<GlobalChartData>(`/global/chart?${params}`)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [tf, source, selected]);

  useEffect(() => {
    fetchChart();
    if (tf === "1d") return;
    const id = setInterval(fetchChart, 30_000);
    return () => clearInterval(id);
  }, [fetchChart, tf]);

  useEffect(() => {
    const refresh = () => {
      api<GlobalMarketStatus>("/global/status").then(setMarket).catch(() => {});
      api<GlobalDataStatus>("/global/data/status").then(setStatus).catch(() => {});
    };
    refresh();
    api<GlobalDataCheck>("/global/data/check").then(setCheck).catch(() => {});
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => save("tb.global.tf", tf), [tf]);
  useEffect(() => save("tb.global.source", source), [source]);
  useEffect(() => save("tb.global.indicators", selected), [selected]);

  const activeSource = data?.price_source ?? source ?? status?.default_source ?? "futures";
  const last = data?.bars.at(-1);
  const change = data?.last_price != null && data.prev_close ? (data.last_price / data.prev_close - 1) * 100 : null;
  const up = (change ?? 0) >= 0;
  const asOf = data?.as_of
    ? new Date(data.as_of * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : null;

  return (
    <div className="flex flex-1 flex-col gap-3">
      <div className="panel flex flex-wrap items-center gap-2 p-2">
        <div className="seg" role="group" aria-label="Timeframe">
          {TIMEFRAMES.map((x) => (
            <button key={x} aria-pressed={x === tf} onClick={() => { setLoading(true); setTf(x); }} className="num">{x}</button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Price source">
          <button aria-pressed={activeSource === "spot"} onClick={() => { setLoading(true); setSource("spot"); }} title="Real spot prices: Dukascopy history, plus Twelve Data when a key is set">Spot</button>
          <button aria-pressed={activeSource === "futures"} onClick={() => { setLoading(true); setSource("futures"); }} title="COMEX futures from Yahoo: current, free, but a different price from spot">Futures</button>
        </div>
        <MarketPill m={market} />
        {loading && <span className="flex items-center gap-1.5 text-xs text-subtle"><span className="size-1.5 animate-pulse rounded-full bg-accent" />Updating</span>}
        {asOf && <span className="ml-auto text-2xs text-subtle">last bar <span className="num">{asOf}</span> IST · {data?.source} · may be delayed</span>}
      </div>

      <div className="panel overflow-hidden">
        <div className="flex flex-wrap items-end gap-x-5 gap-y-2 border-b border-line px-3 py-2.5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="num text-[18px] font-semibold tracking-tight">XAUUSD</h1>
              <span className="truncate text-xs text-muted">Gold, US dollars an ounce</span>
              {activeSource === "futures" && <Badge tone="warn" title="COMEX futures: the level differs from spot">futures price</Badge>}
            </div>
            {data?.last_price != null ? (
              <p className="mt-0.5 flex items-baseline gap-2">
                <span className="num text-[22px] leading-7 font-medium">${usd(data.last_price)}</span>
                {change != null && <span className={`num text-[13px] font-medium ${up ? "text-up" : "text-down"}`}>{up ? "+" : "−"}{Math.abs(change).toFixed(2)}%</span>}
              </p>
            ) : loading ? <div className="skeleton mt-1.5 h-6 w-36" /> : <p className="mt-1 text-xs text-subtle">No price yet</p>}
          </div>
          {last && (
            <dl className="num flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {([["O", last.o], ["H", last.h], ["L", last.l], ["C", last.c]] as const).map(([k, v]) => (
                <div key={k} className="flex gap-1.5"><dt className="text-subtle">{k}</dt><dd className="text-fg">{v.toFixed(2)}</dd></div>
              ))}
              {!last.complete && <span className="text-warn">forming</span>}
            </dl>
          )}
          {data?.prev_close != null && <span className="num text-xs text-subtle">Prev close <span className="text-muted">{usd(data.prev_close)}</span></span>}
        </div>

        <div className="flex flex-wrap gap-1.5 border-b border-line px-3 py-2" role="group" aria-label="Indicators">
          {INDICATORS.map((ind) => {
            const on = selected.includes(ind.id);
            return (
              <button key={ind.id} title={ind.help} aria-pressed={on} className="chip"
                onClick={() => { setLoading(true); setSelected((s) => (on ? s.filter((x) => x !== ind.id) : [...s, ind.id])); }}>
                {ind.label}
              </button>
            );
          })}
        </div>

        {(error || (data?.notes.length ?? 0) > 0) && (
          <div className="space-y-1.5 border-b border-line p-3">
            {error && <Notice tone="error">{error}</Notice>}
            {data?.notes.map((n) => <p key={n} className="text-xs text-warn">{n}</p>)}
          </div>
        )}

        <div className="h-[62vh] min-h-[380px] max-h-[720px]">
          {data && data.bars.length > 0 ? (
            <PriceChart data={data} />
          ) : (
            <div className="grid h-full place-items-center text-xs text-subtle">{loading ? <div className="skeleton h-3/4 w-[92%]" /> : "No bars from this source yet"}</div>
          )}
        </div>
      </div>

      <DataPanel status={status} check={check} />
    </div>
  );
}
