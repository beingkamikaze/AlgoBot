"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { DailyPnlChart, EquityChart } from "@/components/equity-chart";
import { MorningBrief } from "@/components/morning-brief";
import { SignIn } from "@/components/sign-in";
import { IconArrowUpRight, IconHistory, IconShield, IconWallet } from "@/components/ui/icons";
import {
  Badge, DataSourceBadge, Empty, Meter, Notice, Num, Page, PageHeader, PageLoading, Panel, Pnl, RValue, Side, SkeletonRows, Stat, StatStrip,
  TableWrap, Term, TERMS, cx,
 px } from "@/components/ui/kit";
import {
  api, inr, type BacktestBrief, type LabData, type Me, type PaperOverview, type PaperTrade, type Quote, type StrategyInfo,
} from "@/lib/api";
import { supabase } from "@/lib/supabase";

export default function Home() {
  const sb = supabase();
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [meError, setMeError] = useState<string | null>(null);

  useEffect(() => {
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setReady(true);
    });
    const { data } = sb.auth.onAuthStateChange((_event, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, [sb]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    api<Me>("/me")
      .then((m) => !cancelled && setMe(m))
      .catch((e: Error) => !cancelled && setMeError(e.message));
    return () => {
      cancelled = true;
    };
  }, [session]);

  if (!sb)
    return (
      <Page>
        <Notice tone="warn" title="Supabase is not configured yet">
          Copy <code>web/.env.local.example</code> to <code>web/.env.local</code>, fill in your project URL and anon key, then restart <code>npm run dev</code>.
        </Notice>
      </Page>
    );
  if (!ready) return <PageLoading />;
  if (!session) return <SignInScreen />;
  return <Dashboard me={me} meError={meError} email={session.user.email ?? null} />;
}

function SignInScreen() {
  return (
    <main className="grid flex-1 place-items-center px-4 py-10">
      <div className="grid w-full max-w-[880px] overflow-hidden rounded-lg border border-line bg-surface md:grid-cols-[1.1fr_1fr]">
        <div className="hidden flex-col justify-between gap-8 border-r border-line bg-canvas/40 p-8 md:flex">
          <div className="space-y-3">
            <p className="eyebrow text-accent">NSE equity · Nifty F&amp;O</p>
            <h1 className="text-[22px] leading-tight font-semibold tracking-tight">Rule-based trading,<br />every signal explained.</h1>
            <p className="text-[13px] text-muted">Backtested strategies, a risk engine that checks every order, paper trading with Zerodha charges, and an AI analyst that only states numbers it can verify.</p>
          </div>
          <ul className="space-y-2.5 text-[12.5px] text-muted">
            {[[IconShield, "Every order passes the risk check: stop, max loss, daily limits"], [IconHistory, "Results after costs, judged on days a strategy was not tuned on"],
              [IconWallet, "Paper first; live orders need Kite, two-factor and your confirmation"]].map(([Icon, t]) => {
              const I = Icon as typeof IconShield;
              return <li key={t as string} className="flex gap-2.5"><I size={15} className="mt-0.5 shrink-0 text-accent" />{t as string}</li>;
            })}
          </ul>
        </div>
        <div className="p-6 sm:p-8">
          <h2 className="text-[15px] font-semibold">Sign in</h2>
          <p className="mt-1 mb-5 text-xs text-muted">Use the account you created for this app.</p>
          <SignIn />
        </div>
      </div>
    </main>
  );
}

// ---------------------------------------------------------------- dashboard

const IST_DAY = (iso: string) => new Date(iso).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–";
const INDICES = ["NIFTY", "BANKNIFTY", "INDIAVIX"];
const NAMES: Record<string, string> = { NIFTY: "Nifty 50", BANKNIFTY: "Nifty Bank", INDIAVIX: "India VIX" };

function readWatchlist(): string[] {
  try {
    return (JSON.parse(localStorage.getItem("tb.watchlist") ?? "[]") as string[]).filter((s) => !INDICES.includes(s)).slice(0, 6);
  } catch {
    return [];
  }
}

function performance(trades: PaperTrade[]) {
  const closed = trades.filter((t) => t.exit_ts).sort((a, b) => (a.exit_ts! < b.exit_ts! ? -1 : 1));
  let eq = 0;
  const curve = closed.map((t) => ({ t: Math.floor(new Date(t.exit_ts!).getTime() / 1000), equity: (eq += t.net_pnl ?? 0) }));
  const byDay = new Map<string, number>();
  for (const t of closed) byDay.set(IST_DAY(t.exit_ts!), (byDay.get(IST_DAY(t.exit_ts!)) ?? 0) + (t.net_pnl ?? 0));
  const nets = closed.map((t) => t.net_pnl ?? 0);
  const wins = nets.filter((n) => n > 0);
  const losses = nets.filter((n) => n <= 0);
  const gp = wins.reduce((a, b) => a + b, 0);
  const gl = -losses.reduce((a, b) => a + b, 0);
  const rs = closed.map((t) => t.r_multiple).filter((x): x is number => x != null);
  const strat = new Map<string, PaperTrade[]>();
  for (const t of closed) {
    const k = t.entry_context?.strategy ?? t.entry_context?.source ?? "manual";
    strat.set(k, [...(strat.get(k) ?? []), t]);
  }
  return {
    n: closed.length, curve, days: [...byDay].map(([day, net]) => ({ day, net: Math.round(net) })),
    winRate: closed.length ? wins.length / closed.length : null, pf: gl > 0 ? gp / gl : null,
    avgR: rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null,
    best: nets.length ? Math.max(...nets) : null, worst: nets.length ? Math.min(...nets) : null,
    total: nets.reduce((a, b) => a + b, 0),
    byStrategy: [...strat].map(([k, ts]) => {
      const r = ts.map((t) => t.r_multiple).filter((x): x is number => x != null);
      return { key: k, trades: ts.length, wins: ts.filter((t) => (t.net_pnl ?? 0) > 0).length, net: ts.reduce((a, t) => a + (t.net_pnl ?? 0), 0), avgR: r.length ? r.reduce((a, b) => a + b, 0) / r.length : null };
    }).sort((a, b) => b.net - a.net),
  };
}

function Dashboard({ me, meError, email }: { me: Me | null; meError: string | null; email: string | null }) {
  const router = useRouter();
  const [paper, setPaper] = useState<PaperOverview | null>(null);
  const [paperError, setPaperError] = useState<string | null>(null);
  const [quotes, setQuotes] = useState<Quote[] | null>(null);
  const [feedSource, setFeedSource] = useState<string | null>(null);   // "kite" when logged in to Kite today, else "yahoo"
  const [lab, setLab] = useState<LabData | null>(null);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [runs, setRuns] = useState<BacktestBrief[]>([]);

  useEffect(() => {
    const load = () => api<PaperOverview>("/paper").then((d) => { setPaper(d); setPaperError(null); }).catch((e: Error) => setPaperError(e.message));
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const syms = [...INDICES, ...readWatchlist()];
    const load = () => {
      api<Quote[]>(`/market/quotes?symbols=${encodeURIComponent(syms.join(","))}`).then(setQuotes).catch(() => setQuotes([]));
      api<{ source: string }>("/market/source").then((r) => setFeedSource(r.source)).catch(() => setFeedSource(null));
    };
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    api<LabData>("/lab").then(setLab).catch(() => {});
    api<StrategyInfo[]>("/strategies").then(setStrategies).catch(() => {});
    api<BacktestBrief[]>("/backtests").then(setRuns).catch(() => {});
  }, []);

  const perf = useMemo(() => performance(paper?.trades ?? []), [paper]);
  const a = paper?.account;
  const l = paper?.limits;
  const total = a ? a.capital - a.starting_capital : null;
  const working = paper?.orders.filter((o) => o.state === "working" || o.state === "proposed") ?? [];

  const strategyRows = strategies.map((s) => {
    const v = lab?.variants.find((x) => x.strategy_id === s.id && !x.user_id);
    const bt = runs.find((b) => b.strategy.id === s.id && b.status === "done");
    const mine = perf.byStrategy.find((x) => x.key === s.id);
    const versions = lab?.variants.filter((x) => x.strategy_id === s.id && x.user_id && x.status !== "retired").length ?? 0;
    return { s, v, bt, mine, versions };
  });

  return (
    <Page wide>
      <PageHeader
        title="Dashboard"
        subtitle={<>Paper account{email ? ` · ${email}` : ""} · figures after Zerodha charges</>}
        meta={<Badge tone="warn">{me ? me.mode : "paper"}</Badge>}
        actions={<>
          <Link href="/signals" className="btn">Find signals</Link>
          <Link href="/paper" className="btn btn-primary">Open paper desk</Link>
        </>}
      />
      {meError && <Notice tone="error" title="The backend could not verify your session">{meError}</Notice>}
      {paperError && <Notice tone="error" title="Paper account unavailable">{paperError}</Notice>}
      {a?.kill_switch && <Notice tone="error" title="Kill switch is on">No new entries until tomorrow.</Notice>}

      <StatStrip cols={5}>
        <Stat label="Capital" loading={!a} value={a && inr(a.capital)}
          sub={a && total != null && <><Pnl v={total} /> <span className="text-subtle">since {inr(a.starting_capital)} start</span></>} />
        <Stat label="Today" loading={!a} value={a && <Pnl v={a.realized_today} plain />} tone={a ? (a.realized_today > 0 ? "up" : a.realized_today < 0 ? "down" : null) : null}
          sub={a && `${a.entries_today} entr${a.entries_today === 1 ? "y" : "ies"} today`} hint="Realized P&L today, after charges" />
        <Stat label="This month" loading={!a} value={a && <Pnl v={a.realized_month} plain />} tone={a ? (a.realized_month > 0 ? "up" : a.realized_month < 0 ? "down" : null) : null}
          sub={perf.n ? `${perf.n} closed trades shown` : "no closed trades yet"} />
        <Stat label={<Term tip={TERMS.openRisk}>Open risk</Term>} loading={!a} value={a && inr(a.open_risk)}
          sub={paper && `${paper.positions.length} open position${paper.positions.length === 1 ? "" : "s"}`} />
        <Stat label="Free cash" loading={!a} value={a && inr(a.cash_free)} sub={a && a.capital > 0 && `${((a.cash_free / a.capital) * 100).toFixed(0)}% of capital`} />
      </StatStrip>

      <div className="grid gap-4 lg:grid-flow-dense lg:grid-cols-2 xl:grid-cols-12">
        <Panel className="max-lg:order-5 lg:col-span-2 xl:col-span-8" title="Equity curve" actions={<span className="text-2xs text-subtle">cumulative net P&amp;L, closed paper trades</span>}>
          {!paper ? <div className="skeleton h-56" /> : perf.n === 0 ? (
            <Empty title="No closed trades yet" icon={<IconHistory size={16} />} action={<Link href="/signals" className="btn btn-sm">Find a signal</Link>}>
              The curve starts with your first closed paper trade.
            </Empty>
          ) : <EquityChart points={perf.curve} height="h-60" />}
        </Panel>

        <Panel className="max-lg:order-2 xl:col-span-4" title="Risk limits today" actions={<Link href="/paper#limits" className="btn btn-ghost btn-sm">Edit</Link>}>
          {!a || !l ? <SkeletonRows rows={4} cols={1} /> : (
            <div className="space-y-3.5">
              <Meter label="Daily loss + open risk" used={Math.max(0, -a.realized_today) + a.open_risk} limit={l.max_daily_loss} format={inr} />
              <Meter label="Entries" used={a.entries_today} limit={l.max_trades_per_day} />
              <Meter label="Losses in a row" used={a.consecutive_losses} limit={l.max_consecutive_losses} />
              <Meter label="Open positions" used={paper!.positions.length} limit={l.max_open_positions} />
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1 border-t border-line pt-3 text-xs">
                <dt className="text-muted">Max loss / trade</dt><dd className="num text-right">{inr(l.max_loss_per_trade)}</dd>
                <dt className="text-muted">F&amp;O max loss</dt><dd className="num text-right">{l.max_loss_per_trade_fno ? inr(l.max_loss_per_trade_fno) : <span className="font-sans text-subtle">same as stocks</span>}</dd>
                <dt className="text-muted">Entry window</dt><dd className="num text-right">{l.entry_start}–{l.entry_end}</dd>
                <dt className="text-muted">Flat by</dt><dd className="num text-right">{l.flat_by}</dd>
              </dl>
            </div>
          )}
        </Panel>

        <Panel className="max-lg:order-1 lg:col-span-2 xl:col-span-8" title={<div className="flex items-center gap-2"><h2 className="panel-title">Open positions</h2>{paper && <Badge>{paper.positions.length}</Badge>}</div>}
          actions={<Link href="/paper" className="btn btn-ghost btn-sm">Manage<IconArrowUpRight size={13} /></Link>} flush>
          {!paper ? <SkeletonRows rows={3} /> : paper.positions.length === 0 ? (
            <Empty title="No open positions">{working.length ? `${working.length} order${working.length === 1 ? "" : "s"} working or waiting for your confirmation.` : "Positions appear here once a paper order fills."}</Empty>
          ) : (
            <TableWrap>
              <table className="dtable">
                <thead><tr><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Last</th><th className="r">Stop</th><th className="r">Target</th><th className="r">Unrealized</th><th>Since</th></tr></thead>
                <tbody>
                  {paper.positions.map((t) => {
                    const opt = (t.entry_context as { kind?: string } | null)?.kind === "option";
                    return (
                      <tr key={t.id} title={t.entry_context?.summary ?? undefined} className="cursor-pointer" onClick={() => router.push(`/monitor?trade=${t.id}`)}>
                        <td className="font-medium"><Link href={`/monitor?trade=${t.id}`} className="hover:text-accent">{t.symbol}</Link></td><td><Side side={t.side} /></td>
                        <td className="r">{t.qty}</td><td className="r">{px(t.entry)}</td><td className="r">{px(t.last)}</td>
                        <td className="r text-down/90">{opt ? inr(t.stop ?? 0) : px(t.stop)}</td><td className="r text-up/90">{opt ? inr(t.target ?? 0) : px(t.target)}</td>
                        <td className="r"><Pnl v={t.unrealized} /></td><td className="text-muted">{when(t.entry_ts)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
          )}
        </Panel>

        <Panel className="max-lg:order-6 xl:col-span-4" title="Markets" actions={<DataSourceBadge source={feedSource} />} flush>
          {!quotes ? <SkeletonRows rows={5} cols={3} /> : quotes.length === 0 ? <Empty title="Quotes unavailable">No prices came back. Try again in a minute.</Empty> : (
            <ul className="divide-y divide-line/70 lg:max-h-[268px] lg:overflow-y-auto">
              {quotes.map((q) => {
                const up = (q.change_pct ?? 0) >= 0;
                return (
                  <li key={q.symbol}>
                    <Link href={`/chart?symbol=${encodeURIComponent(q.symbol)}`} className="grid grid-cols-[1fr_auto_4.5rem] items-baseline gap-3 px-3 py-1.5 transition-colors hover:bg-hover">
                      <p className="min-w-0 truncate"><span className="text-[13px] font-medium">{q.symbol}</span> <span className="text-2xs text-subtle">{NAMES[q.symbol] ?? ""}</span></p>
                      <p className="num text-right text-[13px]">{q.last.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
                      <p className={cx("num text-right text-xs", q.change_pct == null ? "text-subtle" : up ? "text-up" : "text-down")}>{q.change_pct == null ? "–" : `${up ? "+" : "−"}${Math.abs(q.change_pct).toFixed(2)}%`}</p>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel className="max-lg:order-8 xl:col-span-4" title="Daily P&L" actions={<span className="text-2xs text-subtle">net, by exit day</span>}>
          {!paper ? <div className="skeleton h-40" /> : perf.days.length === 0 ? <Empty title="Nothing closed yet" /> : <DailyPnlChart days={perf.days} />}
        </Panel>

        <Panel className="max-lg:order-7 xl:col-span-4" title="Performance" actions={<span className="text-2xs text-subtle">closed paper trades</span>}>
          {!paper ? <SkeletonRows rows={4} cols={2} /> : (
            <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line">
              {([
                ["Trades", <Num key="n">{perf.n}</Num>],
                [<Term key="w" tip={TERMS.winRate}>Win rate</Term>, <Num key="w">{perf.winRate == null ? "–" : `${(perf.winRate * 100).toFixed(1)}%`}</Num>],
                [<Term key="p" tip={TERMS.pf}>Profit factor</Term>, <Num key="p" className={perf.pf == null ? "" : perf.pf >= 1 ? "text-up" : "text-down"}>{perf.pf?.toFixed(2) ?? "–"}</Num>],
                [<Term key="r" tip={TERMS.expectancy}>Average R</Term>, <RValue key="r" v={perf.avgR} />],
                ["Best trade", <Pnl key="b" v={perf.best} />],
                ["Worst trade", <Pnl key="x" v={perf.worst} />],
              ] as [React.ReactNode, React.ReactNode][]).map(([k, v], i) => (
                <div key={i} className="bg-surface px-3 py-2.5"><dt className="eyebrow">{k}</dt><dd className="mt-0.5 text-[15px]">{v}</dd></div>
              ))}
            </dl>
          )}
        </Panel>

        <Panel className="max-lg:order-9 xl:col-span-4" title="Strategies" actions={<Link href="/lab" className="btn btn-ghost btn-sm">Lab<IconArrowUpRight size={13} /></Link>} flush>
          {strategies.length === 0 ? <SkeletonRows rows={4} cols={3} /> : (
            <TableWrap>
              <table className="dtable dtable-compact">
                <thead><tr><th>Strategy</th><th className="r" title="Average R per trade in the latest finished backtest">Backtest</th><th className="r">Paper</th><th className="r">Your avg</th></tr></thead>
                <tbody>
                  {strategyRows.map(({ s, bt, mine, versions }) => (
                    <tr key={s.id}>
                      <td className="min-w-36 whitespace-normal">
                        <p className="leading-snug font-medium" title={s.description}>{s.name}</p>
                        {versions > 0 && <p className="text-2xs text-accent">{versions} lab version{versions > 1 ? "s" : ""}</p>}
                      </td>
                      <td className="r">{bt ? <><RValue v={bt.expectancy_r} /><p className="text-2xs text-subtle" title="Universe of the latest finished backtest">{bt.universe}</p></> : <span className="text-subtle">–</span>}</td>
                      <td className="r text-muted">{mine?.trades ?? 0}</td>
                      <td className="r"><RValue v={mine?.avgR ?? null} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </Panel>

        <Panel className="max-lg:order-3 lg:col-span-2 xl:col-span-8" title="Recent trades" actions={<Link href="/journal" className="btn btn-ghost btn-sm">Journal<IconArrowUpRight size={13} /></Link>} flush>
          {!paper ? <SkeletonRows rows={5} /> : paper.trades.length === 0 ? <Empty title="No closed trades yet">Paper-trade a signal; it shows here when it closes, with its post-mortem in the Journal.</Empty> : (
            <TableWrap max="360px">
              <table className="dtable">
                <thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Exit</th><th>Exit reason</th><th className="r">Net</th><th className="r"><Term tip={TERMS.R}>R</Term></th><th>Strategy</th></tr></thead>
                <tbody>
                  {paper.trades.slice(0, 25).map((t) => (
                    <tr key={t.id} title={t.entry_context?.summary ?? undefined}>
                      <td className="text-muted">{when(t.exit_ts)}</td><td className="font-medium">{t.symbol}</td><td><Side side={t.side} /></td>
                      <td className="r">{t.qty}</td><td className="r">{px(t.entry)}</td><td className="r">{px(t.exit)}</td>
                      <td className="text-muted">{t.exit_reason}</td><td className="r"><Pnl v={t.net_pnl} /></td><td className="r"><RValue v={t.r_multiple} /></td>
                      <td className="text-muted">{t.entry_context?.strategy ?? t.entry_context?.source ?? "manual"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </Panel>

        <MorningBrief className="max-lg:order-10 xl:col-span-4" />
      </div>
    </Page>
  );
}
