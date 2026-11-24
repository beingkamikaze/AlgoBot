"use client";

/** The shared building blocks every page uses: headers, panels, stats, states, values. */
import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { inr } from "@/lib/api";

import { IconAlert, IconCheck, IconInfo, IconX } from "./icons";

const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");
export { cx };

// ---------------------------------------------------------------- layout

export function Page({ children, wide = false, className }: { children: ReactNode; wide?: boolean; className?: string }) {
  return (
    <main className={cx("mx-auto flex w-full flex-1 flex-col gap-4 px-4 py-4 sm:px-5 lg:py-5", wide ? "max-w-[1680px]" : "max-w-[1360px]", className)}>
      {children}
    </main>
  );
}

export function PageHeader({ title, subtitle, actions, meta }: { title: string; subtitle?: ReactNode; actions?: ReactNode; meta?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-[17px] font-semibold tracking-tight text-fg">{title}</h1>
          {meta}
        </div>
        {subtitle && <p className="mt-0.5 max-w-3xl text-[12.5px] text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Panel({ title, actions, children, className, bodyClassName, flush = false, id }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClassName?: string; flush?: boolean; id?: string;
}) {
  return (
    <section id={id} className={cx("panel flex min-w-0 flex-col", className)}>
      {(title || actions) && (
        <div className="panel-head">
          {typeof title === "string" ? <h2 className="panel-title">{title}</h2> : title}
          {actions && <div className="ml-auto flex items-center gap-1.5">{actions}</div>}
        </div>
      )}
      <div className={cx(flush ? "" : "p-3", "min-w-0 flex-1", bodyClassName)}>{children}</div>
    </section>
  );
}

/** A table that scrolls on its own on small screens, never the page. */
export function TableWrap({ children, max }: { children: ReactNode; max?: string }) {
  return <div className="overflow-x-auto" style={max ? { maxHeight: max, overflowY: "auto" } : undefined}>{children}</div>;
}

// ---------------------------------------------------------------- values

/** A price with Indian grouping and two decimals: 1,166.10. */
export const px = (n: number | null | undefined) =>
  n == null ? "–" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function Num({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("num", className)}>{children}</span>;
}

/** Signed rupees, green or red; the sign is also written so colour is never the only cue. */
export function Pnl({ v, className, plain = false }: { v: number | null | undefined; className?: string; plain?: boolean }) {
  if (v == null) return <span className={cx("num text-subtle", className)}>–</span>;
  v = Math.round(v);                                   // shown in whole rupees: −0.4 must not read as "−₹0"
  const tone = v > 0 ? "text-up" : v < 0 ? "text-down" : "text-muted";
  const s = inr(Math.abs(v));
  return <span className={cx("num", plain ? "" : tone, className)}>{v > 0 ? "+" : v < 0 ? "−" : ""}{s}</span>;
}

export function RValue({ v, className }: { v: number | null | undefined; className?: string }) {
  if (v == null) return <span className={cx("num text-subtle", className)}>–</span>;
  v = Math.round(v * 100) / 100;                       // −0.004 rounds to 0.00: no sign, no colour
  return <span className={cx("num", v > 0 ? "text-up" : v < 0 ? "text-down" : "text-muted", className)}>{v > 0 ? "+" : v < 0 ? "−" : ""}{Math.abs(v).toFixed(2)}R</span>;
}

export function Side({ side, className }: { side: string; className?: string }) {
  const buy = side === "buy";
  return <span className={cx("badge", buy ? "badge-up" : "badge-down", className)}>{buy ? "Buy" : "Sell"}</span>;
}

export function Badge({ tone = "neutral", children, title }: { tone?: "neutral" | "up" | "down" | "warn" | "info" | "accent" | "live"; children: ReactNode; title?: string }) {
  return <span title={title} className={`badge badge-${tone}`}>{children}</span>;
}

/** Where NSE prices came from: Kite's live ticks, Kite's candles, or the free Yahoo fallback (no Kite login today). */
export function DataSourceBadge({ source }: { source: string | null | undefined }) {
  if (!source) return null;
  const s = ({
    kite_live: { tone: "live", label: "Kite live", title: "Bars built from Kite's real-time ticks" },
    kite: { tone: "info", label: "Kite", title: "Kite's own candles (real time; this symbol is not streaming)" },
    yahoo: { tone: "warn", label: "Yahoo, delayed", title: "Free Yahoo data, can lag: log in to Kite on the Live page for real-time prices" },
  } as const)[source] ?? { tone: "neutral" as const, label: source, title: undefined };
  return <Badge tone={s.tone} title={s.title}>{s.label}</Badge>;
}

/** Order / trade / proposal state as a badge with a consistent colour. */
export function StateBadge({ state }: { state: string }) {
  const tone = ({ working: "info", sending: "info", proposed: "warn", filled: "up", passed: "up", approved: "up", paper: "accent",
    rejected: "down", failed: "down", retired: "down", cancelled: "neutral", expired: "neutral", testing: "info", new: "neutral" } as const)[state] ?? "neutral";
  return <Badge tone={tone}>{state}</Badge>;
}

// ---------------------------------------------------------------- stats

export function Stat({ label, value, sub, tone, hint, loading }: {
  label: ReactNode; value: ReactNode; sub?: ReactNode; tone?: "up" | "down" | null; hint?: string; loading?: boolean;
}) {
  return (
    <div className="min-w-0 px-3 py-2.5" title={hint}>
      <p className="eyebrow truncate">{label}</p>
      {loading ? <div className="skeleton mt-1.5 h-5 w-24" /> : (
        <p className={cx("num mt-0.5 truncate text-[19px] leading-7 font-medium", tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-fg")}>{value}</p>
      )}
      {sub && <p className="mt-0.5 truncate text-xs text-muted">{sub}</p>}
    </div>
  );
}

/** A row of stats sharing one panel, divided by hairlines: dense and calm. */
export function StatStrip({ children, cols = 4 }: { children: ReactNode; cols?: 2 | 3 | 4 | 5 | 6 }) {
  const grid = { 2: "grid-cols-2", 3: "grid-cols-2 sm:grid-cols-3", 4: "grid-cols-2 lg:grid-cols-4", 5: "grid-cols-2 sm:grid-cols-3 lg:grid-cols-5", 6: "grid-cols-2 sm:grid-cols-3 xl:grid-cols-6" }[cols];
  return <div className={cx("panel grid divide-line [&>*]:border-line max-sm:[&>*:nth-child(odd)]:border-r max-sm:[&>*:nth-child(n+3)]:border-t sm:divide-x", grid)}>{children}</div>;
}

export function Meter({ label, used, limit, format = (n: number) => String(n), hint }: {
  label: string; used: number; limit: number; format?: (n: number) => string; hint?: string;
}) {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  const color = pct >= 100 ? "bg-down" : pct >= 70 ? "bg-warn" : "bg-accent";
  return (
    <div className="space-y-1.5" title={hint}>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="text-muted">{label}</span>
        <span className="num text-fg">{format(used)} <span className="text-subtle">/ {format(limit)}</span></span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-raised" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={limit} aria-valuenow={used}>
        <div className={cx("h-full rounded-full transition-[width] duration-500", color)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- states

export function Notice({ tone = "info", title, children, action }: { tone?: "info" | "warn" | "error" | "success" | "live"; title?: ReactNode; children?: ReactNode; action?: ReactNode }) {
  const map = {
    info: ["border-info/30 bg-info/[0.06]", "text-info", IconInfo],
    warn: ["border-warn/30 bg-warn/[0.06]", "text-warn", IconAlert],
    error: ["border-down/35 bg-down/[0.07]", "text-down", IconAlert],
    success: ["border-up/30 bg-up/[0.06]", "text-up", IconCheck],
    live: ["border-live bg-live/[0.1]", "text-live", IconAlert],
  } as const;
  const [box, ink, Icon] = map[tone];
  return (
    <div role={tone === "error" ? "alert" : "status"} className={cx("flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-[12.5px]", box)}>
      <Icon size={15} className={cx("mt-px shrink-0", ink)} />
      <div className="min-w-0 flex-1 space-y-0.5">
        {title && <p className={cx("font-medium", tone === "info" ? "text-fg" : ink)}>{title}</p>}
        {children && <div className="text-muted [&_code]:num [&_code]:text-fg">{children}</div>}
      </div>
      {action}
    </div>
  );
}

export function Empty({ title, children, icon, action }: { title: string; children?: ReactNode; icon?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 px-4 py-8 text-center">
      {icon && <div className="mb-1 grid size-9 place-items-center rounded-md border border-line bg-raised text-subtle">{icon}</div>}
      <p className="text-[13px] font-medium text-fg">{title}</p>
      {children && <p className="max-w-sm text-xs text-muted">{children}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("skeleton", className)} />;
}

export function SkeletonRows({ rows = 4, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="space-y-2 p-3" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex gap-3">{Array.from({ length: cols }, (_, j) => <div key={j} className="skeleton h-4 flex-1" />)}</div>
      ))}
    </div>
  );
}

export function SignInRequired({ what }: { what: string }) {
  return (
    <Page>
      <div className="panel mx-auto mt-10 w-full max-w-md">
        <Empty title="Sign in required" action={<Link href="/" className="btn btn-primary">Go to sign in</Link>}>
          {what} uses your account. Sign in on the dashboard first.
        </Empty>
      </div>
    </Page>
  );
}

export function PageLoading() {
  return (
    <Page>
      <div className="skeleton h-6 w-48" />
      <div className="skeleton h-20 w-full" />
      <div className="skeleton h-64 w-full" />
    </Page>
  );
}

export function Check({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <li className="flex items-start gap-2">
      {ok ? <IconCheck size={14} className="mt-0.5 shrink-0 text-up" label="met" /> : <IconX size={14} className="mt-0.5 shrink-0 text-down" label="not met" />}
      <span className="min-w-0">{children}</span>
    </li>
  );
}

/** A finance term with a plain-words explanation on hover and focus. */
export function Term({ children, tip }: { children: ReactNode; tip: string }) {
  return (
    <span tabIndex={0} title={tip} className="cursor-help underline decoration-subtle decoration-dotted underline-offset-[3px] outline-none focus-visible:decoration-accent">
      {children}
    </span>
  );
}

export const TERMS = {
  R: "R = the money you risk on a trade (entry to stop). +1R means you made what you risked; −1R means the stop was hit.",
  pf: "Profit factor = gross profit ÷ gross loss. Above 1 makes money; 1.5+ is solid.",
  winRate: "Share of trades that closed with a profit, after charges.",
  openRisk: "What you would lose, after charges, if every open position hit its stop now.",
  drawdown: "The largest drop from a peak in equity to a later low.",
  pcr: "Put-call ratio of open interest. Above 1: more puts open than calls.",
  maxPain: "The strike where option buyers in total would lose the most at expiry.",
  iv: "Implied volatility: the yearly move the option price expects, in %.",
  delta: "How much the option price moves for a 1-point move in the index.",
  mis: "MIS: Zerodha's intraday product. Positions must close the same day (auto square-off from 15:25).",
  expectancy: "Average result per trade in R, after charges.",
} as const;

/** A deliberate action: press and hold (mouse, touch, or Space / Enter) until the bar fills. Releasing early cancels. */
export function HoldButton({ onDone, children, ms = 1500, className, disabled }: {
  onDone: () => void; children: ReactNode; ms?: number; className?: string; disabled?: boolean;
}) {
  const [p, setP] = useState(0);
  const timer = useRef<number | null>(null);
  const start = useRef(0);
  const stop = () => { if (timer.current) cancelAnimationFrame(timer.current); timer.current = null; setP(0); };
  const begin = () => {
    if (disabled || timer.current) return;
    start.current = performance.now();
    const step = (t: number) => {
      const k = Math.min(1, (t - start.current) / ms);
      setP(k);
      if (k >= 1) { timer.current = null; setP(0); onDone(); return; }
      timer.current = requestAnimationFrame(step);
    };
    timer.current = requestAnimationFrame(step);
  };
  useEffect(() => () => { if (timer.current) cancelAnimationFrame(timer.current); }, []);
  return (
    <button type="button" disabled={disabled} aria-label={`${typeof children === "string" ? children : "Confirm"}: press and hold`}
      onPointerDown={begin} onPointerUp={stop} onPointerLeave={stop} onPointerCancel={stop}
      onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); begin(); } }}
      onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") stop(); }}
      className={cx("btn relative overflow-hidden touch-none select-none", className)}>
      <span className="absolute inset-y-0 left-0 bg-warn/25" style={{ width: `${p * 100}%` }} aria-hidden />
      <span className="relative">{p > 0 ? "Keep holding…" : children}</span>
    </button>
  );
}
