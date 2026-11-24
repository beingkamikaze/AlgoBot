"use client";

import { useCallback, useEffect, useState } from "react";

import { PriceChart } from "@/components/price-chart";
import { SymbolSearch } from "@/components/symbol-search";
import { DataSourceBadge, Notice } from "@/components/ui/kit";
import { Watchlist } from "@/components/watchlist";
import { api, type ChartData } from "@/lib/api";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "1d"] as const;
type Tf = (typeof TIMEFRAMES)[number];

const INDICATORS: { id: string; label: string; help: string }[] = [
  { id: "ema:20", label: "EMA 20", help: "Exponential moving average, 20 bars: the short-term trend" },
  { id: "ema:50", label: "EMA 50", help: "Exponential moving average, 50 bars: the medium-term trend" },
  { id: "vwap", label: "VWAP", help: "Volume-weighted average price, reset each session (stocks and futures; indices have no volume)" },
  { id: "supertrend:10:3", label: "Supertrend", help: "ATR(10) x 3 trailing line: green below price in an uptrend, red above in a downtrend" },
  { id: "bb:20:2", label: "Bollinger", help: "20-bar average with bands 2 standard deviations away" },
  { id: "or:15", label: "Opening range", help: "High and low of 9:15 to 9:30, drawn after the range closes" },
  { id: "pdl", label: "Prev day H/L/C", help: "Yesterday's high, low and close" },
  { id: "rsi:14", label: "RSI 14", help: "Relative strength index (Wilder): above 70 overbought, below 30 oversold" },
  { id: "macd:12:26:9", label: "MACD", help: "EMA 12 minus EMA 26, with a 9-bar signal line and histogram" },
  { id: "atr:14", label: "ATR 14", help: "Average true range: how far price typically moves in one bar" },
];

const DEFAULT_WATCHLIST = ["NIFTY", "BANKNIFTY", "INDIAVIX", "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN"];
const DEFAULT_INDICATORS = ["ema:20", "vwap", "supertrend:10:3", "or:15", "rsi:14"];

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

export default function ChartWorkspace() {
  const [symbol, setSymbol] = useState<string>(
    () => new URLSearchParams(window.location.search).get("symbol")?.toUpperCase() || load("tb.symbol", "NIFTY"),
  );
  const [tf, setTf] = useState<Tf>(() => load<Tf>("tb.tf", "5m"));
  const [selected, setSelected] = useState<string[]>(() => load("tb.indicators", DEFAULT_INDICATORS));
  const [watch, setWatch] = useState<string[]>(() => load("tb.watchlist", DEFAULT_WATCHLIST));
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchChart = useCallback(
    () =>
      api<ChartData>(`/market/chart?symbol=${encodeURIComponent(symbol)}&tf=${tf}&ind=${encodeURIComponent(selected.join(","))}`)
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e: Error) => setError(e.message))
        .finally(() => setLoading(false)),
    [symbol, tf, selected],
  );

  useEffect(() => {
    fetchChart();
    if (tf === "1d") return;
    const id = setInterval(fetchChart, 30_000);
    return () => clearInterval(id);
  }, [fetchChart, tf]);

  useEffect(() => save("tb.symbol", symbol), [symbol]);
  useEffect(() => save("tb.tf", tf), [tf]);
  useEffect(() => save("tb.indicators", selected), [selected]);
  useEffect(() => save("tb.watchlist", watch), [watch]);

  function pick(s: string) {
    setLoading(true);
    setSymbol(s);
    setWatch((w) => (w.includes(s) ? w : [...w, s]));
  }

  const last = data?.bars.at(-1);
  const change = data?.last_price != null && data.prev_close ? ((data.last_price / data.prev_close - 1) * 100) : null;
  const asOf = data?.as_of
    ? new Date(data.as_of * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : null;

  const up = (change ?? 0) >= 0;
  return (
    <div className="flex flex-1 flex-col gap-3 xl:flex-row">
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <div className="panel flex flex-wrap items-center gap-2 p-2">
          <SymbolSearch onPick={pick} />
          <div className="seg" role="group" aria-label="Timeframe">
            {TIMEFRAMES.map((x) => (
              <button
                key={x}
                aria-pressed={x === tf}
                onClick={() => {
                  setLoading(true);
                  setTf(x);
                }}
                className="num"
              >
                {x}
              </button>
            ))}
          </div>
          {loading && <span className="flex items-center gap-1.5 text-xs text-subtle"><span className="size-1.5 animate-pulse rounded-full bg-accent" />Updating</span>}
          {asOf && <span className="ml-auto flex items-center gap-2 text-2xs text-subtle">as of <span className="num">{asOf}</span> IST <DataSourceBadge source={data?.source} /></span>}
        </div>

        <div className="panel overflow-hidden">
          <div className="flex flex-wrap items-end gap-x-5 gap-y-2 border-b border-line px-3 py-2.5">
            <div className="min-w-0">
              <div className="flex items-baseline gap-2">
                <h1 className="num text-[18px] font-semibold tracking-tight">{data?.symbol ?? symbol}</h1>
                {data?.name && <span className="truncate text-xs text-muted">{data.name}</span>}
              </div>
              {data?.last_price != null ? (
                <p className="mt-0.5 flex items-baseline gap-2">
                  <span className="num text-[22px] leading-7 font-medium">{data.last_price.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                  {change != null && (
                    <span className={`num text-[13px] font-medium ${up ? "text-up" : "text-down"}`}>{up ? "+" : "−"}{Math.abs(change).toFixed(2)}%</span>
                  )}
                </p>
              ) : <div className="skeleton mt-1.5 h-6 w-36" />}
            </div>
            {last && (
              <dl className="num flex flex-wrap gap-x-4 gap-y-1 text-xs">
                {([["O", last.o], ["H", last.h], ["L", last.l], ["C", last.c]] as const).map(([k, v]) => (
                  <div key={k} className="flex gap-1.5"><dt className="text-subtle">{k}</dt><dd className="text-fg">{v.toFixed(2)}</dd></div>
                ))}
                {!last.complete && <span className="text-warn">forming</span>}
              </dl>
            )}
            {data?.prev_close != null && <span className="num text-xs text-subtle">Prev close <span className="text-muted">{data.prev_close.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span></span>}
          </div>

          <div className="flex flex-wrap gap-1.5 border-b border-line px-3 py-2" role="group" aria-label="Indicators">
            {INDICATORS.map((ind) => {
              const on = selected.includes(ind.id);
              return (
                <button
                  key={ind.id}
                  title={ind.help}
                  aria-pressed={on}
                  onClick={() => {
                    setLoading(true);
                    setSelected((s) => (on ? s.filter((x) => x !== ind.id) : [...s, ind.id]));
                  }}
                  className="chip"
                >
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
              <div className="grid h-full place-items-center text-xs text-subtle">{loading ? <div className="skeleton h-3/4 w-[92%]" /> : "No bars for this symbol and timeframe"}</div>
            )}
          </div>
        </div>
      </div>

      <div className="w-full shrink-0 xl:w-72">
        <Watchlist symbols={watch} active={symbol} onPick={pick} onRemove={(s) => setWatch((w) => w.filter((x) => x !== s))} />
      </div>
    </div>
  );
}
