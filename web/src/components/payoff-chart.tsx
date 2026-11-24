"use client";

import { C } from "@/lib/theme";

/** P&L of an option position against the index level: at expiry (solid) and today (dashed). Plain SVG. */
export function PayoffChart({ points, spot, breakevens }: { points: { x: number; expiry: number; today: number }[]; spot: number; breakevens: number[] }) {
  if (!points.length) return null;
  const W = 640, H = 240, P = { l: 56, r: 12, t: 14, b: 28 };
  const xs = points.map((p) => p.x);
  const ys = points.flatMap((p) => [p.expiry, p.today]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(0, ...ys), y1 = Math.max(0, ...ys);
  const pad = (y1 - y0) * 0.08 || 1;
  const sx = (x: number) => P.l + ((x - x0) / (x1 - x0)) * (W - P.l - P.r);
  const sy = (y: number) => P.t + (1 - (y - (y0 - pad)) / (y1 + pad - (y0 - pad))) * (H - P.t - P.b);
  const path = (k: "expiry" | "today") => points.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p[k]).toFixed(1)}`).join(" ");
  const fmt = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0));
  const ticksY = [y0, 0, y1].filter((v, i, a) => a.indexOf(v) === i);
  const ticksX = [x0, (x0 + x1) / 2, x1];
  const font = { fontFamily: "var(--font-mono)", fontSize: 10 };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Payoff chart: profit and loss against the index level">
      <rect x={P.l} y={sy(Math.max(y1 + pad, 0))} width={W - P.l - P.r} height={Math.max(sy(0) - sy(y1 + pad), 0)} fill={C.up} fillOpacity={0.06} />
      <rect x={P.l} y={sy(0)} width={W - P.l - P.r} height={Math.max(sy(y0 - pad) - sy(0), 0)} fill={C.down} fillOpacity={0.06} />
      {ticksY.map((v) => <line key={`g${v}`} x1={P.l} x2={W - P.r} y1={sy(v)} y2={sy(v)} stroke={C.line} strokeWidth={1} />)}
      <line x1={P.l} x2={W - P.r} y1={sy(0)} y2={sy(0)} stroke={C.subtle} strokeWidth={1} />
      <line x1={sx(spot)} x2={sx(spot)} y1={P.t} y2={H - P.b} stroke={C.info} strokeDasharray="3 3" />
      {breakevens.filter((b) => b >= x0 && b <= x1).map((b) => (
        <g key={b}>
          <line x1={sx(b)} x2={sx(b)} y1={P.t} y2={H - P.b} stroke={C.subtle} strokeDasharray="2 4" />
          <text x={sx(b)} y={H - P.b + 22} textAnchor="middle" fill={C.text} style={font}>{b.toFixed(0)}</text>
        </g>
      ))}
      <path d={path("today")} fill="none" stroke={C.warn} strokeWidth={1.5} strokeDasharray="5 4" />
      <path d={path("expiry")} fill="none" stroke={C.fg} strokeWidth={2} />
      {ticksY.map((v) => <text key={v} x={P.l - 6} y={sy(v) + 3.5} textAnchor="end" fill={C.text} style={font}>₹{fmt(v)}</text>)}
      {ticksX.map((v) => <text key={v} x={sx(v)} y={H - P.b + 12} textAnchor="middle" fill={C.subtle} style={font}>{v.toFixed(0)}</text>)}
      <text x={sx(spot) + 4} y={P.t + 8} fill={C.info} style={font}>spot {spot.toFixed(0)}</text>
    </svg>
  );
}
