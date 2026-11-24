"use client";

import type { Session } from "@supabase/supabase-js";
import { useCallback, useEffect, useState } from "react";

import { Ticket } from "@/components/global-paper";
import { TelegramPanel } from "@/components/telegram-panel";
import { useFeedback } from "@/components/ui/feedback";
import { IconSparkle } from "@/components/ui/icons";
import {
  Badge, Empty, Notice, PageHeader, PageLoading, Panel, RValue, Side, SignInRequired, SkeletonRows, Stat, StatStrip, StateBadge, px,
} from "@/components/ui/kit";
import { api, type AgentDesk, type AgentGroup, type AgentIdea, type GlobalOrder } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const time = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const day = (iso: string) =>
  new Date(iso).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short" });

const REVIEW_TONE = { ok: "up", caution: "warn", reject: "down" } as const;

function Outcome({ o }: { o: AgentIdea["outcome"] }) {
  if (!o) return <span className="text-xs text-muted">Result: still running</span>;
  if (o.status !== "done") return <span className="text-xs text-muted">Result: {o.note ?? o.status}</span>;
  return (
    <span className="text-xs text-muted">
      If taken: {o.reason} at {px(o.exit ?? null)} → <RValue v={o.r ?? null} />
    </span>
  );
}

function IdeaCard({ idea, now, onTicket }: { idea: AgentIdea; now: number; onTicket: (i: AgentIdea) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const p = idea.proposal;
  const live = idea.state === "new" && !!idea.expires_at && new Date(idea.expires_at).getTime() > now;
  const r = idea.review;
  return (
    <div className="overflow-hidden rounded-md border border-line-strong bg-canvas">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        {idea.side && <Side side={idea.side} />}
        <span className="text-[14px] font-semibold capitalize">{idea.setup ?? "idea"}</span>
        <span className="text-xs text-muted">{day(idea.created_at)} {time(idea.created_at)} IST · {idea.price_source === "futures" ? "GC=F, about 10 min late" : "spot"}</span>
        <span className="ml-auto flex items-center gap-2">
          {idea.your_ticket && <StateBadge state={idea.your_ticket} />}
          {idea.state === "vetoed" ? <Badge tone="down">Turned down</Badge>
            : live ? <Badge tone="info">open until {time(idea.expires_at!)}</Badge> : <Badge>expired</Badge>}
        </span>
      </div>
      {p.plan && (
        <dl className="grid grid-cols-4 divide-x divide-line border-b border-line">
          {([["Price seen", px(p.plan.entry), ""], ["Stop", px(p.plan.stop), "text-down"], ["Target", px(p.plan.target), "text-up"],
            ["Reward : risk", p.plan.reward_risk ? `${p.plan.reward_risk} : 1` : "–", ""]] as const).map(([k, v, ink]) => (
            <div key={k} className="px-3 py-2"><dt className="eyebrow">{k}</dt><dd className={`num mt-0.5 text-[14px] ${ink}`}>{v}</dd></div>
          ))}
        </dl>
      )}
      <div className="space-y-2 px-3 py-2.5 text-[12.5px]">
        <p className="text-xs text-muted">Looked because: {idea.triggers.join("; ")}</p>
        <ul className="list-disc space-y-0.5 pl-4 text-fg">{p.why.map((w) => <li key={w}>{w}</li>)}</ul>
        {p.wrong_if && <p className="text-muted">Wrong if: {p.wrong_if}</p>}
        {r.verdict && (
          <div className="rounded border border-line px-2.5 py-2">
            <p className="flex items-center gap-2">
              <Badge tone={REVIEW_TONE[r.verdict]}>{r.by === "rules" ? "App checks" : "Reviewer"}: {r.verdict}</Badge>
              <span className="text-muted">{r.summary}</span>
            </p>
            {!!r.concerns?.length && <ul className="mt-1 list-disc pl-4 text-muted">{r.concerns.map((c) => <li key={c}>{c}</li>)}</ul>}
          </div>
        )}
        <Outcome o={idea.outcome} />
      </div>
      {idea.state !== "vetoed" && (
        <div className="flex items-center gap-2 border-t border-line px-3 py-2.5">
          <button className="btn btn-primary" disabled={!live || busy || !!idea.your_ticket}
            onClick={async () => { setBusy(true); await onTicket(idea); setBusy(false); }}>
            {busy ? "Making ticket…" : idea.your_ticket ? "Ticket made" : "Make paper ticket"}
          </button>
          <span className="text-xs text-muted">Priced now and sized by the risk check. You still confirm it.</span>
        </div>
      )}
    </div>
  );
}

function PassRow({ idea }: { idea: AgentIdea }) {
  return (
    <li className="flex gap-3 border-b border-line py-2 text-[12.5px] last:border-0">
      <span className="num shrink-0 text-muted">{time(idea.created_at)}</span>
      <span className="min-w-0">
        <span className="text-fg">No trade.</span> <span className="text-muted">{idea.proposal.pass_reason}</span>
        <span className="block text-xs text-subtle">Looked because: {idea.triggers.join("; ")}</span>
      </span>
    </li>
  );
}

function Group({ label, g }: { label: string; g: AgentGroup }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line py-1.5 text-[12.5px] last:border-0">
      <span className="text-muted">{label}</span>
      <span className="num text-right">
        {g.judged ? <><RValue v={g.avg_r ?? null} /> <span className="text-xs text-muted">avg · {g.judged} judged · {Math.round((g.win_rate ?? 0) * 100)}% won</span></>
          : <span className="text-xs text-muted">{g.ideas} ideas, none judged yet</span>}
      </span>
    </div>
  );
}

export function GlobalAgentView() {
  const sb = supabase();
  const { toast } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<AgentDesk | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState<GlobalOrder | null>(null);
  const [now, setNow] = useState(0);
  const [asking, setAsking] = useState(false);
  const [showPasses, setShowPasses] = useState(false);

  useEffect(() => {
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => setSession(data.session));
  }, [sb]);

  const load = useCallback(() => api<AgentDesk>("/global/agent").then((d) => { setData(d); setNow(Date.now()); setError(null); }).catch((e: Error) => setError(e.message)), []);
  useEffect(() => {
    if (!session) return;
    load();
    const id = setInterval(load, 20_000);
    return () => clearInterval(id);
  }, [session, load]);

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="the XAUUSD agent" />;

  async function makeTicket(i: AgentIdea) {
    try {
      setTicket(await api<GlobalOrder>(`/global/agent/ideas/${i.id}/ticket`, { method: "POST" }));
      await load();
    } catch (e) {
      toast("error", "Could not make the ticket", (e as Error).message);
    }
  }

  async function askNow() {
    setAsking(true);
    try {
      const r = await api<{ idea: AgentIdea | null }>("/global/agent/look", { method: "POST" });
      toast("success", r.idea?.kind === "trade" ? (r.idea.state === "vetoed" ? "Idea turned down by the checks" : "New trade idea") : "The agent passed",
        r.idea?.kind === "pass" ? r.idea.proposal.pass_reason : undefined);
      await load();
    } catch (e) {
      toast("error", "The agent could not look", (e as Error).message);
    } finally {
      setAsking(false);
    }
  }

  const trades = data?.ideas.filter((i) => i.kind === "trade") ?? [];
  const passes = data?.ideas.filter((i) => i.kind === "pass") ?? [];
  const st = data?.state;
  return (
    <>
      <PageHeader
        title="XAUUSD agent"
        subtitle="GPT-5 watches live gold, looks when something happens, and proposes a trade or passes. A second agent argues against each idea. You decide."
        actions={data && (
          <div className="flex items-center gap-2">
            <Badge tone={data.market_open ? "up" : "neutral"}>{data.market_open ? (data.in_window ? "In the entry window" : "Market open") : "Market closed"}</Badge>
            <button className="btn btn-primary" disabled={asking || !data.market_open || !data.ai_ready || !data.enabled} onClick={askNow}>
              {asking ? "Looking…" : "Ask the agent now"}
            </button>
          </div>
        )}
      />
      {error && <Notice tone="error" title="Could not load the agent">{error}</Notice>}
      <Notice tone="warn" title="Untested ideas, paper only">
        The agent makes trades up from the live market, so there is no backtest behind them, and there cannot be one: GPT-5 has read about
        past gold prices. Every idea grades low. Its only evidence is the track record on this page, and nothing it does is used to tune the
        tested strategies. Nothing trades by itself: an idea becomes a ticket only when you ask, and the ticket needs your confirmation.
      </Notice>
      {data && !data.ai_ready && <Notice tone="warn" title="No language model set up">Add the Azure OpenAI keys to backend/.env to switch the agent on.</Notice>}
      {data && !data.enabled && <Notice tone="warn" title="The agent is off">GLOBAL_AGENT_ENABLED is false in backend/.env.</Notice>}
      {st?.error && <Notice tone="warn" title="The agent's last look failed">{st.error}</Notice>}

      <div className="mt-4">
        <StatStrip cols={4}>
          <Stat label="Agent" value={<span className="text-[14px]">{st?.status ?? "–"}</span>} loading={!data} />
          <Stat label="Looks today" value={data ? `${st?.looks_today ?? 0} of ${data.looks_per_day}` : "–"} sub={data && `at most one every ${data.cooldown_minutes} min`} loading={!data} />
          <Stat label="Last look" value={st?.last_look ? `${time(st.last_look)} IST` : "–"} loading={!data} />
          <Stat label="Ideas so far, average if taken" value={data?.record.all.judged ? <RValue v={data.record.all.avg_r ?? null} /> : "–"}
            sub={data && `${data.record.all.ideas} trade ideas from ${data.record.looks} looks`} loading={!data} />
        </StatStrip>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-8">
          {ticket && (
            <Panel title="Confirm the paper ticket">
              <Ticket order={ticket} onDone={(o) => { setTicket(o && o.state === "proposed" ? o : null); if (o?.state === "working") toast("success", "Paper order working", "Follow it on the XAUUSD paper page."); load(); }} />
            </Panel>
          )}
          <Panel title="Trade ideas">
            {!data ? <SkeletonRows rows={3} /> : !trades.length ? (
              <Empty title="No trade ideas yet" icon={<IconSparkle size={16} />}>
                The agent looks only inside the entry window (08:00 London to 11:00 New York, 13:30 to 21:30 IST in winter), when price crosses a
                key level, a big bar prints, a trend turns, a session opens, a headline lands or a US release comes out, and at least once an hour.
                Passing is its normal answer. Each idea lasts {data.idea_minutes} minutes.
              </Empty>
            ) : (
              <div className="space-y-3">{trades.map((i) => <IdeaCard key={i.id} idea={i} now={now} onTicket={makeTicket} />)}</div>
            )}
          </Panel>
          <Panel title={`Passes (${passes.length})`} actions={!!passes.length && (
            <button className="btn btn-ghost" onClick={() => setShowPasses((v) => !v)}>{showPasses ? "Hide" : "Show"}</button>
          )}>
            {!passes.length ? <p className="text-[12.5px] text-muted">None yet.</p>
              : showPasses ? <ul>{passes.map((i) => <PassRow key={i.id} idea={i} />)}</ul>
              : <p className="text-[12.5px] text-muted">Latest, {time(passes[0].created_at)} IST: {passes[0].proposal.pass_reason}</p>}
          </Panel>
        </div>
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-4">
          <TelegramPanel />
          <Panel title="Track record">
            {!data ? <SkeletonRows rows={4} /> : (
              <>
                <Group label="All trade ideas" g={data.record.all} />
                <Group label="Passed the checks" g={data.record.passed_checks} />
                <Group label="Turned down" g={data.record.vetoed} />
                <Group label="You made a ticket" g={data.record.you_took} />
                <p className="mt-2 text-xs text-muted">{data.record.note}</p>
              </>
            )}
          </Panel>
          <Panel title="How it works">
            <ol className="list-decimal space-y-1 pl-4 text-[12.5px] text-muted">
              <li><span className="text-fg">Watcher</span> (code) measures levels, trends, the typical move, US releases and headlines from closed bars.</li>
              <li><span className="text-fg">Strategist</span> (GPT-5) proposes one trade with a stop where the idea is wrong, or passes.</li>
              <li><span className="text-fg">App checks</span> (code): stop on the right side, 0.8 to 4 typical moves away; reward at least 1.5 times risk; no made-up numbers.</li>
              <li><span className="text-fg">Reviewer</span> (GPT-5) argues against it and can turn it down. It cannot approve.</li>
              <li><span className="text-fg">You</span> make a ticket; the risk check sizes it from your max loss; you confirm.</li>
              <li><span className="text-fg">Scorekeeper</span> works out how every idea would have ended, taken or not.</li>
            </ol>
          </Panel>
        </div>
      </div>
    </>
  );
}
