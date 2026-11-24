"use client";

import { AreaSeries, BaselineSeries, createChart, HistogramSeries, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

import { C, chartBase } from "@/lib/theme";

const IST = 19800;

/** Cumulative P&L: green above zero, red below (a baseline at 0, so the sign is also the position). */
export function EquityChart({ points, height = "h-56", precision = 0 }: { points: { t: number; equity: number }[]; dark?: boolean; height?: string; precision?: number }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current || points.length === 0) return;
    const chart = createChart(box.current, {
      ...chartBase(),
      autoSize: true,
      grid: { vertLines: { visible: false }, horzLines: { color: C.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true },
      handleScroll: false,
      handleScale: false,
    });
    const s = chart.addSeries(BaselineSeries, {
      baseValue: { type: "price", price: 0 },
      topLineColor: C.up, topFillColor1: `${C.up}38`, topFillColor2: `${C.up}05`,
      bottomLineColor: C.down, bottomFillColor1: `${C.down}05`, bottomFillColor2: `${C.down}38`,
      lineWidth: 2,
      priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision },
      priceLineVisible: false,
    });
    // one point per second at most: several trades can close in the same bar
    const seen = new Map<number, number>();
    for (const p of points) seen.set(p.t + IST, p.equity);
    s.setData([...seen].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ time: t as UTCTimestamp, value: v })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, precision]);
  return <div ref={box} className={`${height} w-full`} />;
}

/** One bar per trading day: net P&L after charges. */
export function DailyPnlChart({ days, height = "h-40" }: { days: { day: string; net: number }[]; height?: string }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current || days.length === 0) return;
    const chart = createChart(box.current, {
      ...chartBase(),
      autoSize: true,
      grid: { vertLines: { visible: false }, horzLines: { color: C.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      handleScroll: false,
      handleScale: false,
    });
    const s = chart.addSeries(HistogramSeries, { priceFormat: { type: "price", precision: 0, minMove: 1 }, priceLineVisible: false, lastValueVisible: false });
    s.setData(days.map((d) => ({ time: d.day, value: d.net, color: d.net >= 0 ? C.up : C.down })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [days]);
  return <div ref={box} className={`${height} w-full`} />;
}

/** A small area line for a single series (e.g. an index), no axes. */
export function Sparkline({ values, up }: { values: number[]; up: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current || values.length < 2) return;
    const chart = createChart(box.current, {
      ...chartBase(),
      autoSize: true,
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      rightPriceScale: { visible: false },
      timeScale: { visible: false },
      crosshair: { vertLine: { visible: false }, horzLine: { visible: false } },
      handleScroll: false,
      handleScale: false,
    });
    const col = up ? C.up : C.down;
    const s = chart.addSeries(AreaSeries, { lineColor: col, topColor: `${col}30`, bottomColor: `${col}00`, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    s.setData(values.map((v, i) => ({ time: (i + 1) as UTCTimestamp, value: v })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [values, up]);
  return <div ref={box} className="h-8 w-full" aria-hidden />;
}
