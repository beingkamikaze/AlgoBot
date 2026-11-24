"use client";

import Link from "next/link";
import { useState } from "react";

import { FnoPanel } from "@/components/fno-panel";
import { OrderTicket } from "@/components/order-ticket";
import { useFeedback } from "@/components/ui/feedback";
import { IconCandles, IconClock, IconLayers, IconSparkle } from "@/components/ui/icons";
import { Check, RValue, Term, TERMS, cx } from "@/components/ui/kit";
import { api, inr, pct, type AiSummary, type PaperOrder, type SignalCard as Card, type SimilarFromJournal } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const CONFIDENCE: Record<Card["explanation"]["confidence"], string> = {
  high: "badge-up",
  medium: "badge-info",
  low: "badge-warn",
  "no track record": "badge-down",
};

function Section({ title, children, className }: { title: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cx("min-w-0 space-y-1.5 p-3", className)}>
      <h4 className="eyebrow">{title}</h4>
      {children}
    </section>
  );
}

const n = (x: number | null | undefined) => (x == null ? "–" : x.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

export function SignalCard({ s }: { s: Card }) {
  const { toast } = useFeedback();
  const [ticket, setTicket] = useState<PaperOrder | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function paperTrade() {
    setBusy(true);
    setResult(null);
    try {
      const session = (await supabase()?.auth.getSession())?.data.session;
      if (!session) {
        setResult("Sign in on the dashboard first.");
        return;
      }
      setTicket(await api<PaperOrder>("/paper/tickets/from-signal", { method: "POST", body: { signal: s } }));
    } catch (err) {
      setResult((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const [ai, setAi] = useState<AiSummary | null>(null);
  const [mine, setMine] = useState<SimilarFromJournal | null>(null);
  const [aiBusy, setAiBusy] = useState<"summary" | "similar" | null>(null);
  const [fno, setFno] = useState(false);

  async function aiCall(kind: "summary" | "similar") {
    setAiBusy(kind);
    setResult(null);
    try {
      if (kind === "summary") setAi(await api<AiSummary>("/ai/summary", { method: "POST", body: { signal: s } }));
      else setMine(await api<SimilarFromJournal>("/journal/similar", { method: "POST", body: { signal: s } }));
    } catch (err) {
      setResult((err as Error).message);
    } finally {
      setAiBusy(null);
    }
  }
  const e = s.explanation;
  const p = e.plan;
  const time = new Date(s.created_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" });
  const buy = s.side === "buy";
  const risk = Math.abs(p.entry - p.stop);
  const reward = p.target != null ? Math.abs(p.target - p.entry) : null;
  return (
    <article className="panel overflow-hidden">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-3 py-2.5">
        <span className={cx("h-6 w-1 rounded-full", buy ? "bg-up" : "bg-down")} aria-hidden />
        <span className={cx("badge", buy ? "badge-up" : "badge-down")}>{buy ? "Buy" : "Sell"}</span>
        <h3 className="num text-[15px] font-semibold tracking-tight">{s.symbol}</h3>
        <span className="flex items-center gap-1 text-xs text-muted"><IconClock size={13} />{time} IST</span>
        {s.fresh ? <span className="badge badge-accent">Fresh</span> : <span className="text-xs text-subtle">{s.bars_ago} bar{s.bars_ago === 1 ? "" : "s"} ago: price has moved since</span>}
        <span className={cx("badge ml-auto", CONFIDENCE[e.confidence])} title="Graded by a fixed rule from the evidence below">Confidence: {e.confidence}</span>
      </header>

      <div className="grid grid-cols-2 divide-x divide-line border-b border-line sm:grid-cols-4">
        {[["Entry", n(p.entry), "text-fg"], ["Stop", n(p.stop), "text-down"], ["Target", n(p.target), "text-up"],
          ["Reward : risk", reward != null && risk > 0 ? (reward / risk).toFixed(2) : "–", "text-fg"]].map(([k, v, ink], i) => (
          <div key={k} className={cx("px-3 py-2", i >= 2 && "max-sm:border-t max-sm:border-line")}><p className="eyebrow">{k}</p><p className={cx("num mt-0.5 text-[15px]", ink)}>{v}</p></div>
        ))}
      </div>

      {ai?.source && ai.source !== "template" ? (
        <div className="space-y-1 border-b border-line bg-info/[0.05] px-3 py-2.5 text-[12.5px]">
          <p className="leading-relaxed text-fg">{ai.summary}</p>
          {ai.verdict_in_words && <p className="font-medium text-fg">{ai.verdict_in_words}</p>}
          <p className="text-2xs text-subtle">Written by {ai.source} from the facts below only; every number was checked against them.</p>
        </div>
      ) : (
        e.summary && <p className="border-b border-line px-3 py-2.5 text-[12.5px] leading-relaxed text-fg">{e.summary}</p>
      )}
      {ai?.note && <p className="border-b border-line px-3 py-2 text-xs text-warn">{ai.note}</p>}

      <div className="grid divide-line md:grid-cols-2 md:divide-x [&>section]:border-b [&>section]:border-line">
        <Section title="Why: the rule that fired">
          <p className="text-[12.5px] text-fg">{e.rule}</p>
          <ul className="mt-1 space-y-1 text-[12.5px]">
            {e.conditions.map((c) => (
              <Check key={c.name} ok={c.met}><span className="font-medium text-fg">{c.name}:</span> <span className="text-muted">{c.detail}</span></Check>
            ))}
          </ul>
        </Section>

        <Section title="The plan">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12.5px]">
            <dt className="text-subtle">Entry</dt><dd>near <span className="num">{n(p.entry)}</span> <span className="text-muted">(next bar&apos;s open)</span></dd>
            <dt className="text-subtle">Stop</dt><dd><span className="num text-down">{n(p.stop)}</span> <span className="text-muted">· {p.stop_reason}</span></dd>
            {p.target != null && (<><dt className="text-subtle">Target</dt><dd><span className="num text-up">{n(p.target)}</span> <span className="text-muted">· {p.target_reason}</span></dd></>)}
            {p.instrument === "future" ? (
              <>
                <dt className="text-subtle">Contract</dt><dd><span className="num">{p.contract}</span> · {p.lots} lot{p.lots === 1 ? "" : "s"} of {p.lot_size} <span className="text-muted">({p.quantity} units)</span></dd>
                {p.notional != null && (<><dt className="text-subtle">Exposure</dt><dd className="num">{inr(p.notional)} <span className="font-sans text-muted">on about</span> {inr(p.margin_estimate ?? 0)} <span className="font-sans text-muted">margin</span></dd></>)}
              </>
            ) : (
              p.quantity != null && (<><dt className="text-subtle">Size</dt><dd><span className="num">{p.quantity}</span> shares</dd></>)
            )}
            {p.max_loss_with_charges != null && (<><dt className="text-subtle">Max loss</dt><dd><span className="num">{inr(p.max_loss_with_charges)}</span> <span className="text-muted">incl. charges, if the stop fills</span></dd></>)}
            {p.exit_by && (<><dt className="text-subtle">Exit by</dt><dd><span className="num">{p.exit_by.slice(0, 5)}</span> IST, win or lose</dd></>)}
          </dl>
        </Section>

        <Section title="The evidence (after costs)">
          {e.evidence.length === 0 ? (
            <p className="text-[12.5px] text-down">No backtest of this exact version yet.</p>
          ) : (
            <table className="dtable dtable-compact -mx-2 w-[calc(100%+1rem)]">
              <thead><tr><th>Source</th><th className="r">Trades</th><th className="r"><Term tip={TERMS.expectancy}>Avg</Term></th><th className="r">Win</th></tr></thead>
              <tbody>
                {e.evidence.map((ev, i) => (
                  <tr key={i}>
                    <td className="text-muted">{ev.source}{ev.regime ? `, ${ev.regime}` : ""}</td>
                    <td className="r">{ev.trades}</td>
                    <td className="r"><RValue v={ev.expectancy_r} /></td>
                    <td className="r text-muted">{pct(ev.win_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {e.similar && (
            <p className="text-[12.5px]">
              <span className="font-medium text-fg">Similar setups:</span> {e.similar.wins} won, {e.similar.losses} lost, average <RValue v={e.similar.average_r} />.{" "}
              <span className="text-muted">{e.similar.note}</span>
            </p>
          )}
        </Section>

        <Section title="Risks, and what cancels it">
          <ul className="space-y-1 text-[12.5px]">
            {e.risks.map((x) => <li key={x} className="flex gap-2 text-muted"><span className="mt-[7px] size-1 shrink-0 rounded-full bg-warn" />{x}</li>)}
          </ul>
          <p className="text-[12.5px]"><span className="font-medium text-fg">Invalidation:</span> <span className="text-muted">{e.invalidation}</span></p>
        </Section>

        {mine && (
          <Section title="From your journal" className="md:col-span-2">
            {!mine.available ? (
              <p className="text-[12.5px] text-muted">Not available: {mine.reason}</p>
            ) : mine.trades.length === 0 ? (
              <p className="text-[12.5px] text-muted">{mine.note ?? "No similar trades yet."}</p>
            ) : (
              <>
                <p className="text-[12.5px]">{mine.wins} won, {mine.losses} lost, average <RValue v={mine.average_r} /> across your {mine.trades.length} most similar trades.</p>
                <ul className="space-y-0.5 text-xs text-muted">
                  {mine.trades.map((t) => (
                    <li key={t.trade_id} className="num">{t.entry_ts.slice(0, 10)} {t.side} {t.symbol}: {t.exit_reason}, <RValue v={t.r} /> <span className="text-subtle">(match {(t.similarity * 100).toFixed(0)}%)</span></li>
                  ))}
                </ul>
              </>
            )}
          </Section>
        )}
      </div>

      <footer className="flex flex-wrap items-center gap-2 px-3 py-2.5">
        <button onClick={paperTrade} disabled={busy || ticket !== null} className={cx("btn", buy ? "btn-buy" : "btn-danger")}>
          {busy ? "Preparing ticket…" : `Paper ${buy ? "buy" : "sell"}`}
        </button>
        <button onClick={() => aiCall("summary")} disabled={aiBusy !== null} className="btn"><IconSparkle size={14} />{aiBusy === "summary" ? "Writing…" : "Explain in plain words"}</button>
        <button onClick={() => aiCall("similar")} disabled={aiBusy !== null} className="btn">{aiBusy === "similar" ? "Searching…" : "Similar trades I took"}</button>
        <button onClick={() => setFno((x) => !x)} aria-expanded={fno} className="btn"><IconLayers size={14} />{fno ? "Hide futures & options" : "Futures & options"}</button>
        <Link href={`/chart?symbol=${encodeURIComponent(s.symbol)}`} className="btn btn-ghost"><IconCandles size={14} />Chart</Link>
        <span className="num ml-auto text-2xs text-subtle">{s.strategy_id} v{s.strategy_version}</span>
        {result && <span className="w-full text-xs text-muted">{result}</span>}
      </footer>
      {fno && (
        <section className="border-t border-line p-3">
          <h4 className="eyebrow mb-2">Trade it with futures or options</h4>
          <FnoPanel symbol={s.symbol} side={s.side} entry={p.entry} stop={p.stop} target={p.target} signal={s} />
        </section>
      )}
      {ticket && (
        <div className="border-t border-line p-3">
          <OrderTicket order={ticket} onDone={(o) => {
            setTicket(null);
            if (o?.state === "working") { setResult("Paper order working: see the Paper page."); toast("success", "Paper order working", `${o.params.side.toUpperCase()} ${o.params.qty} ${o.params.symbol}`); }
            else if (o?.state === "rejected") setResult(o.risk_verdict?.verdict === "BLOCK" ? `Blocked: ${o.risk_verdict.reasons.join("; ")}` : "Cancelled.");
          }} />
        </div>
      )}
    </article>
  );
}
