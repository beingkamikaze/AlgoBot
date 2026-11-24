"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { MarketNotes, WhyCard } from "@/components/global-journal";
import { Ticket } from "@/components/global-paper";
import { useFeedback } from "@/components/ui/feedback";
import { IconBolt } from "@/components/ui/icons";
import { Badge, Empty, Notice, PageHeader, PageLoading, Panel, Side, SignInRequired, SkeletonRows, StateBadge, px } from "@/components/ui/kit";
import { api, type GlobalOrder, type GlobalSignal, type GlobalSignals } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const when = (iso: string) =>
  new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });

function SignalCard({ s, now, onTicket }: { s: GlobalSignal; now: number; onTicket: (s: GlobalSignal) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const e = s.explanation;
  const live = s.state === "new" && new Date(s.expires_at).getTime() > now;
  const age = Math.round(e.age_when_seen_seconds / 60);
  return (
    <div className="overflow-hidden rounded-md border border-line-strong bg-canvas">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <Side side={s.side} />
        <span className="text-[14px] font-semibold">{e.card?.source ?? s.strategy_id}</span>
        <span className="text-xs text-muted">v{s.strategy_version} · {s.timeframe} · bar closed {when(s.signal_ts)} IST{age > 0 ? ` · seen ${age} min later` : ""}</span>
        <span className="ml-auto flex items-center gap-2">
          {s.your_ticket && <StateBadge state={s.your_ticket} />}
          <Badge tone={live ? "info" : "neutral"}>{live ? `open until ${when(s.expires_at).split(", ").pop()}` : "expired"}</Badge>
        </span>
      </div>
      <dl className="grid grid-cols-3 divide-x divide-line border-b border-line">
        {([["Signal close", px(s.ref_price), ""], ["Stop", px(s.stop), "text-down"], ["Target", `${s.target_r}R`, "text-up"]] as const).map(([k, v, ink]) => (
          <div key={k} className="px-3 py-2"><dt className="eyebrow">{k}</dt><dd className={`num mt-0.5 text-[14px] ${ink}`}>{v}</dd></div>
        ))}
      </dl>
      <div className="space-y-2 px-3 py-2.5 text-[12.5px]">
        <ul className="space-y-0.5">{e.conditions.map((c) => <li key={c.name} className="text-muted"><span className={c.met ? "text-fg" : "text-down"}>{c.met ? "✓" : "✗"} {c.name}</span> · {c.detail}</li>)}</ul>
        <p className="text-muted">Stop: {e.stop_reason}. Target: {e.target_reason}. Cancelled if: {e.invalidation}</p>
        {e.card && <WhyCard card={e.card} compact />}
        {!!e.card?.risks?.length && e.card.risks.map((r) => <p key={r} className="text-xs text-warn">Note: {r}</p>)}
      </div>
      <div className="flex items-center gap-2 border-t border-line px-3 py-2.5">
        <button className="btn btn-primary" disabled={!live || busy || !!s.your_ticket}
          onClick={async () => { setBusy(true); await onTicket(s); setBusy(false); }}>
          {busy ? "Making ticket…" : s.your_ticket ? "Ticket made" : "Make paper ticket"}
        </button>
        <span className="text-xs text-muted">A ticket still goes through the risk check and needs your confirmation. Nothing trades by itself.</span>
      </div>
    </div>
  );
}

export function GlobalSignalsView() {
  const sb = supabase();
  const { toast } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<GlobalSignals | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState<GlobalOrder | null>(null);
  const [now, setNow] = useState(0);

  useEffect(() => {
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => setSession(data.session));
  }, [sb]);

  const load = useCallback(() => api<GlobalSignals>("/global/signals").then((d) => { setData(d); setNow(Date.now()); setError(null); }).catch((e: Error) => setError(e.message)), []);
  useEffect(() => {
    if (!session) return;
    load();
    const id = setInterval(load, 20_000);
    return () => clearInterval(id);
  }, [session, load]);

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="XAUUSD signals" />;

  async function makeTicket(s: GlobalSignal) {
    try {
      setTicket(await api<GlobalOrder>(`/global/signals/${s.id}/ticket`, { method: "POST" }));
      await load();
    } catch (e) {
      toast("error", "Could not make the ticket", (e as Error).message);
    }
  }

  const sc = data?.scanner;
  return (
    <>
      <PageHeader
        title="XAUUSD signals"
        subtitle="What gold's three strategies find on live closed 5-minute bars, at the settings fixed on 7 Nov 2026. Each signal says why; you decide."
        actions={data && <Badge tone={data.market_open ? "up" : "neutral"}>{data.market_open ? "Market open" : "Market closed"}</Badge>}
      />
      {error && <Notice tone="error" title="Could not load signals">{error}</Notice>}
      <Notice tone="warn" title="These strategies have not passed their backtests">
        On the research days none of them passes the Lab&apos;s bar (see <Link href="/global/backtests" className="underline">XAUUSD backtests</Link>), so treat every
        signal as an experiment. Live signals and their paper results are never used to choose settings: that keeps the one Lab test on the held-back days honest.
      </Notice>
      {sc?.error && <Notice tone="warn" title="The scanner is failing">{sc.error}</Notice>}

      <div className="mt-4 grid gap-4 xl:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-8">
          {ticket && (
            <Panel title="Confirm the paper ticket">
              <Ticket order={ticket} onDone={(o) => { setTicket(o && o.state === "proposed" ? o : null); if (o?.state === "working") toast("success", "Paper order working", "Follow it on the XAUUSD paper page."); load(); }} />
            </Panel>
          )}
          <Panel title="Signals">
            {!data ? <SkeletonRows rows={3} /> : !data.signals.length ? (
              <Empty title="No signals yet" icon={<IconBolt size={16} />}>
                Signals come only from bars that closed between 08:00 London and 11:00 New York (13:30 to 21:30 IST in winter), at most two per strategy a day.
                {sc?.at ? ` Last check ${new Date(sc.at * 1000).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} IST.` : ""}
              </Empty>
            ) : (
              <div className="space-y-3">{data.signals.map((s) => <SignalCard key={s.id} s={s} now={now} onTicket={makeTicket} />)}</div>
            )}
          </Panel>
        </div>
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-4">
          <Panel title="The strategies">
            {!data ? <SkeletonRows rows={3} /> : (
              <ul className="space-y-2 text-[12.5px]">{data.strategies.map((s) => <li key={s.id}><p className="font-medium text-fg">{s.name}</p><p className="text-muted">{s.description}</p></li>)}</ul>
            )}
          </Panel>
          <MarketNotes />
        </div>
      </div>
    </>
  );
}
