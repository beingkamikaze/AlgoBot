"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { EquityChart } from "@/components/equity-chart";
import { IconRefresh, IconShield } from "@/components/ui/icons";
import {
  Badge, Empty, Notice, Page, PageHeader, PageLoading, Panel, Pnl, RValue, SignInRequired, SkeletonRows, Stat, StatStrip, TableWrap, Term, TERMS,
} from "@/components/ui/kit";
import { api } from "@/lib/api";
import { supabase } from "@/lib/supabase";

// ---------------------------------------------------------------- the /api/forward shape

type Stats = {
  trades: number; trades_with_r: number; wins: number; win_rate: number | null; average_r: number | null; total_r: number | null;
  net_pnl: number; worst_losing_streak: number; max_drawdown_r: number; curve: { t: number; equity: number }[];
  trades_per_month?: number | null;
};
type Expected = {
  trades: number; average_r: number | null; win_rate: number | null; trades_per_month: number | null; range: [number, number] | null;
  range_method: string; held_back_average_r: number | null; period?: { from?: string | null; to?: string | null };
  backtest_id?: string; universe?: string; symbols?: number;
};
type Group = {
  key: string; strategy: string; name: string; version: string; version_kind: "default" | "lab"; instrument: string;
  forward: Stats; expected: Expected | null; gap: { average_r: number | null; win_rate: number | null } | null;
  verdict: { kind: "too_few" | "no_backtest" | "inside" | "below" | "above"; text: string }; notes: string[];
};
type Section = { since: string | null; strategies: Group[]; overridden: Stats & { by_strategy?: { strategy: string; trades: number; average_r: number | null; net_pnl: number }[] }; unlinked: Stats };
type Forward = { min_trades: number; as_of: string; nse: Section; xauusd: (Section & { available: true; currency: string }) | { available: false; note: string } };

// ---------------------------------------------------------------- small formatters

const pct = (x: number | null | undefined) => (x == null ? "–" : `${(x * 100).toFixed(0)}%`);
const pts = (x: number | null | undefined) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x * 100).toFixed(0)} pts`);
const num = (x: number | null | undefined) => (x == null ? "–" : String(x));
const usd = (x: number | null | undefined) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}$${Math.abs(x).toLocaleString("en-US", { maximumFractionDigits: 0 })}`);

function Money({ v, currency }: { v: number | null | undefined; currency: "INR" | "USD" }) {
  if (currency === "INR") return <Pnl v={v} />;
  return <span className={`num ${v == null || v === 0 ? "text-muted" : v > 0 ? "text-up" : "text-down"}`}>{usd(v)}</span>;
}

const VERDICT_TONE = { too_few: "neutral", no_backtest: "neutral", inside: "info", below: "down", above: "warn" } as const;
const VERDICT_LABEL = { too_few: "Too few trades", no_backtest: "No backtest", inside: "In line", below: "Worse than expected", above: "Better than expected" } as const;

// ---------------------------------------------------------------- one strategy

function Row({ label, fwd, exp, gap, tip }: { label: ReactNode; fwd: ReactNode; exp: ReactNode; gap?: ReactNode; tip?: string }) {
  return (
    <tr className="border-t border-line">
      <td className="py-1.5 pr-3 text-muted">{tip ? <Term tip={tip}>{label}</Term> : label}</td>
      <td className="num py-1.5 pr-3 text-right text-fg">{fwd}</td>
      <td className="num py-1.5 pr-3 text-right text-muted">{exp}</td>
      <td className="num py-1.5 text-right text-subtle">{gap ?? ""}</td>
    </tr>
  );
}

function StrategyCard({ g, currency }: { g: Group; currency: "INR" | "USD" }) {
  const f = g.forward;
  const e = g.expected;
  return (
    <Panel
      title={
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h2 className="panel-title truncate">{g.name}</h2>
          <Badge tone={g.version_kind === "lab" ? "accent" : "neutral"} title={`version ${g.version}`}>{g.version_kind === "lab" ? `Lab v${g.version}` : "default settings"}</Badge>
          <Badge>{g.instrument}</Badge>
        </div>
      }
      actions={<Badge tone={VERDICT_TONE[g.verdict.kind]}>{VERDICT_LABEL[g.verdict.kind]}</Badge>}
    >
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-3">
          <p className="text-[12.5px] text-fg">{g.verdict.text}</p>
          <TableWrap>
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="text-2xs text-subtle">
                  <th className="pb-1 pr-3 text-left font-normal" />
                  <th className="pb-1 pr-3 text-right font-normal">Forward (paper)</th>
                  <th className="pb-1 pr-3 text-right font-normal">Backtest expected</th>
                  <th className="pb-1 text-right font-normal">Gap</th>
                </tr>
              </thead>
              <tbody>
                <Row label="Trades" fwd={f.trades} exp={e ? e.trades.toLocaleString("en-IN") : "–"} />
                <Row label="Trades a month" fwd={num(f.trades_per_month)} exp={num(e?.trades_per_month)} />
                <Row label="Win rate" tip={TERMS.winRate} fwd={pct(f.win_rate)} exp={pct(e?.win_rate)} gap={g.gap ? pts(g.gap.win_rate) : undefined} />
                <Row label="Average R" tip={TERMS.expectancy} fwd={<RValue v={f.average_r} />} exp={<RValue v={e?.average_r} />} gap={g.gap?.average_r != null ? <RValue v={g.gap.average_r} /> : undefined} />
                {e?.held_back_average_r != null && <Row label="Average R, held-back part" fwd="" exp={<RValue v={e.held_back_average_r} />} />}
                {e?.range && <Row label="Rough 95% range" fwd="" exp={`${e.range[0] >= 0 ? "+" : "−"}${Math.abs(e.range[0]).toFixed(2)} to ${e.range[1] >= 0 ? "+" : "−"}${Math.abs(e.range[1]).toFixed(2)}R`} />}
                <Row label={`Total P&L (${currency === "INR" ? "₹" : "$"})`} fwd={<Money v={f.net_pnl} currency={currency} />} exp="" />
                <Row label="Worst losing streak" fwd={f.worst_losing_streak} exp="" />
                <Row label="Max drawdown" tip={TERMS.drawdown} fwd={`${f.max_drawdown_r.toFixed(2)}R`} exp="" />
              </tbody>
            </table>
          </TableWrap>
          {e && (
            <p className="text-2xs text-subtle">
              Backtest {e.backtest_id ? <Link href="/backtests" className="link">{e.backtest_id}</Link> : null}
              {e.universe ? ` · ${e.universe}${e.symbols ? ` (${e.symbols} symbols)` : ""}` : ""}
              {e.period?.from ? ` · ${e.period.from} → ${e.period.to}` : ""} · range: {e.range_method}
            </p>
          )}
          {g.notes.length > 0 && (
            <ul className="space-y-1 text-xs text-muted">
              {g.notes.map((n) => <li key={n} className="flex gap-2"><span className="mt-[7px] size-1 shrink-0 rounded-full bg-subtle" />{n}</li>)}
            </ul>
          )}
        </div>
        <div className="min-w-0">
          <p className="eyebrow mb-1">Cumulative R, forward trades</p>
          {f.curve.length > 0 ? <EquityChart points={f.curve} height="h-44" precision={2} /> : <Empty title="No trades with R yet" />}
        </div>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- the groups kept apart

function SideGroup({ title, s, currency, children }: { title: string; s: Stats; currency: "INR" | "USD"; children: ReactNode }) {
  return (
    <Panel title={title}>
      <div className="space-y-2 text-[12.5px]">
        <p className="text-muted">{children}</p>
        {s.trades === 0 ? <p className="text-subtle">None in this period.</p> : (
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            <span className="text-muted">Trades <span className="num text-fg">{s.trades}</span></span>
            <span className="text-muted">Win rate <span className="num text-fg">{pct(s.win_rate)}</span></span>
            <span className="text-muted">Average <RValue v={s.average_r} /></span>
            <span className="text-muted">P&amp;L <Money v={s.net_pnl} currency={currency} /></span>
          </div>
        )}
      </div>
    </Panel>
  );
}

function SectionView({ s, currency, minTrades, linkHint }: { s: Section; currency: "INR" | "USD"; minTrades: number; linkHint: ReactNode }) {
  const clean = s.strategies.reduce((a, g) => a + g.forward.trades, 0);
  const net = s.strategies.reduce((a, g) => a + g.forward.net_pnl, 0);
  return (
    <div className="space-y-3">
      <StatStrip cols={4}>
        <Stat label="Clean trades linked to a strategy" value={clean} sub={`judged from ${minTrades} trades each`} />
        <Stat label="Their P&L after charges" value={<Money v={net} currency={currency} />} />
        <Stat label="Rules not followed (kept apart)" value={s.overridden.trades} sub={s.overridden.trades ? <Money v={s.overridden.net_pnl} currency={currency} /> : "none"} />
        <Stat label="No strategy linked" value={s.unlinked.trades} sub={s.unlinked.trades ? <Money v={s.unlinked.net_pnl} currency={currency} /> : "none"} />
      </StatStrip>
      {s.strategies.length === 0
        ? <div className="panel"><Empty title="No closed trades linked to a strategy yet">Trades count here once they close. {linkHint}</Empty></div>
        : s.strategies.map((g) => <StrategyCard key={g.key} g={g} currency={currency} />)}
      <div className="grid gap-3 lg:grid-cols-2">
        <SideGroup title="Rules not followed (risk override)" s={s.overridden} currency={currency}>
          Trades you took past a risk limit. They never count in a strategy&apos;s numbers above.
          {s.overridden.by_strategy?.length ? ` By strategy: ${s.overridden.by_strategy.map((b) => `${b.strategy} ${b.trades}`).join(", ")}.` : ""}
        </SideGroup>
        <SideGroup title="No strategy linked" s={s.unlinked} currency={currency}>Manual trades, or trades not yet linked. {linkHint}</SideGroup>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- page

export default function ForwardPage() {
  const sb = supabase();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<Forward | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [since, setSince] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => setSession(data.session));
  }, [sb]);

  const load = useCallback((from: string) => api<Forward>(`/forward${from ? `?since=${from}` : ""}`)
    .then((d) => { setData(d); setError(null); })
    .catch((e: Error) => setError(e.message))
    .finally(() => setLoading(false)), []);

  useEffect(() => {
    if (session) load("");
  }, [session, load]);

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="Forward results" />;

  return (
    <Page wide>
      <PageHeader
        title="Forward results"
        subtitle="Each strategy's closed paper trades next to what its backtest expected. This page only describes: no grades, no suggested settings."
        meta={data?.nse.since && <Badge>since {data.nse.since}</Badge>}
        actions={
          <>
            <label className="flex items-center gap-2 text-xs text-muted">
              From
              <input type="date" value={since} onChange={(e) => setSince(e.target.value)} className="field h-8 w-36" aria-label="Count trades closed from this date" />
            </label>
            <button onClick={() => { setLoading(true); load(since); }} disabled={loading} className="btn h-8"><IconRefresh size={14} />{loading ? "Loading…" : "Refresh"}</button>
          </>
        }
      />
      <Notice tone="info" title="Forward results are the honest test.">
        A backtest can fit the past by chance; paper trades taken after it cannot. A strategy whose backtest looks good but fails here
        should not go live. With fewer than {data?.min_trades ?? 30} trades a strategy is not judged at all: a handful of trades says almost nothing.
      </Notice>
      {error && <Notice tone="error">{error}</Notice>}

      {!data ? <div className="panel"><SkeletonRows rows={5} /></div> : (
        <>
          <div className="flex items-center gap-2 pt-1"><IconShield size={15} className="text-accent" /><h2 className="text-[14px] font-semibold text-fg">NSE paper trades</h2></div>
          <SectionView s={data.nse} currency="INR" minTrades={data.min_trades}
            linkHint={<>Link a trade to its strategy on the <Link href="/monitor" className="link">Trade monitor</Link>.</>} />

          <div className="flex items-center gap-2 pt-3"><IconShield size={15} className="text-accent" /><h2 className="text-[14px] font-semibold text-fg">XAUUSD paper trades</h2></div>
          {data.xauusd.available
            ? <SectionView s={data.xauusd} currency="USD" minTrades={data.min_trades}
                linkHint={<>A trade counts under a strategy when its ticket came from a <Link href="/global/signals" className="link">XAUUSD signal</Link>.</>} />
            : <Notice tone="warn">{data.xauusd.note}</Notice>}
        </>
      )}
    </Page>
  );
}
