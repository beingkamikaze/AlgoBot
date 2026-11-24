"use client";

import { useEffect, useState } from "react";

import { IconSearch } from "@/components/ui/icons";
import { Empty, Notice, Page, PageHeader } from "@/components/ui/kit";
import { api } from "@/lib/api";

type Entry = { what: string; use: string; limits: string };

export default function LearnPage() {
  const [items, setItems] = useState<Record<string, Entry>>({});
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  useEffect(() => {
    api<Record<string, Entry>>("/learn/indicators").then(setItems).catch((e: Error) => setError(e.message));
  }, []);
  const entries = Object.entries(items).filter(([name]) => name.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <Page>
      <PageHeader title="Indicator guide" subtitle="What each indicator in this app measures, how traders use it, and where it fails. The analyst on the Journal page answers from this same guide."
        actions={<div className="relative w-64"><IconSearch size={14} className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-subtle" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter indicators" aria-label="Filter indicators" className="field pl-8" /></div>} />
      {error && <Notice tone="error">{error}</Notice>}
      {!error && Object.keys(items).length === 0 && <div className="grid gap-3 md:grid-cols-2">{Array.from({ length: 6 }, (_, i) => <div key={i} className="skeleton h-36" />)}</div>}
      <div className="grid gap-3 md:grid-cols-2">
        {entries.map(([name, e]) => (
          <article key={name} className="panel overflow-hidden text-[12.5px]">
            <h2 className="border-b border-line px-3 py-2 text-[13px] font-semibold">{name}</h2>
            <dl className="space-y-2.5 p-3">
              <div><dt className="eyebrow">What it measures</dt><dd className="mt-0.5 text-fg">{e.what}</dd></div>
              <div><dt className="eyebrow">How traders use it</dt><dd className="mt-0.5 text-fg">{e.use}</dd></div>
              <div><dt className="eyebrow text-warn">Where it fails</dt><dd className="mt-0.5 text-muted">{e.limits}</dd></div>
            </dl>
          </article>
        ))}
      </div>
      {Object.keys(items).length > 0 && entries.length === 0 && <div className="panel"><Empty title="No indicator matches that filter" /></div>}
    </Page>
  );
}
