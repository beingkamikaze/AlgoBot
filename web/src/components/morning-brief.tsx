"use client";

import { useEffect, useState } from "react";

import { IconRefresh, IconSparkle } from "@/components/ui/icons";
import { Empty, Notice, Panel } from "@/components/ui/kit";
import { api, type Review } from "@/lib/api";

function watchlist(): string[] {
  try {
    return JSON.parse(localStorage.getItem("tb.watchlist") ?? "[]");
  } catch {
    return [];
  }
}

function List({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="space-y-1">
      <p className="eyebrow">{title}</p>
      <ul className="space-y-1">{items.map((x) => <li key={x} className="flex gap-2"><span className="mt-[7px] size-1 shrink-0 rounded-full bg-subtle" />{x}</li>)}</ul>
    </div>
  );
}

export function MorningBrief({ className }: { className?: string }) {
  const [brief, setBrief] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Review | null>("/ai/brief/latest").then(setBrief).catch(() => {});
  }, []);

  async function make() {
    setBusy(true);
    setError(null);
    try {
      setBrief(await api<Review>("/ai/brief", { method: "POST", body: { symbols: watchlist() } }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const b = brief?.body;
  return (
    <Panel className={className} title="Pre-market brief" actions={
      <button onClick={make} disabled={busy} className="btn btn-sm">
        <IconRefresh size={13} className={busy ? "animate-spin" : ""} />{busy ? "Reading the news…" : brief ? "Refresh" : "Write today's brief"}
      </button>
    }>
      {error && <div className="mb-2"><Notice tone="error">{error}</Notice></div>}
      {!b ? (
        <Empty title="No brief yet" icon={<IconSparkle size={16} />}>
          From index levels, India VIX and your watchlist&apos;s NSE filings, results dates and headlines. Headlines are leads, not facts.
        </Empty>
      ) : (
        <div className="space-y-3 text-[12.5px] leading-relaxed text-fg">
          <p className="text-2xs text-subtle">{new Date(brief.created_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" })} IST · {brief.model}</p>
          {b.market && <p>{b.market}</p>}
          {!!b.results_and_events?.length && <List title="Results and events" items={b.results_and_events} />}
          {!!b.stock_notes?.length && (
            <div className="space-y-1">
              <p className="eyebrow">Your watchlist</p>
              <ul className="space-y-1">{b.stock_notes.map((x) => <li key={x.symbol}><span className="num font-medium text-accent">{x.symbol}</span> <span className="text-muted">{x.note}</span></li>)}</ul>
            </div>
          )}
          {!!b.watch_out?.length && <List title="Watch out" items={b.watch_out} />}
          {!!b.unverified_numbers?.length && <Notice tone="warn">Check these numbers, they were not in the source data: {b.unverified_numbers.join(", ")}</Notice>}
        </div>
      )}
    </Panel>
  );
}
