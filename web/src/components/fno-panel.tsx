"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { OrderTicket } from "@/components/order-ticket";
import { useFeedback } from "@/components/ui/feedback";
import { IconAlert, IconLayers } from "@/components/ui/icons";
import { Badge, Notice, Pnl, Side, Term, TERMS, cx, px } from "@/components/ui/kit";
import { api, inr, type FnoPlan, type PaperOrder, type SignalCard } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const date = (iso: string) => new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" });

function Fits({ fits, limit, need }: { fits: boolean | null; limit: number | null; need: number }) {
  if (fits == null) return null;
  return fits
    ? <span className="text-xs text-up">within your F&amp;O max loss ({inr(limit ?? 0)})</span>
    : <span className="text-xs text-warn">over your F&amp;O max loss ({inr(limit ?? 0)}): the risk check needs about {inr(Math.ceil(need / 100) * 100)} · <Link href="/paper#limits" className="link">limits</Link></span>;
}

/** The signal as futures and options: the exact contract to trade, its cost, and its estimated result at the signal's stop and target. */
export function FnoPanel({ symbol, side, entry, stop, target, compact = false, signal }: {
  symbol: string; side: "buy" | "sell"; entry: number; stop: number; target: number | null; compact?: boolean;
  signal?: SignalCard;      // the ticket keeps the signal: its strategy, rule and levels (the trade monitor uses them)
}) {
  const { toast } = useFeedback();
  const [plan, setPlan] = useState<FnoPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState<PaperOrder | null>(null);
  const [busy, setBusy] = useState<"future" | "option" | null>(null);

  useEffect(() => {
    let stop_ = false;
    const q = new URLSearchParams({ symbol, side, entry: String(entry), stop: String(stop) });
    if (target != null) q.set("target", String(target));
    api<FnoPlan>(`/fno/plan?${q}`).then((p) => !stop_ && setPlan(p)).catch((e: Error) => !stop_ && setError(e.message));
    return () => { stop_ = true; };
  }, [symbol, side, entry, stop, target]);

  async function paper(kind: "future" | "option") {
    if (!plan) return;
    if (!(await supabase()?.auth.getSession())?.data.session) { toast("error", "Sign in first", "Paper trading uses your account."); return; }
    setBusy(kind);
    try {
      const o = kind === "future"
        ? await api<PaperOrder>("/paper/tickets/manual", { method: "POST", body: { ...plan.future!.ticket, signal } })
        : await api<PaperOrder>("/paper/tickets/option", { method: "POST", body: { ...plan.option!.ticket, signal } });
      setTicket(o);
    } catch (e) {
      toast("error", "Could not make the ticket", (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (error) return <Notice tone="error">{error}</Notice>;
  if (!plan) return <div className="space-y-2" aria-busy="true"><div className="skeleton h-4 w-48" /><div className="skeleton h-16" /><div className="skeleton h-16" /></div>;
  if (!plan.available) return <p className="text-xs text-muted">{plan.reason}</p>;

  const f = plan.future, o = plan.option;
  const buyCall = side === "buy";
  const failed = !!plan.setup_failed;
  return (
    <div className="space-y-3 text-[12.5px]">
      {!compact && <p className="text-xs text-muted">{plan.note}</p>}
      {failed && (
        <Notice tone="warn" title="This setup has already failed">
          {plan.underlying} is at <span className="num text-fg">{px(plan.spot)}</span>, beyond the signal&apos;s stop at <span className="num text-fg">{px(stop)}</span>.
          There is nothing to trade from it now; the contracts below are shown for reference only.
        </Notice>
      )}

      {o ? (
        <div className="overflow-hidden rounded-md border border-line bg-canvas/60">
          <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
            <span className="eyebrow">Option</span>
            <span className="badge badge-up">Buy</span>
            <span className="num text-[13.5px] font-semibold">{o.contract}</span>
            <span className="text-xs text-muted">{buyCall ? "call" : "put"} · strike {o.strike} · expires {date(o.expiry)} ({o.days_to_expiry}d)</span>
            {o.source === "modeled" && <Badge tone="warn">modeled</Badge>}
          </div>
          <dl className="grid grid-cols-2 gap-px bg-line sm:grid-cols-4">
            {([
              ["Premium (ask)", `${px(o.ask)}`, `bid ${px(o.bid)} · spread ${o.spread_pct ?? "–"}%`],
              ["1 lot", inr(o.cost), `${o.qty} = ${o.lot_size} × 1 lot`],
              [`If ${plan.underlying} hits stop`, failed ? <span key="s" className="text-subtle">setup failed</span> : <Pnl key="s" v={o.pnl_at_stop} />, failed ? "price is past the stop" : `est. premium ${px(o.est_at_stop)}`],
              [`If it hits target`, <Pnl key="t" v={o.pnl_at_target} />, `est. premium ${px(o.est_at_target)}`],
            ] as [string, React.ReactNode, string][]).map(([k, v, sub]) => (
              <div key={k} className="bg-surface px-3 py-2">
                <dt className="eyebrow truncate">{k}</dt><dd className="num mt-0.5 text-[14px]">{v}</dd><p className="num truncate text-2xs text-subtle">{sub}</p>
              </div>
            ))}
          </dl>
          <div className="space-y-1 px-3 py-2 text-xs">
            <p className="text-muted">
              <Term tip={TERMS.delta}>Delta</Term> <span className="num text-fg">{o.delta ?? "–"}</span> ·
              <Term tip={TERMS.iv}> IV</Term> <span className="num text-fg">{o.iv ?? "–"}%</span> ·
              time decay <span className="num text-fg">{o.theta_per_day != null ? inr(o.theta_per_day) : "–"}</span>/day ·
              OI <span className="num text-fg">{o.oi ?? "–"}</span> · max loss <span className="num text-fg">{inr(o.max_loss)}</span> (the premium)
            </p>
            <p className="text-muted">The paper ticket exits at the signal&apos;s stop or target on {plan.underlying}, at 15:15, or if the premium falls 50% (<span className="num">{inr(o.planned_loss)}</span>: what the risk check counts).</p>
            <Fits fits={o.fits_limit} limit={o.limit} need={o.planned_loss} />
            {o.warnings.map((w) => <p key={w} className="flex gap-1.5 text-warn"><IconAlert size={13} className="mt-0.5 shrink-0" />{w}</p>)}
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-2">
            <button disabled={failed || busy !== null || ticket !== null} onClick={() => paper("option")} className="btn btn-sm btn-primary">{busy === "option" ? "Checking risk…" : `Paper trade ${o.contract}`}</button>
            <Link href={`/options?underlying=${plan.underlying}&template=${o.ticket.template}&ustop=${stop}${target != null ? `&utarget=${target}` : ""}&side=${side}`} className="btn btn-sm btn-ghost"><IconLayers size={13} />Other strikes and spreads</Link>
            <span className="ml-auto text-2xs text-subtle">NSE {o.as_of}</span>
          </div>
        </div>
      ) : plan.option_error && <p className="text-xs text-muted">Option: {plan.option_error}</p>}

      {f ? (
        <div className="overflow-hidden rounded-md border border-line bg-canvas/60">
          <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
            <span className="eyebrow">Future</span>
            <Side side={side} />
            <span className="num text-[13.5px] font-semibold">{f.contract}</span>
            <span className="text-xs text-muted">1 lot = {f.lot_size} · expires {f.expiry ? date(f.expiry) : "–"}</span>
          </div>
          <dl className="grid grid-cols-2 gap-px bg-line sm:grid-cols-4">
            {([
              ["Exposure", inr(f.notional), "leveraged"],
              ["Margin (est.)", inr(f.margin), "blocked while open"],
              ["Loss at stop", <Pnl key="l" v={-f.loss_at_stop} />, "incl. charges"],
              ["Gain at target", <Pnl key="g" v={f.gain_at_target} />, "after charges"],
            ] as [string, React.ReactNode, string][]).map(([k, v, sub]) => (
              <div key={k} className="bg-surface px-3 py-2"><dt className="eyebrow">{k}</dt><dd className="num mt-0.5 text-[14px]">{v}</dd><p className="text-2xs text-subtle">{sub}</p></div>
            ))}
          </dl>
          <div className="space-y-1 px-3 py-2 text-xs">
            <p className="text-muted">{f.price_note}.</p>
            <Fits fits={f.fits_limit} limit={f.limit} need={f.loss_at_stop} />
          </div>
          <div className="border-t border-line px-3 py-2">
            <button disabled={failed || busy !== null || ticket !== null} onClick={() => paper("future")} className={cx("btn btn-sm", side === "buy" ? "btn-buy" : "btn-danger")}>
              {busy === "future" ? "Checking risk…" : `Paper ${side} ${f.contract}`}
            </button>
          </div>
        </div>
      ) : plan.future_error && <p className="text-xs text-muted">Future: {plan.future_error}</p>}

      {ticket && (
        <OrderTicket order={ticket} onDone={(done) => {
          setTicket(null);
          if (done?.state === "working") toast("success", "Paper order working", "See Paper trading.");
          else if (done?.state === "rejected" && done.risk_verdict?.verdict === "BLOCK") toast("error", "Blocked by the risk check", done.risk_verdict.reasons.join("; "));
        }} />
      )}
    </div>
  );
}
