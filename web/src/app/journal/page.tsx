"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { useFeedback } from "@/components/ui/feedback";
import { IconBook, IconRefresh, IconSparkle } from "@/components/ui/icons";
import { Badge, Empty, Meter, Notice, Page, PageHeader, PageLoading, Panel, Pnl, RValue, Side, SignInRequired, SkeletonRows } from "@/components/ui/kit";
import { api, type AiStatus, type AskAnswer, type JournalData, type Review } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const EMOTIONS = ["", "calm", "confident", "anxious", "fearful", "greedy", "impatient", "revenge", "bored"];
const when = (iso: string) => new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function ReviewCard({ rv }: { rv: Review }) {
  const b = rv.body;
  return (
    <article className="panel space-y-2.5 p-3 text-[12.5px]">
      <header className="flex flex-wrap items-center gap-2">
        <Badge tone={rv.kind === "weekly" ? "info" : "neutral"}>{rv.kind}</Badge>
        <span className="num text-2xs text-subtle">{rv.period_start === rv.period_end ? rv.period_end : `${rv.period_start} → ${rv.period_end}`} · {rv.model ?? "template"}</span>
      </header>
      {b.headline && <h3 className="text-[14px] font-semibold text-fg">{b.headline}</h3>}
      {b.what_happened && <p className="text-fg">{b.what_happened}</p>}
      {b.discipline && <p className="text-muted">{b.discipline}</p>}
      {!!b.patterns?.length && <ul className="space-y-1">{b.patterns.map((x) => <li key={x} className="flex gap-2"><span className="mt-[7px] size-1 shrink-0 rounded-full bg-subtle" />{x}</li>)}</ul>}
      {!!b.for_tomorrow?.length && (<div className="space-y-1"><p className="eyebrow">For tomorrow</p><ul className="space-y-1">{b.for_tomorrow.map((x) => <li key={x} className="flex gap-2"><span className="mt-[7px] size-1 shrink-0 rounded-full bg-accent" />{x}</li>)}</ul></div>)}
      {b.proposals_note && <p className="text-xs text-subtle">{b.proposals_note}</p>}
      {!!b.proposals?.length && (
        <div className="space-y-1.5">
          <p className="eyebrow">Hypotheses to test <span className="normal-case">· untested: nothing changes until they pass a backtest, paper trading and your approval</span></p>
          {b.proposals.map((p) => (
            <div key={p.hypothesis} className="rounded-md border border-dashed border-line-strong p-2.5">
              <p className="font-medium text-fg">{p.hypothesis}</p>
              <p className="text-xs text-muted">Change to test (<span className="num">{p.strategy_id}</span>): {p.change}</p>
              <p className="text-xs text-subtle">Why: {p.evidence}</p>
            </div>
          ))}
          <Link href="/lab" className="link text-xs">Test them in the Lab</Link>
        </div>
      )}
    </article>
  );
}

export default function JournalPage() {
  const sb = supabase();
  const { toast } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<JournalData | null>(null);
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, { user_notes: string; emotion: string }>>({});
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<AskAnswer | null>(null);

  useEffect(() => {
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => setSession(data.session));
  }, [sb]);

  const load = useCallback(() => {
    api<JournalData>("/journal").then(setData).catch((e: Error) => setError(e.message));
    api<AiStatus>("/ai/status").then(setStatus).catch(() => {});
  }, []);

  useEffect(() => {
    if (session) load();
  }, [session, load]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="The journal" />;

  const ask = () => run("ask", async () => setAnswer(await api<AskAnswer>("/ai/ask", { method: "POST", body: { question: q } })));
  return (
    <Page wide>
      <PageHeader
        title="Journal"
        subtitle="Every closed trade gets a post-mortem from its facts. Your notes become part of what similar signals recall."
        meta={status && <Badge tone={status.configured ? "accent" : "neutral"}>{status.configured ? `${status.model}` : "No AI model"}</Badge>}
        actions={status && (
          <div className="w-48" title="Model tokens used today, of the daily cap">
            <Meter label="AI tokens today" used={status.tokens_today} limit={status.daily_cap} format={(n) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n))} />
          </div>
        )}
      />
      {error && <Notice tone="error">{error}</Notice>}

      <Panel title={<div className="flex items-center gap-2"><IconSparkle size={15} className="text-accent" /><h2 className="panel-title">Ask the analyst</h2></div>}
        actions={<span className="hidden text-2xs text-subtle md:inline">answers from your account, trades, journal, reviews, backtests and the indicator guide · no buy or sell calls</span>}>
        <div className="flex gap-2">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Why did my trades lose this week? How does Supertrend work?"
            onKeyDown={(e) => { if (e.key === "Enter" && q.trim()) ask(); }} className="field h-9 flex-1" aria-label="Question for the analyst" />
          <button disabled={!q.trim() || busy !== null} onClick={ask} className="btn btn-primary h-9 px-4">{busy === "ask" ? "Thinking…" : "Ask"}</button>
        </div>
        {busy === "ask" && <div className="mt-3 space-y-2"><div className="skeleton h-3 w-11/12" /><div className="skeleton h-3 w-9/12" /><div className="skeleton h-3 w-10/12" /></div>}
        {answer && busy !== "ask" && (
          <div className="mt-3 space-y-2 rounded-md border border-line bg-canvas/60 p-3 text-[13px]" style={{ animation: "tb-in 160ms ease-out" }}>
            <p className="leading-relaxed whitespace-pre-wrap text-fg">{answer.answer}</p>
            <p className="text-2xs text-subtle">Based on: {answer.based_on.join(", ") || "–"} · {answer.model}</p>
            {answer.unverified_numbers.length > 0 && (
              <Notice tone="warn">Check these numbers, they were not found in your data: {answer.unverified_numbers.join(", ")}</Notice>
            )}
          </div>
        )}
      </Panel>

      <div className="grid gap-4 xl:grid-cols-12">
        <section className="flex min-w-0 flex-col gap-3 xl:col-span-7">
          <div className="flex items-center gap-2">
            <h2 className="text-[14px] font-semibold">Closed trades</h2>
            {data && <Badge>{data.trades.length}</Badge>}
          </div>
          {!data && <div className="panel"><SkeletonRows rows={5} /></div>}
          {data && data.trades.length === 0 && <div className="panel"><Empty title="No closed trades yet" icon={<IconBook size={16} />}>Paper-trade a signal, and its post-mortem appears here when it closes.</Empty></div>}
          {data?.trades.map((t) => {
            const j = t.journal;
            const n = notes[t.id] ?? { user_notes: j?.user_notes ?? "", emotion: j?.emotion ?? "" };
            return (
              <article key={t.id} className="panel overflow-hidden text-[12.5px]">
                <header className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line px-3 py-2">
                  <Side side={t.side} />
                  <span className="num font-semibold">{t.qty} × {t.symbol}</span>
                  <span className="num text-xs text-muted">{when(t.entry_ts)} → {t.exit_ts ? when(t.exit_ts) : "open"}</span>
                  <span className="text-xs text-subtle">{t.exit_reason}</span>
                  <span className="ml-auto flex items-center gap-2"><Pnl v={t.net_pnl ?? 0} className="font-medium" /><RValue v={t.r_multiple} className="text-xs" /></span>
                </header>
                <div className="space-y-2 p-3">
                  {j?.ai_postmortem ? (
                    <>
                      <p className="leading-relaxed whitespace-pre-wrap text-fg">{j.ai_postmortem}</p>
                      {j.lessons && <p><span className="font-medium text-accent">Lesson:</span> {j.lessons}</p>}
                      <div className="flex flex-wrap items-center gap-1.5">
                        {j.mistake_tags.map((tag) => <Badge key={tag} tone="warn">{tag}</Badge>)}
                        <span className="ml-auto text-2xs text-subtle">post-mortem: {j.ai_model ?? "template"} · {t.entry_context?.strategy ?? t.entry_context?.source ?? "manual"}</span>
                      </div>
                    </>
                  ) : (
                    <p className="text-muted">Post-mortem not written yet.</p>
                  )}
                  <div className="flex flex-wrap items-start gap-2 border-t border-line pt-2.5">
                    <textarea rows={2} placeholder="Your note: why you took it, what you felt, what you would do differently" aria-label="Your note"
                      value={n.user_notes} onChange={(e) => setNotes({ ...notes, [t.id]: { ...n, user_notes: e.target.value } })}
                      className="field min-w-56 flex-1 text-[12.5px]" />
                    <div className="flex flex-col gap-1.5">
                      <select value={n.emotion} onChange={(e) => setNotes({ ...notes, [t.id]: { ...n, emotion: e.target.value } })} className="field w-36" aria-label="How you felt">
                        {EMOTIONS.map((x) => <option key={x} value={x}>{x || "Feeling…"}</option>)}
                      </select>
                      <button disabled={busy !== null || !notes[t.id]} onClick={() => run(`note-${t.id}`, async () => {
                        await api(`/journal/${t.id}`, { method: "PUT", body: n });
                        setNotes((x) => { const c = { ...x }; delete c[t.id]; return c; });
                        toast("success", "Note saved");
                      })} className="btn btn-sm">Save note</button>
                    </div>
                  </div>
                  <button disabled={busy !== null} onClick={() => run(`pm-${t.id}`, () => api(`/journal/${t.id}/postmortem`, { method: "POST" }))}
                    className="btn btn-ghost btn-sm -ml-2"><IconRefresh size={13} className={busy === `pm-${t.id}` ? "animate-spin" : ""} />{busy === `pm-${t.id}` ? "Writing…" : "Rewrite post-mortem"}</button>
                </div>
              </article>
            );
          })}
        </section>

        <section className="flex min-w-0 flex-col gap-3 xl:col-span-5">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[14px] font-semibold">Reviews</h2>
            <div className="ml-auto flex gap-2">
              <button disabled={busy !== null} onClick={() => run("daily", () => api("/reviews", { method: "POST", body: { kind: "daily" } }))} className="btn btn-sm">{busy === "daily" ? "Writing…" : "Today's review"}</button>
              <button disabled={busy !== null} onClick={() => run("weekly", () => api("/reviews", { method: "POST", body: { kind: "weekly" } }))} className="btn btn-sm">{busy === "weekly" ? "Writing…" : "This week's review"}</button>
            </div>
          </div>
          {!data && <div className="panel"><SkeletonRows rows={4} cols={1} /></div>}
          {data?.reviews.filter((x) => x.kind !== "brief").slice(0, 4).map((rv) => <ReviewCard key={rv.id} rv={rv} />)}
          {data && !data.reviews.some((x) => x.kind !== "brief") && <div className="panel"><Empty title="No reviews yet">Write today&apos;s review after the close; the weekly one adds hypotheses once you have 20+ trades.</Empty></div>}
        </section>
      </div>
    </Page>
  );
}
