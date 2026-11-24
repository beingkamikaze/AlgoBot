"use client";

import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  HistogramSeries,
  type IChartApi,
  LineSeries,
  LineStyle,
  LineType,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

import type { ChartData, IndicatorSeries } from "@/lib/api";
import { C, chartBase } from "@/lib/theme";

// The chart draws times as UTC; shifting by 5:30 makes its axis read IST.
const IST_OFFSET = 19800;
const t = (unix: number) => (unix + IST_OFFSET) as UTCTimestamp;

const PALETTE = C.series;
const UP = C.up;
const DOWN = C.down;

function colorFor(s: IndicatorSeries, i: number): string {
  if (s.id.startsWith("vwap")) return C.warn;
  if (s.id.startsWith("or:")) return "#b48cf2";
  if (s.id.startsWith("pdl")) return C.subtle;
  if (s.id.startsWith("bb:")) return "#4fc3d9";
  if (s.id.endsWith(".signal")) return "#e5a93b";
  return PALETTE[i % PALETTE.length];
}

export type ChartLevel = { price: number; label: string; color: string; style?: "solid" | "dashed" | "dotted"; width?: 1 | 2; keepInView?: boolean };
export type ChartMarker = { t: number; text: string; color: string; position: "aboveBar" | "belowBar"; shape: "arrowUp" | "arrowDown" | "circle" };

export function PriceChart({ data, levels = [], markers = [], fit = false }: {
  data: ChartData; dark?: boolean; levels?: ChartLevel[]; markers?: ChartMarker[]; fit?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const base = chartBase();
    const chart = createChart(box.current, {
      ...base,
      autoSize: true,
      crosshair: { ...base.crosshair, mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: C.line },
      timeScale: { borderColor: C.line, timeVisible: data.tf !== "1d", secondsVisible: false },
    });
    chartRef.current = chart;

    const bars = data.bars;
    const inView = levels.filter((l) => l.keepInView).map((l) => l.price);
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN,
      // the trade's entry, stop and target always fit on the chart, even when price is far from them
      ...(inView.length ? {
        autoscaleInfoProvider: (original: () => { priceRange: { minValue: number; maxValue: number } } | null) => {
          const r = original();
          if (!r) return r;
          return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, ...inView), maxValue: Math.max(r.priceRange.maxValue, ...inView) } };
        },
      } : {}),
    });
    candles.setData(bars.map((b) => ({ time: t(b.t), open: b.o, high: b.h, low: b.l, close: b.c })));

    if (bars.some((b) => b.v > 0)) {
      const vol = chart.addSeries(HistogramSeries, { priceScaleId: "volume", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      vol.setData(bars.map((b) => ({ time: t(b.t), value: b.v, color: b.c >= b.o ? `${UP}55` : `${DOWN}55` })));
    }
    for (const lv of levels) {          // trade levels and support / resistance, labelled on the price axis
      candles.createPriceLine({
        price: lv.price, color: lv.color, lineWidth: lv.width ?? 1, axisLabelVisible: true, title: lv.label,
        lineStyle: lv.style === "dashed" ? LineStyle.Dashed : lv.style === "dotted" ? LineStyle.Dotted : LineStyle.Solid,
      });
    }
    if (markers.length) {
      const first = bars[0]?.t ?? 0;
      createSeriesMarkers(candles, markers.filter((m) => m.t >= first).map((m) => {
        const bar = [...bars].reverse().find((b) => b.t <= m.t) ?? bars[0];   // snap to the bar the event fell in
        return { time: t(bar.t), position: m.position, color: m.color, shape: m.shape, text: m.text };
      }));
    }
    if (data.prev_close && data.tf !== "1d" && !levels.some((l) => /prev close/.test(l.label))) {
      candles.createPriceLine({ price: data.prev_close, color: C.subtle, lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: true, title: "prev close" });
    }

    const paneIndex = new Map<string, number>();
    data.indicators.forEach((s, i) => {
      let pane = 0;
      if (s.placement !== "overlay") {
        if (!paneIndex.has(s.placement)) paneIndex.set(s.placement, paneIndex.size + 1);
        pane = paneIndex.get(s.placement)!;
      }
      const points = s.values.map((v, k) => (v === null ? { time: t(bars[k].t) } : { time: t(bars[k].t), value: v }));
      if (s.kind === "histogram") {
        const h = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane);
        h.setData(s.values.map((v, k) => (v === null ? { time: t(bars[k].t) } : { time: t(bars[k].t), value: v, color: v >= 0 ? `${UP}99` : `${DOWN}99` })));
        return;
      }
      const line = chart.addSeries(LineSeries, {
        color: colorFor(s, i),
        lineWidth: s.kind === "line" ? 2 : 1,
        lineType: s.kind === "step" ? LineType.WithSteps : LineType.Simple,
        lineStyle: s.kind.startsWith("band") ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: s.placement !== "overlay" || s.kind === "line",
        crosshairMarkerVisible: false,
        title: s.placement === "overlay" ? "" : s.label,
      }, pane);
      if (s.colors) {
        line.setData(points.map((p, k) => ("value" in p ? { ...p, color: s.colors![k] === "up" ? UP : DOWN } : p)));
      } else {
        line.setData(points);
      }
      if (s.id.startsWith("rsi")) {
        line.createPriceLine({ price: 70, color: C.subtle, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
        line.createPriceLine({ price: 30, color: C.subtle, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
      }
    });
    chart.panes().forEach((p, i) => p.setStretchFactor(i === 0 ? 3 : 1));

    // show roughly the last session or two, the rest is a scroll away
    const visible = data.tf === "1d" ? 120 : data.tf === "1h" ? 60 : 160;
    if (fit) chart.timeScale().fitContent();
    else if (bars.length > visible) chart.timeScale().setVisibleLogicalRange({ from: bars.length - visible, to: bars.length + 3 });
    else chart.timeScale().fitContent();

    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [data, levels, markers, fit]);

  return <div ref={box} className="h-full w-full" />;
}
