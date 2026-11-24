"use client";

import type { Session } from "@supabase/supabase-js";
import { Fragment, useCallback, useEffect, useState } from "react";

import { GlobalReviews, MarketNotes, TradeJournalView, WhyCard } from "@/components/global-journal";
import { useFeedback } from "@/components/ui/feedback";
import { IconCheck, IconPower, IconShield, IconWallet, IconX } from "@/components/ui/icons";
import {
  Badge, Empty, HoldButton, Meter, Notice, PageHeader, PageLoading, Panel, RValue, Side, SignInRequired, SkeletonRows, Stat,
  StatStrip, StateBadge, TableWrap, cx, px,
} from "@/components/ui/kit";
import { api, type GlobalOrder, type GlobalPaperOverview, type GlobalTrade } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const usd = (n: number | null | undefined) =>
  n == null ? "–" : `${n < 0 ? "−" : ""}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const time = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–";
const clock = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false }) : "–";
const age = (s: number | null | undefined) => (s == null ? "" : s < 90 ? "just now" : `${Math.round(s / 60)} min old`);

function UsdPnl({ v, plain = false }: { v: number | null | undefined; plain?: boolean }) {
  if (v == null) return <span className="num text-subtle">–</span>;
  const tone = v > 0 ? "text-up" : v < 0 ? "text-down" : "text-muted";
  return <span className={cx("num", plain ? "" : tone)}>{v > 0 ? "+" : ""}{usd(v)}</span>;
}

const LIMIT_LABEL: Record<string, string> = {
  max_loss_per_trade: "Max loss per trade ($)", max_daily_loss: "Max daily loss ($)", max_monthly_loss: "Max monthly loss ($)",
  max_trades_per_day: "Max trades per day", max_consecutive_losses: "Stop after losses in a row", max_open_positions: "Max open positions",
  max_position_value: "Max position value ($)", entry_start_london: "Entries from (London)", entry_end_new_york: "No entries after (New York)",
  flat_by_new_york: "Flat by (New York)",
};

/** The confirm step: the exact order, the risk check's verdict, and the buttons. Nothing is placed before "Confirm". */
export function Ticket({ order, onDone }: { order: GlobalOrder; onDone: (o: GlobalOrder | null) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const p = order.params;
  const v = order.risk_verdict;
  const pass = v?.verdict === "PASS";

  async function act(what: "approve" | "cancel", override = false) {
    setBusy(true);
    setError(null);
    try {
      onDone(await api<GlobalOrder>(`/global/paper/orders/${order.id}/${what}`, { method: "POST", body: what === "approve" ? { override, reason: note } : undefined }));
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="overflow-hidden rounded-md border border-line-strong bg-canvas" style={{ animation: "tb-in 160ms ease-out" }}>
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <Badge tone="warn">Paper</Badge>
        <Side side={order.side} />
        <span className="num text-[14px] font-semibold">{order.lots} lot × {order.symbol}</span>
        <span className="text-xs text-muted">{p.units} oz</span>
        <Badge tone={p.source === "spot" ? "info" : "warn"} title={p.source === "futures" ? "COMEX futures stand in for spot until a Twelve Data key is set" : "Twelve Data spot"}>
          {p.source === "spot" ? "spot" : "GC=F futures"}
        </Badge>
      </div>
      <dl className="grid grid-cols-3 divide-x divide-line border-b border-line">
        {([["Limit", px(p.limit), ""], ["Stop", px(p.stop), "text-down"], ["Target", p.target != null ? px(p.target) : "none", "text-up"]] as const).map(([k, val, ink]) => (
          <div key={k} className="px-3 py-2"><dt className="eyebrow">{k}</dt><dd className={`num mt-0.5 text-[14px] ${ink}`}>{val}</dd></div>
        ))}
      </dl>
      <p className="border-b border-line px-3 py-1.5 text-xs text-muted">
        Last price <span className="num text-fg">{px(p.last_price)}</span> ({age(p.price_age_seconds)}). A {order.side === "buy" ? "buy fills at the ask" : "sell fills at the bid"},
        assumed <span className="num">${p.spread?.toFixed(2)}</span> spread plus <span className="num">${p.slippage?.toFixed(2)}</span> slippage. Out by the flat-by time.
      </p>
      {p.card ? <div className="border-b border-line px-3 py-2.5"><WhyCard card={p.card} /></div>
        : p.why && <p className="border-b border-line px-3 py-1.5 text-xs text-muted">Why: <span className="text-fg">{p.why}</span></p>}
      {v && (
        <div className={`space-y-1.5 px-3 py-2.5 text-[12.5px] ${pass ? "bg-up/[0.05]" : "bg-down/[0.06]"}`}>
          <p className={`flex items-center gap-1.5 font-semibold ${pass ? "text-up" : "text-down"}`}><IconShield size={14} /> Risk check: {v.verdict}</p>
          {v.numbers.max_loss != null && (
            <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs sm:grid-cols-4">
              {([["Max loss", usd(v.numbers.max_loss)], ["Slippage at stop", usd(v.numbers.costs_at_stop)], ["Position", usd(v.numbers.value)],
                 ["Reward : risk", v.numbers.reward_risk != null ? String(v.numbers.reward_risk) : "–"]] as const).map(([k, n]) => (
                <div key={k}><dt className="text-subtle">{k}</dt><dd className="num text-fg">{n}</dd></div>
              ))}
            </dl>
          )}
          {v.reasons.length > 0 && <ul className="space-y-0.5 text-down">{v.reasons.map((r) => <li key={r} className="flex gap-1.5"><IconX size={13} className="mt-0.5 shrink-0" />{r}</li>)}</ul>}
          {v.warnings.map((w) => <p key={w} className="text-xs text-muted">Note: {w}</p>)}
        </div>
      )}
      {v?.verdict === "BLOCK" && order.state === "proposed" && (v.overridable ? (
        <div className="space-y-2 border-t border-line px-3 py-2.5">
          <p className="text-xs text-muted">Paper only: you can take it anyway. It is recorded as <span className="text-warn">rules not followed</span>, with these reasons, and still counts toward your daily and monthly losses.</p>
          <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} placeholder="Why you are taking it anyway (optional)" aria-label="Why you are overriding the risk check" className="field text-xs" />
          <HoldButton onDone={() => act("approve", true)} disabled={busy} className="w-full border-warn/60 text-warn">Hold to take it anyway (paper)</HoldButton>
        </div>
      ) : !!v.hard.length && <p className="border-t border-line px-3 py-2 text-xs text-muted">Cannot be taken even on paper: {v.hard.join("; ")}.</p>)}
      {error && <p className="px-3 py-2 text-xs text-down">{error}</p>}
      <div className="flex gap-2 border-t border-line px-3 py-2.5">
        {pass && order.state === "proposed" && (
          <button disabled={busy} onClick={() => act("approve")} className="btn btn-primary flex-1"><IconCheck size={14} />{busy ? "Confirming…" : "Confirm paper order"}</button>
        )}
        <button disabled={busy} onClick={() => (order.state === "proposed" ? act("cancel") : onDone(null))} className={`btn ${pass ? "" : "flex-1"}`}>{pass ? "Cancel" : "Close"}</button>
      </div>
    </div>
  );
}

export function GlobalPaper() {
  const sb = supabase();
  const { toast, confirm } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<GlobalPaperOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState<GlobalOrder | null>(null);
  const [form, setForm] = useState({ side: "buy", stop: "", target: "", lots: "", why: "" });
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [openTrade, setOpenTrade] = useState<string | null>(null);

  useEffect(() => {
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => setSession(data.session));
  }, [sb]);

  const load = useCallback(() => api<GlobalPaperOverview>("/global/paper").then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message)), []);

  useEffect(() => {
    if (!session) return;
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [session, load]);

  async function post<T>(path: string, body?: unknown): Promise<T | null> {
    try {
      const out = await api<T>(path, { method: "POST", body });
      await load();
      return out;
    } catch (e) {
      toast("error", "Action failed", (e as Error).message);
      return null;
    }
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="XAUUSD paper trading" />;

  const a = data?.account;
  const l = data?.limits;
  const working = data?.orders.filter((o) => o.state === "working" || o.state === "proposed") ?? [];
  const recentOrders = data?.orders.filter((o) => !["working", "proposed"].includes(o.state)).slice(0, 12) ?? [];

  async function killSwitch() {
    const ok = await confirm({
      title: "Activate the XAUUSD kill switch?",
      body: "Cancels every ticket and working entry, closes every open global position at the next bar, and blocks new entries until the next trading day (17:00 New York, 03:30 IST in winter). NSE paper and live are not touched: they have their own switch.",
      confirm: "Activate kill switch",
    });
    if (ok && (await post("/global/paper/kill-switch"))) toast("success", "Kill switch on", "Orders cancelled, positions closing, no new entries this trading day.");
  }

  return (
    <>
      <PageHeader
        title="XAUUSD paper trading"
        subtitle="A $10,000 paper account in dollars, apart from NSE. Fills are simulated on 1-minute bars with an assumed spread and slippage, checked every 20 s."
        meta={data && <>
          <Badge tone={data.market.is_open ? "up" : "neutral"} title={data.market.reason ?? undefined}>{data.market.is_open ? `Open · ${data.market.session?.replace("_", " ")}` : "Closed"}</Badge>
          <Badge tone={data.price_source === "spot" ? "info" : "warn"}>{data.price_source === "spot" ? "Spot prices" : "GC=F futures prices"}</Badge>
        </>}
        actions={<button onClick={killSwitch} className="btn btn-danger"><IconPower size={14} />Kill switch</button>}
      />

      {error && <Notice tone="error" title="Could not load the XAUUSD paper account">{error}</Notice>}
      {data?.loop.error && <Notice tone="warn" title="The paper loop is failing">{data.loop.error}</Notice>}
      {a?.kill_switch && <Notice tone="error" title="Kill switch is on">No new entries until the next trading day.</Notice>}
      {data?.price_source === "futures" && (
        <Notice tone="warn" title="Priced from COMEX futures, not spot">
          No Twelve Data key is set, so paper trades use Yahoo&apos;s <code>GC=F</code>, which runs about 10 minutes late and sits a few dollars
          away from spot. Fills still follow the bars in order. Add <code>TWELVEDATA_API_KEY</code> to <code>backend/.env</code> for spot.
        </Notice>
      )}

      <StatStrip cols={6}>
        <Stat label="Capital" loading={!a} value={a && usd(a.capital)} sub={a && <><UsdPnl v={a.capital - a.starting_capital} /> <span className="text-subtle">since start</span></>} />
        <Stat label="Today, after costs" loading={!a} value={a && <UsdPnl v={a.realized_today} plain />} tone={a ? (a.realized_today > 0 ? "up" : a.realized_today < 0 ? "down" : null) : null} sub="day rolls at 17:00 New York" />
        <Stat label="This month" loading={!a} value={a && <UsdPnl v={a.realized_month} plain />} tone={a ? (a.realized_month > 0 ? "up" : a.realized_month < 0 ? "down" : null) : null} />
        <Stat label="Open risk" loading={!a} value={a && usd(a.open_risk)} />
        <Stat label="Entries today" loading={!a} value={a && `${a.entries_today}${l ? ` / ${l.max_trades_per_day}` : ""}`} />
        <Stat label={`${data?.price.symbol ?? "XAUUSD"} last`} loading={!data} value={data && <span className="num">{px(data.price.last)}</span>} sub={data ? age(data.price.age_seconds) : undefined} />
      </StatStrip>

      <div className="grid gap-4 xl:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-8">
          <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Open positions</h2>{data && <Badge>{data.positions.length}</Badge>}</div>} flush>
            {!data ? <SkeletonRows rows={2} /> : !data.positions.length ? <Empty title="No open positions" icon={<IconWallet size={16} />}>Positions appear here once a paper order fills.</Empty> : (
              <TableWrap>
                <table className="dtable">
                  <thead><tr><th>Symbol</th><th>Side</th><th className="r">Lots</th><th className="r">Entry</th><th className="r">Last</th><th className="r">Stop</th><th className="r">Target</th><th className="r">Unrealized</th><th>Since</th><th></th></tr></thead>
                  <tbody>
                    {data.positions.map((t: GlobalTrade) => (
                      <tr key={t.id} title={t.entry_context?.why ?? undefined}>
                        <td className="font-medium">{t.symbol}{t.entry_context?.override ? <span className="badge badge-warn ml-2" title="Taken although the risk check blocked it">override</span> : null}</td>
                        <td><Side side={t.side} /></td><td className="r">{t.lots}</td><td className="r">{px(t.entry)}</td>
                        <td className="r">{px(t.last)}<span className="block text-2xs text-subtle">{age(t.price_age_seconds)}</span></td>
                        <td className="r text-down/90">{px(t.stop)}</td><td className="r text-up/90">{t.target != null ? px(t.target) : "–"}</td>
                        <td className="r"><UsdPnl v={t.unrealized} /></td><td className="text-muted">{time(t.entry_ts)}</td>
                        <td className="text-right">
                          <button onClick={async () => {
                            if (!(await confirm({ title: `Close ${t.symbol}?`, body: "An exit fills at the next 1-minute bar's open, at the bid for a long and the ask for a short. Exits are never blocked.", confirm: "Close position", tone: "primary" }))) return;
                            if (await post(`/global/paper/positions/${t.id}/close`)) toast("success", `Closing ${t.symbol}`, "Exit order working.");
                          }} className="btn btn-sm">Close</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </Panel>

          <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Working orders</h2>{data && <Badge>{working.length}</Badge>}</div>} flush>
            {!data ? <SkeletonRows rows={2} /> : !working.length ? <Empty title="No working orders">Use the order ticket to make one.</Empty> : (
              <TableWrap>
                <table className="dtable">
                  <thead><tr><th>State</th><th>Order</th><th className="r">Lots</th><th className="r">Limit</th><th className="r">Stop</th><th>Placed</th><th></th></tr></thead>
                  <tbody>
                    {working.map((o) => (
                      <tr key={o.id}>
                        <td><StateBadge state={o.state} /></td>
                        <td>{o.leg === "exit" ? <span className="text-warn">Exit · {o.params.reason}</span> : <Side side={o.side} />}</td>
                        <td className="r">{o.lots}</td>
                        <td className="r">{o.params.limit != null ? px(o.params.limit) : <span className="text-subtle">next open</span>}</td>
                        <td className="r">{px(o.params.stop)}</td>
                        <td className="text-muted">{time(o.approved_at ?? o.created_at)}</td>
                        <td className="text-right">
                          {o.state === "proposed" ? <button onClick={() => setTicket(o)} className="btn btn-sm">Review</button>
                            : o.leg === "entry" && <button onClick={async () => { if (await post(`/global/paper/orders/${o.id}/cancel`)) toast("info", "Order cancelled"); }} className="btn btn-sm btn-danger">Cancel</button>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </Panel>

          <Panel title="Closed trades · click one for why, the post-mortem and notes" flush>
            {!data ? <SkeletonRows rows={3} /> : !data.trades.length ? <Empty title="No closed trades yet" /> : (
              <TableWrap max="420px">
                <table className="dtable">
                  <thead><tr><th>Day</th><th>Side</th><th className="r">Lots</th><th className="r">Entry</th><th className="r">Exit</th><th>Exit reason</th><th className="r">Costs</th><th className="r">Net</th><th className="r">R</th><th>Prices</th></tr></thead>
                  <tbody>
                    {data.trades.map((t) => (
                      <Fragment key={t.id}>
                      <tr onClick={() => setOpenTrade(openTrade === t.id ? null : t.id)} className="cursor-pointer" aria-expanded={openTrade === t.id}
                        title="Show why, the post-mortem and your notes">
                        <td className="text-muted">{time(t.entry_ts)}</td><td><Side side={t.side} /></td><td className="r">{t.lots}</td>
                        <td className="r">{px(t.entry)}</td><td className="r">{px(t.exit)}</td>
                        <td>{t.exit_reason}{t.rules_followed === false && <span className="badge badge-warn ml-2">rules not followed</span>}</td>
                        <td className="r text-muted">{usd(t.costs)}</td><td className="r"><UsdPnl v={t.net_pnl} /></td><td className="r"><RValue v={t.r_multiple} /></td>
                        <td className="text-muted">{t.price_source === "spot" ? "spot" : "GC=F"}</td>
                      </tr>
                      {openTrade === t.id && <tr><td colSpan={10} className="bg-canvas p-0"><TradeJournalView trade={t} /></td></tr>}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </Panel>

          <GlobalReviews />

          {recentOrders.length > 0 && (
            <Panel title="Recent orders" flush>
              <TableWrap>
                <table className="dtable">
                  <thead><tr><th>State</th><th>Order</th><th className="r">Lots</th><th>When</th><th>Note</th></tr></thead>
                  <tbody>
                    {recentOrders.map((o) => (
                      <tr key={o.id}>
                        <td><StateBadge state={o.state} /></td><td>{o.leg === "exit" ? `Exit · ${o.params.reason}` : <Side side={o.side} />}</td>
                        <td className="r">{o.lots}</td><td className="text-muted">{time(o.created_at)}</td>
                        <td className="text-xs text-muted">{o.params.expired_because ?? (o.state === "rejected" ? o.risk_verdict?.reasons.join("; ") : "")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            </Panel>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-4 xl:col-span-4">
          <Panel title="Order ticket · XAUUSD">
            <p className="mb-3 text-xs text-muted">
              Leave lots empty and the size comes from your max loss per trade and position cap. Stop and target are on the price you would close at
              (the bid for a long, the ask for a short).
              {data?.window && <> Entries today {clock(data.window.entry_start)}–{clock(data.window.entry_end)} IST, flat by {clock(data.window.flat_by)} IST.</>}
            </p>
            <div className="grid grid-cols-2 gap-2">
              <div className="col-span-2 grid grid-cols-2 gap-1 rounded-md border border-line-strong bg-canvas p-0.5" role="group" aria-label="Side">
                {(["buy", "sell"] as const).map((s) => (
                  <button key={s} type="button" aria-pressed={form.side === s} onClick={() => setForm({ ...form, side: s })}
                    className={`h-7 cursor-pointer rounded-[4px] text-xs font-semibold transition-colors ${form.side === s ? (s === "buy" ? "bg-up text-[#03140d]" : "bg-down text-white") : "text-muted hover:text-fg"}`}>
                    {s === "buy" ? "Buy" : "Sell (short)"}
                  </button>
                ))}
              </div>
              <label><span className="field-label">Stop ($)</span>
                <input type="number" step="0.01" inputMode="decimal" value={form.stop} onChange={(e) => setForm({ ...form, stop: e.target.value })} className="field field-num" /></label>
              <label><span className="field-label">Target <span className="text-subtle">(optional)</span></span>
                <input type="number" step="0.01" inputMode="decimal" value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })} className="field field-num" /></label>
              <label className="col-span-2"><span className="field-label">Lots <span className="text-subtle">(optional, 0.01 = 1 oz)</span></span>
                <input type="number" step="0.01" min="0.01" inputMode="decimal" value={form.lots} onChange={(e) => setForm({ ...form, lots: e.target.value })} className="field field-num" /></label>
              <label className="col-span-2"><span className="field-label">Why this trade <span className="text-subtle">(goes in the journal)</span></span>
                <input maxLength={300} value={form.why} onChange={(e) => setForm({ ...form, why: e.target.value })} className="field text-xs" /></label>
              <button disabled={!form.stop} className="btn btn-primary col-span-2 mt-1" onClick={async () => {
                const o = await post<GlobalOrder>("/global/paper/tickets", {
                  symbol: "XAUUSD", side: form.side, stop: Number(form.stop), target: form.target ? Number(form.target) : null,
                  lots: form.lots ? Number(form.lots) : null, why: form.why,
                });
                if (o) setTicket(o);
              }}>Get ticket</button>
            </div>
            {ticket && <div className="mt-3"><Ticket order={ticket} onDone={(o) => {
              setTicket(null);
              load();
              if (o?.state === "working") toast("success", "Paper order working", `${o.side.toUpperCase()} ${o.lots} lot XAUUSD`);
              else if (o?.state === "rejected" && o.risk_verdict?.verdict === "BLOCK") toast("error", "Blocked at confirm", o.risk_verdict.reasons.join("; "));
            }} /></div>}
          </Panel>

          <MarketNotes />

          <Panel title="Risk usage">
            {!a || !l ? <SkeletonRows rows={4} cols={1} /> : (
              <div className="space-y-3.5">
                <Meter label="Daily loss + open risk" used={Math.max(0, -a.realized_today) + a.open_risk} limit={l.max_daily_loss} format={usd} />
                <Meter label="Monthly loss" used={Math.max(0, -a.realized_month)} limit={l.max_monthly_loss} format={usd} />
                <Meter label="Entries today" used={a.entries_today} limit={l.max_trades_per_day} />
                <Meter label="Losses in a row" used={a.consecutive_losses} limit={l.max_consecutive_losses} />
              </div>
            )}
          </Panel>

          {l && (
            <Panel title="Your XAUUSD limits" actions={
              <button disabled={!Object.keys(limits).length} className="btn btn-sm btn-primary" onClick={async () => {
                const body = Object.fromEntries(Object.entries(limits).map(([k, v]) => [k, /^\d{1,2}:\d{2}$/.test(v) ? v : Number(v)]));
                try {
                  await api("/global/paper/limits", { method: "PUT", body });
                  setLimits({});
                  await load();
                  toast("success", "Limits saved");
                } catch (e) {
                  toast("error", "Could not save", (e as Error).message);
                }
              }}>Save</button>
            }>
              <div className="grid grid-cols-2 gap-2">
                {(Object.keys(LIMIT_LABEL) as (keyof typeof l)[]).map((k) => (
                  <label key={k}><span className="field-label">{LIMIT_LABEL[k]}</span>
                    <input value={limits[k] ?? String(l[k])} onChange={(e) => setLimits({ ...limits, [k]: e.target.value })}
                      className="field field-num" inputMode={String(l[k]).includes(":") ? "text" : "decimal"} /></label>
                ))}
              </div>
              <p className="mt-2 text-2xs text-subtle">Times are in London and New York time, so they stay put when the clocks change. Assumed costs: ${data?.assumed.spread.toFixed(2)} spread, ${data?.assumed.slippage.toFixed(2)} slippage a fill.</p>
            </Panel>
          )}
        </div>
      </div>
    </>
  );
}
