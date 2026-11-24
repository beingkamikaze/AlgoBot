"use client";

import { useCallback, useEffect, useState } from "react";

import { useFeedback } from "@/components/ui/feedback";
import { Badge, Empty, Notice, Panel, SkeletonRows } from "@/components/ui/kit";
import {
  api, type GlobalCard, type GlobalJournal, type GlobalMacro, type GlobalNews, type GlobalReview, type GlobalTrade,
} from "@/lib/api";

const usd = (n: number | null | undefined) =>
  n == null ? "–" : `${n < 0 ? "−" : ""}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const until = (m: number | undefined) =>
  m == null ? "" : m < 0 ? `${-m} min ago` : m < 60 ? `in ${m} min` : m < 48 * 60 ? `in ${Math.round(m / 60)} h` : `in ${Math.round(m / 1440)} days`;

/** "Why this trade": your reason first, then the facts the app measured. Nothing on it is written by a model. */
export function WhyCard({ card, compact = false }: { card: GlobalCard; compact?: boolean }) {
  if (card.error) return <p className="text-xs text-muted">Why: <span className="text-fg">{card.why || "not written"}</span> · {card.error}</p>;
  const noReason = card.why.startsWith("No reason was written");
  return (
    <div className="space-y-2 text-[12.5px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow">Why this trade</span>
        <Badge>{card.source}</Badge>
        {card.confidence && <Badge tone="warn" title={card.confidence.why}>confidence {card.confidence.grade}</Badge>}
      </div>
      <p className={noReason ? "text-warn" : "text-fg"}>{card.why}</p>
      {card.summary && <p className="text-muted">{card.summary}</p>}
      {!!card.context?.length && <ul className="list-disc space-y-0.5 pl-4 text-muted">{card.context.map((c) => <li key={c}>{c}</li>)}</ul>}
      {!!card.events?.length && (
        <div>
          <p className="eyebrow mb-0.5">US releases until flat-by</p>
          <ul className="space-y-0.5">{card.events.map((e) => <li key={e.at} className="text-warn">{e.name} · {e.ist} ({until(e.minutes_away)})</li>)}</ul>
        </div>
      )}
      {!compact && !!card.headlines?.length && (
        <div>
          <p className="eyebrow mb-0.5">Latest gold headlines (leads, not facts)</p>
          <ul className="space-y-0.5 text-muted">{card.headlines.map((h) => <li key={h.title}>{h.title}{h.source && <span className="text-subtle"> · {h.source}</span>}</li>)}</ul>
        </div>
      )}
      {card.confidence && <p className="text-xs text-subtle">{card.confidence.why}</p>}
    </div>
  );
}

/** A closed trade's card, post-mortem and your notes. */
export function TradeJournalView({ trade }: { trade: GlobalTrade }) {
  const { toast } = useFeedback();
  const [j, setJ] = useState<GlobalJournal | null | undefined>(undefined);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api<{ journal: GlobalJournal | null }>(`/global/paper/trades/${trade.id}/journal`)
      .then((r) => { if (live) { setJ(r.journal); setNotes(r.journal?.user_notes ?? ""); } })
      .catch((e) => { if (live) { setJ(null); setError((e as Error).message); } });
    return () => { live = false; };
  }, [trade.id]);

  async function write() {
    setBusy(true);
    setError(null);
    try {
      const r = await api<GlobalJournal>(`/global/paper/trades/${trade.id}/postmortem`, { method: "POST" });
      setJ(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    try {
      const r = await api<GlobalJournal>(`/global/paper/trades/${trade.id}/notes`, { method: "PUT", body: { notes } });
      setJ((old) => ({ ...(old ?? r), user_notes: r.user_notes }));
      toast("success", "Notes saved");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const card = trade.entry_context?.card;
  return (
    <div className="grid gap-4 px-3 py-3 lg:grid-cols-2">
      <div>{card ? <WhyCard card={card} compact /> : <p className="text-xs text-muted">No card: this trade was made before why cards existed.</p>}</div>
      <div className="space-y-2 text-[12.5px]">
        <div className="flex items-center gap-2">
          <span className="eyebrow">Post-mortem</span>
          {j?.ai_model && <Badge title={j.ai_model === "template" ? "The plain version: no model, or the model's reply failed the number check" : "Written by the model from the facts only"}>{j.ai_model === "template" ? "plain" : j.ai_model}</Badge>}
        </div>
        {j === undefined ? <SkeletonRows rows={2} /> : j?.ai_postmortem ? (
          <>
            <p className="whitespace-pre-line text-fg">{j.ai_postmortem}</p>
            {j.lessons && <p className="text-muted">Lesson: <span className="text-fg">{j.lessons}</span></p>}
            <div className="flex flex-wrap gap-1">{j.mistake_tags.map((t) => <Badge key={t} tone={t === "no mistake" ? "neutral" : "warn"}>{t}</Badge>)}</div>
          </>
        ) : <p className="text-muted">Not written yet. It is written in the background when a trade closes.</p>}
        <button className="btn" disabled={busy} onClick={write}>{busy ? "Writing…" : j?.ai_postmortem ? "Write it again" : "Write it now"}</button>
        <div className="space-y-1.5 pt-1">
          <label className="eyebrow" htmlFor={`notes-${trade.id}`}>Your notes</label>
          <textarea id={`notes-${trade.id}`} value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000} rows={2}
            placeholder="What you saw, how you felt, what you would do again" className="field text-xs" />
          <button className="btn" disabled={busy || notes === (j?.user_notes ?? "")} onClick={save}>Save notes</button>
        </div>
        {error && <p className="text-xs text-down">{error}</p>}
      </div>
    </div>
  );
}

/** US releases (official schedules) and gold headlines. */
export function MarketNotes() {
  const [macro, setMacro] = useState<GlobalMacro | null>(null);
  const [news, setNews] = useState<GlobalNews | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    const load = () => {
      api<GlobalMacro>("/global/macro?days=14").then((m) => live && setMacro(m)).catch((e) => live && setError((e as Error).message));
      api<GlobalNews>("/global/news").then((n) => live && setNews(n)).catch(() => undefined);
    };
    load();
    const id = setInterval(load, 5 * 60_000);
    return () => { live = false; clearInterval(id); };
  }, []);

  return (
    <Panel title="US releases and gold news">
      {error && <p className="text-xs text-down">{error}</p>}
      {!macro ? <SkeletonRows rows={2} /> : (
        <div className="space-y-3 text-[12.5px]">
          {macro.near.map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
          <div>
            <p className="eyebrow mb-1">Next 14 days</p>
            {!macro.events.length ? <p className="text-muted">No CPI, jobs report or Fed decision.</p> : (
              <ul className="space-y-1">
                {macro.events.map((e) => (
                  <li key={e.at + e.name} className="flex justify-between gap-3" title={e.note}>
                    <span className="text-fg">{e.name}</span>
                    <span className="num shrink-0 text-muted">{e.ist} · {until(e.minutes_away)}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-1 text-xs text-subtle">From the official BLS and Federal Reserve schedules, listed until {macro.covered_until}. They can jump past a stop.</p>
          </div>
          <div>
            <p className="eyebrow mb-1">Gold headlines · last 24 h</p>
            {!news ? <SkeletonRows rows={2} /> : news.error ? <p className="text-muted">{news.error}</p> : !news.headlines.length ? <p className="text-muted">None found.</p> : (
              <ul className="space-y-1">
                {news.headlines.slice(0, 6).map((h) => (
                  <li key={h.title}>
                    {h.link ? <a href={h.link} target="_blank" rel="noreferrer" className="text-fg hover:underline">{h.title}</a> : <span className="text-fg">{h.title}</span>}
                    <span className="text-xs text-subtle"> · {h.source}{h.ist ? ` · ${h.ist}` : ""}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-1 text-xs text-subtle">Google News. Headlines are leads, not facts; nothing trades on them.</p>
          </div>
        </div>
      )}
    </Panel>
  );
}

/** Daily and weekly reviews of gold paper trades: the numbers are computed by code; the model only puts them into words. */
export function GlobalReviews() {
  const [rows, setRows] = useState<GlobalReview[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<{ reviews: GlobalReview[] }>("/global/reviews").then((r) => setRows(r.reviews)).catch((e) => { setRows([]); setError((e as Error).message); });
  }, []);
  useEffect(load, [load]);

  async function write(kind: "daily" | "weekly") {
    setBusy(kind);
    setError(null);
    try {
      await api(`/global/reviews/${kind}`, { method: "POST" });
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel title="Reviews" actions={
      <div className="flex gap-2">
        <button className="btn" disabled={!!busy} onClick={() => write("daily")}>{busy === "daily" ? "Writing…" : "Review today"}</button>
        <button className="btn" disabled={!!busy} onClick={() => write("weekly")}>{busy === "weekly" ? "Writing…" : "Review the week"}</button>
      </div>
    }>
      {error && <p className="mb-2 text-xs text-down">{error}</p>}
      {!rows ? <SkeletonRows rows={2} /> : !rows.length ? <Empty title="No reviews yet">A review sums up your closed gold trades for the day (it rolls at 17:00 New York) or the week.</Empty> : (
        <ul className="space-y-3 text-[12.5px]">
          {rows.slice(0, 5).map((r) => (
            <li key={r.id} className="space-y-1 border-b border-line pb-3 last:border-0 last:pb-0">
              <div className="flex flex-wrap items-center gap-2">
                <Badge>{r.kind}</Badge>
                <span className="text-xs text-muted">{r.period_start === r.period_end ? r.period_start : `${r.period_start} to ${r.period_end}`}</span>
                {r.stats.trades > 0 && <span className="num text-xs text-muted">{r.stats.trades} trades · {usd(r.stats.net_pnl_dollars)}</span>}
                {!r.model && r.stats.trades > 0 && <Badge title="No model, or its reply failed the number check">plain</Badge>}
              </div>
              <p className="font-medium text-fg">{r.body.headline}</p>
              <p className="text-muted">{r.body.what_happened}</p>
              {r.body.discipline && <p className="text-muted">{r.body.discipline}</p>}
              {!!r.body.patterns?.length && <ul className="list-disc pl-4 text-muted">{r.body.patterns.map((p) => <li key={p}>{p}</li>)}</ul>}
              {!!r.body.next_time?.length && <ul className="list-disc pl-4 text-fg">{r.body.next_time.map((p) => <li key={p}>{p}</li>)}</ul>}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
