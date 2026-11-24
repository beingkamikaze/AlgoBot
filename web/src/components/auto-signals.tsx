"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { FnoPanel } from "@/components/fno-panel";
import { SignalCard } from "@/components/signal-card";
import { IconAlert, IconChevronDown, IconChevronRight, IconRefresh, IconSearch } from "@/components/ui/icons";
import { Badge, DataSourceBadge, Empty, Notice, RValue, Side, SkeletonRows, Stat, StatStrip, TableWrap, Term, TERMS, px } from "@/components/ui/kit";
import { api, type AutoScanJob, type TradeIdea } from "@/lib/api";

const CONF_TONE = { high: "up", medium: "info", low: "warn", "no track record": "down" } as const;
const CONF_RANK = { high: 3, medium: 2, low: 1, "no track record": 0 } as const;
const STORE = "tb.autoscan.job";
const SHORT: Record<string, string> = { orb: "ORB", vwap_pullback: "VWAP", supertrend: "ST", pdh_pdl: "PDH/PDL" };
const REFRESH_MS = 5 * 60_000;

const time = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" });

function readJob(): string | null {
  try { return sessionStorage.getItem(STORE); } catch { return null; }
}
function saveJob(id: string) {
  try { sessionStorage.setItem(STORE, id); } catch { /* storage blocked: the scan just is not restored */ }
}

/** Every strategy (default + your approved lab versions) on the Nifty 100 and index futures, as one ranked list of trade ideas. */
export function AutoSignals() {
  const [job, setJob] = useState<AutoScanJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [at, setAt] = useState("");
  const [minConf, setMinConf] = useState<0 | 2 | 3>(0);
  const [freshOnly, setFreshOnly] = useState(false);
  const [side, setSide] = useState<"all" | "buy" | "sell">("all");
  const [hideConflicts, setHideConflicts] = useState(false);
  const [strategy, setStrategy] = useState("all");
  const [segment, setSegment] = useState<"all" | "stock" | "fno" | "index">("all");
  const [open, setOpen] = useState<string | null>(null);
  const [shown, setShown] = useState(40);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const started = useRef(false);

  const poll = useCallback((id: string) => {
    const tick = async () => {
      try {
        const j = await api<AutoScanJob>(`/signals/autoscan/${id}`);
        setJob(j);
        if (j.status === "running") setTimeout(tick, 2500);
      } catch (e) {
        setError((e as Error).message);
      }
    };
    tick();
  }, []);

  const run = useCallback(async (replayAt?: string) => {
    setError(null);
    try {
      const j = await api<AutoScanJob>("/signals/autoscan", { method: "POST", body: { at: replayAt || null } });
      saveJob(j.id);
      setJob(j);
      if (j.status === "running") poll(j.id);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [poll]);

  // restore this tab's last sweep, or start one: the page opens with a ranked list, no choices needed
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const id = readJob();
    if (!id) { queueMicrotask(() => run()); return; }
    api<AutoScanJob>(`/signals/autoscan/${id}`)
      .then((j) => { setJob(j); if (j.status === "running") poll(j.id); })
      .catch(() => run());
  }, [run, poll]);

  // optional: sweep again every 5 minutes while the market is open
  useEffect(() => {
    if (!autoRefresh || !job?.market_open_now || job.replay) return;
    const id = setInterval(() => run(), REFRESH_MS);
    return () => clearInterval(id);
  }, [autoRefresh, job?.market_open_now, job?.replay, run]);

  const ideas = useMemo(() => (job?.ideas ?? []).filter((i) =>
    CONF_RANK[i.confidence] >= minConf && (!freshOnly || i.fresh) && (side === "all" || i.side === side) &&
    (!hideConflicts || !i.conflict) && (strategy === "all" || i.strategies.includes(strategy)) &&
    (segment === "all" || (segment === "index" ? i.segment === "index" : segment === "fno" ? i.segment === "stock" && i.fno : i.segment === "stock"))),
  [job, minConf, freshOnly, side, hideConflicts, strategy, segment]);
  const indexIdeas = useMemo(() => (job?.ideas ?? []).filter((i) => i.segment === "index"), [job]);

  const counts = useMemo(() => {
    const c = { high: 0, medium: 0, low: 0, "no track record": 0 } as Record<TradeIdea["confidence"], number>;
    for (const i of job?.ideas ?? []) c[i.confidence] += 1;
    return c;
  }, [job]);
  const strategyNames = useMemo(() => Object.fromEntries((job?.plan ?? []).map((p) => [p.strategy, p.name])), [job]);
  const running = job?.status === "running";
  const pct = job ? (job.progress.done / Math.max(1, job.progress.total)) * 100 : 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="panel flex flex-wrap items-end gap-3 p-3">
        <div className="w-full min-w-0 md:w-auto md:flex-1">
          <p className="text-[13px] font-medium text-fg">Every strategy, every universe, ranked</p>
          <p className="text-xs text-muted">
            Stocks on the Nifty 100 and Nifty / Bank Nifty futures, with each strategy&apos;s default parameters and every Lab version you
            approved. Parameters are never tuned at scan time: that would fit noise.
          </p>
        </div>
        <label className="w-36">
          <span className="field-label">Replay today at <span className="text-subtle">(IST)</span></span>
          <input type="time" value={at} onChange={(e) => setAt(e.target.value)} className="field num" />
        </label>
        <label className="flex h-8 items-center gap-2 text-xs text-muted" title="Sweep again every 5 minutes while the market is open">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />Refresh every 5 min
        </label>
        <button onClick={() => run(at)} disabled={running} className="btn btn-primary min-w-36">
          {running ? <><IconRefresh size={14} className="animate-spin" />Scanning…</> : <><IconSearch size={14} />Scan everything</>}
        </button>
        {running && (
          <div className="w-full space-y-1">
            <div className="h-1 overflow-hidden rounded-full bg-raised"><div className="h-full rounded-full bg-accent transition-[width] duration-500" style={{ width: `${Math.max(4, pct)}%` }} /></div>
            <p className="num text-2xs text-subtle">{job!.progress.done} / {job!.progress.total} strategy scans · about a minute on free data</p>
          </div>
        )}
      </div>

      {error && <Notice tone="error" title="Scan failed">{error}</Notice>}
      {job?.status === "failed" && <Notice tone="error" title="Scan failed">{job.errors.slice(-1)[0]}</Notice>}

      {job?.status === "done" && (
        <>
          {job.data_source && <p className="flex items-center gap-2 text-2xs text-subtle">Prices for this scan <DataSourceBadge source={job.data_source} /></p>}
          <StatStrip cols={5}>
            <Stat label="Trade ideas" value={job.ideas.length} sub={`${job.signal_count} signals from ${job.raw_count} raw`} />
            <Stat label="High confidence" value={counts.high} tone={counts.high ? "up" : null} />
            <Stat label="Medium" value={counts.medium} />
            <Stat label="Low" value={counts.low} sub="negative record after costs" />
            <Stat label="Session" value={job.session ?? "–"}
              sub={`${job.replay ? "replay to " : job.market_open_now ? "live, " : "closed, "}${job.as_of ? time(job.as_of) : ""} · ${job.took_s}s`} />
          </StatStrip>
          {counts.high + counts.medium === 0 && job.ideas.length > 0 && (
            <Notice tone="warn" title="No idea has a positive record after costs">
              Every signal below comes from a strategy whose backtest loses money after charges, so all are graded low or no track record.
              Treat them as practice on paper, not trades to take.
            </Notice>
          )}
        </>
      )}

      {job?.status === "done" && (
        <div className="panel flex flex-wrap items-center gap-2 p-2">
          <div className="seg" role="group" aria-label="Minimum confidence">
            {([[0, "All"], [2, "Medium +"], [3, "High"]] as const).map(([v, label]) => (
              <button key={v} aria-pressed={minConf === v} onClick={() => setMinConf(v)}>{label}</button>
            ))}
          </div>
          <div className="seg" role="group" aria-label="Side">
            {([["all", "Both"], ["buy", "Buy"], ["sell", "Sell"]] as const).map(([v, label]) => (
              <button key={v} aria-pressed={side === v} onClick={() => setSide(v)}>{label}</button>
            ))}
          </div>
          <div className="seg" role="group" aria-label="Segment">
            {([["all", "All"], ["stock", "Stocks"], ["fno", "F&O stocks"], ["index", "Index"]] as const).map(([v, label]) => (
              <button key={v} aria-pressed={segment === v} onClick={() => setSegment(v)}>{label}</button>
            ))}
          </div>
          <button aria-pressed={freshOnly} onClick={() => setFreshOnly((x) => !x)} className="chip" title="Only signals on the latest bar: price has not moved on yet">Fresh only</button>
          <button aria-pressed={hideConflicts} onClick={() => setHideConflicts((x) => !x)} className="chip" title="Hide stocks where another strategy says the opposite">Hide conflicts</button>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)} className="field h-7 w-auto text-xs" aria-label="Strategy">
            <option value="all">All strategies</option>
            {Object.entries(strategyNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
          <span className="ml-auto text-xs text-muted">{ideas.length} of {job.ideas.length} ideas</span>
        </div>
      )}

      {job?.status === "done" && <IndexToday ideas={indexIdeas} names={strategyNames} />}

      <div className="panel overflow-hidden">
        {!job || (running && !job.ideas.length) ? <SkeletonRows rows={8} cols={7} /> : job.status === "done" && ideas.length === 0 ? (
          <Empty title={job.ideas.length ? "No idea matches these filters" : "No signals in this session"} icon={<IconSearch size={16} />}>
            {job.ideas.length ? "Loosen a filter above." : "No strategy fired on any stock or future in this session so far."}
          </Empty>
        ) : job.status === "done" && (
          <>
            <ul className="divide-y divide-line md:hidden">
              {ideas.slice(0, shown).map((i) => {
                const key = `${i.symbol}-${i.side}`;
                const isOpen = open === key;
                return (
                  <li key={key}>
                    <button onClick={() => setOpen(isOpen ? null : key)} aria-expanded={isOpen}
                      className="flex w-full cursor-pointer flex-col gap-1 px-3 py-2.5 text-left transition-colors hover:bg-hover">
                      <span className="flex w-full items-center gap-2">
                        <span className="num text-2xs text-subtle">{i.rank}</span>
                        <span className="num text-[14px] font-semibold">{i.symbol}</span>
                        {i.segment === "stock" && i.fno && <span className="badge badge-neutral">F&amp;O</span>}
                        {i.conflict && <IconAlert size={13} className="text-warn" label="conflict: another strategy says the opposite" />}
                        <span className="ml-auto flex items-center gap-1.5"><Side side={i.side} /><Badge tone={CONF_TONE[i.confidence]}>{i.confidence}</Badge></span>
                      </span>
                      <span className="flex w-full items-center gap-2 text-xs text-muted">
                        <span className="num text-fg">{px(i.best.explanation.plan.entry)}</span>
                        <span>{i.strategies.length > 1 ? `${i.strategies.length} strategies` : SHORT[i.strategies[0]] ?? i.strategies[0]}</span>
                        <span className="ml-auto num">{time(i.best.created_at)}</span>
                        {i.fresh && <span className="badge badge-accent">fresh</span>}
                      </span>
                    </button>
                    {isOpen && (
                      <div className="flex flex-col gap-3 bg-canvas/60 p-2">
                        {i.fno && <IdeaFno i={i} />}
                        {i.signals.map((s) => <SignalCard key={`${s.strategy_id}-${s.strategy_version}`} s={s} />)}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
            <div className="max-md:hidden">
            <TableWrap>
              <table className="dtable dtable-compact [&_tbody>tr:not(.idea-detail)>td]:h-9">
                <thead>
                  <tr>
                    <th className="w-6"></th><th className="r w-10 max-md:hidden">#</th><th>Symbol</th><th>Side</th><th>Confidence</th><th className="max-md:hidden">Strategies</th>
                    <th className="r">Entry</th><th className="r max-md:hidden">Stop</th><th className="r max-md:hidden">Target</th><th className="r max-md:hidden">R : R</th>
                    <th className="r max-md:hidden"><Term tip={TERMS.expectancy}>Record</Term></th><th>Signal</th>
                  </tr>
                </thead>
                <tbody>
                  {ideas.slice(0, shown).map((i) => {
                    const p = i.best.explanation.plan;
                    const risk = Math.abs(p.entry - p.stop);
                    const rr = p.target != null && risk > 0 ? Math.abs(p.target - p.entry) / risk : null;
                    const key = `${i.symbol}-${i.side}`;
                    const isOpen = open === key;
                    return (
                      <Fragment key={key}>
                        <tr onClick={() => setOpen(isOpen ? null : key)} className="cursor-pointer" aria-expanded={isOpen}>
                          <td className="text-subtle">{isOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}</td>
                          <td className="r text-muted max-md:hidden">{i.rank}</td>
                          <td className="font-semibold">
                            <span className="num">{i.symbol}</span>
                            {i.segment === "stock" && i.fno && <span className="badge badge-neutral ml-2" title="Has futures and options on NSE: open the row for the exact contracts">F&amp;O</span>}
                            {i.conflict && (
                              <span className="badge badge-warn ml-2 max-md:px-1" title="Another strategy flags this stock in the opposite direction">
                                <IconAlert size={11} className="md:hidden" label="conflict" /><span className="max-md:hidden">conflict</span>
                              </span>
                            )}
                          </td>
                          <td><Side side={i.side} /></td>
                          <td><Badge tone={CONF_TONE[i.confidence]}>{i.confidence}</Badge></td>
                          <td className="max-md:hidden">
                            <div className="flex items-center gap-1.5 whitespace-nowrap" title={i.strategies.map((s) => strategyNames[s] ?? s).join(", ")}>
                              {i.strategies.length > 1 && <span className="badge badge-accent">{i.strategies.length}×</span>}
                              <span className="num text-xs text-muted">{i.strategies.map((s) => SHORT[s] ?? s).join(" · ")}</span>
                              {i.signals.some((s) => s.version_label === "lab") && <span className="badge badge-info">lab</span>}
                            </div>
                          </td>
                          <td className="r">{px(p.entry)}</td><td className="r text-down max-md:hidden">{px(p.stop)}</td><td className="r text-up max-md:hidden">{px(p.target)}</td>
                          <td className="r max-md:hidden">{rr == null ? "–" : rr.toFixed(2)}</td>
                          <td className="r max-md:hidden" title={`${i.best.rank_info.evidence_trades} backtest trades`}><RValue v={i.best.rank_info.evidence_r} /></td>
                          <td className="whitespace-nowrap">
                            <span className="num text-xs">{time(i.best.created_at)}</span>{" "}
                            {i.fresh ? <span className="badge badge-accent">fresh</span> : <span className="text-2xs text-subtle">{i.best.bars_ago} bars ago</span>}
                          </td>
                        </tr>
                        {isOpen && (
                          <tr className="idea-detail hover:bg-transparent">
                            <td colSpan={12} className="bg-canvas/60 p-3 whitespace-normal">
                              <div className="flex flex-col gap-3">
                                {i.fno && <IdeaFno i={i} />}
                                {i.signals.map((s) => <SignalCard key={`${s.strategy_id}-${s.strategy_version}`} s={s} />)}
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
            </div>
            {ideas.length > shown && (
              <div className="border-t border-line p-2 text-center"><button onClick={() => setShown((n) => n + 40)} className="btn btn-ghost btn-sm">Show 40 more</button></div>
            )}
          </>
        )}
      </div>
      {job?.status === "done" && job.errors.length > 0 && <p className="text-xs text-subtle">Skipped: {job.errors.slice(0, 5).join("; ")}{job.errors.length > 5 ? ` and ${job.errors.length - 5} more` : ""}</p>}
      <p className="text-2xs text-subtle">
        Ranked by confidence (a fixed rule from each strategy&apos;s record after costs), then how many strategies agree, then fresh signals, then the record.
        Click a row for each strategy&apos;s full card: the rule, the plan, the evidence, the risks, and the paper-trade button.
      </p>
    </div>
  );
}


/** For one trade idea: the exact futures contract and option to trade, using its best signal's levels. */
function IdeaFno({ i }: { i: TradeIdea }) {
  const p = i.best.explanation.plan;
  return (
    <div className="panel p-3">
      <p className="eyebrow mb-2">{i.symbol.replace("-FUT", "")} in futures and options</p>
      <FnoPanel symbol={i.symbol} side={i.side} entry={p.entry} stop={p.stop} target={p.target} signal={i.best} />
    </div>
  );
}

/** "Which option to trade today": every Nifty / Bank Nifty idea with its exact option and future. */
function IndexToday({ ideas, names }: { ideas: TradeIdea[]; names: Record<string, string> }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Nifty and Bank Nifty options today</h2>
        <span className="ml-auto text-2xs text-subtle">from index signals: buy a call on a buy signal, a put on a sell signal</span>
      </div>
      {ideas.length === 0 ? (
        <p className="px-3 py-3 text-xs text-muted">
          No strategy fired on Nifty or Bank Nifty in this session, so there is no index option to trade from a signal right now.
          It appears here, with the exact contract, as soon as one does. To build any option position yourself, use the Options page.
        </p>
      ) : (
        <div className="divide-y divide-line">
          {ideas.map((i) => {
            const p = i.best.explanation.plan;
            return (
              <div key={`${i.symbol}-${i.side}`} className="space-y-2 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="num text-[14px] font-semibold">{i.symbol.replace("-FUT", "")}</span>
                  <Side side={i.side} />
                  <Badge tone={CONF_TONE[i.confidence]}>{i.confidence}</Badge>
                  <span className="text-xs text-muted">{i.strategies.map((s) => names[s] ?? s).join(" · ")} · {time(i.best.created_at)}{i.fresh ? " · fresh" : ` · ${i.best.bars_ago} bars ago`}</span>
                  <span className="num ml-auto text-xs text-muted">index {px(p.entry)} · stop {px(p.stop)} · target {px(p.target)}</span>
                </div>
                <FnoPanel symbol={i.symbol} side={i.side} entry={p.entry} stop={p.stop} target={p.target} compact signal={i.best} />
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
