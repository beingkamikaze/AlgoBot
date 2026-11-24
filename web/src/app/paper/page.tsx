"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { OrderTicket } from "@/components/order-ticket";
import { useFeedback } from "@/components/ui/feedback";
import { IconPower, IconRefresh, IconWallet } from "@/components/ui/icons";
import {
  Badge, Empty, Meter, Notice, Page, PageHeader, PageLoading, Panel, Pnl, RValue, Side, SignInRequired, SkeletonRows, Stat,
  StatStrip, StateBadge, TableWrap, Term, TERMS,
 px } from "@/components/ui/kit";
import { api, inr, type PaperOrder, type PaperOverview, type PaperTrade } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const time = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–";

const LIMIT_LABEL: Record<string, string> = {
  max_loss_per_trade: "Max loss per trade (₹)", max_daily_loss: "Max daily loss (₹)", max_monthly_loss: "Max monthly loss (₹)",
  max_trades_per_day: "Max trades per day", max_consecutive_losses: "Stop after losses in a row", max_open_positions: "Max open positions",
  max_margin_used_pct: "Max capital in use (%)", entry_start: "Entries from", entry_end: "No entries after", flat_by: "Flat by",
  max_loss_per_trade_fno: "Max loss per trade, F&O (₹)",
};

export default function PaperPage() {
  const sb = supabase();
  const { toast, confirm } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<PaperOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState<PaperOrder | null>(null);
  const [manual, setManual] = useState({ symbol: "", side: "buy", stop: "", target: "" });
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => setSession(data.session));
  }, [sb]);

  const load = useCallback(
    () =>
      api<PaperOverview>("/paper")
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e: Error) => setError(e.message)),
    [],
  );

  useEffect(() => {
    if (!session) return;
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [session, load]);

  async function post<T>(path: string, body?: unknown): Promise<T | null> {
    setMsg(null);
    try {
      const out = await api<T>(path, { method: "POST", body });
      await load();
      return out;
    } catch (e) {
      setMsg((e as Error).message);
      toast("error", "Action failed", (e as Error).message);
      return null;
    }
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="Paper trading" />;

  const a = data?.account;
  const l = data?.limits;
  const working = data?.orders.filter((o) => o.state === "working" || o.state === "proposed") ?? [];

  async function killSwitch() {
    const ok = await confirm({
      title: "Activate the kill switch?",
      body: "Cancels every working order, closes every open position at the next price, and blocks new entries until tomorrow. Live positions are closed too.",
      confirm: "Activate kill switch",
    });
    if (!ok) return;
    if (await post("/paper/kill-switch")) toast("success", "Kill switch on", "Orders cancelled, positions closing, no new entries today.");
  }

  return (
    <Page wide>
      <PageHeader
        title="Paper trading"
        subtitle="Simulated fills on free 1-minute data with Zerodha charges. Checked every 20 s while the market is open."
        meta={data && <Badge tone={data.market.is_open ? "up" : "neutral"}>{data.market.is_open ? "Market open" : "Market closed"}</Badge>}
        actions={<>
          <button onClick={async () => { if (await post("/paper/tick")) toast("success", "Checked fills"); }} className="btn"><IconRefresh size={14} />Check fills now</button>
          <button onClick={killSwitch} className="btn btn-danger"><IconPower size={14} />Kill switch</button>
        </>}
      />

      {error && <Notice tone="error" title="Could not load the paper account">{error}</Notice>}
      {msg && <Notice tone="error">{msg}</Notice>}
      {a?.kill_switch && <Notice tone="error" title="Kill switch is on">No new entries until tomorrow.</Notice>}

      <StatStrip cols={6}>
        <Stat label="Capital" loading={!a} value={a && inr(a.capital)} sub={a && <><Pnl v={a.capital - a.starting_capital} /> <span className="text-subtle">since start</span></>} />
        <Stat label="Free cash" loading={!a} value={a && inr(a.cash_free)} />
        <Stat label="Today, after charges" loading={!a} value={a && <Pnl v={a.realized_today} plain />} tone={a ? (a.realized_today > 0 ? "up" : a.realized_today < 0 ? "down" : null) : null} />
        <Stat label="This month" loading={!a} value={a && <Pnl v={a.realized_month} plain />} tone={a ? (a.realized_month > 0 ? "up" : a.realized_month < 0 ? "down" : null) : null} />
        <Stat label={<Term tip={TERMS.openRisk}>Open risk</Term>} loading={!a} value={a && inr(a.open_risk)} />
        <Stat label="Entries today" loading={!a} value={a && `${a.entries_today}${l ? ` / ${l.max_trades_per_day}` : ""}`} />
      </StatStrip>

      <div className="grid gap-4 xl:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-8">
          <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Open positions</h2>{data && <Badge>{data.positions.length}</Badge>}</div>} flush>
            {!data ? <SkeletonRows rows={3} /> : !data.positions.length ? <Empty title="No open positions" icon={<IconWallet size={16} />}>Positions appear here once a paper order fills.</Empty> : (
              <TableWrap>
                <table className="dtable">
                  <thead><tr><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Last</th><th className="r">Stop</th><th className="r">Target</th><th className="r">Unrealized</th><th>Since</th><th></th></tr></thead>
                  <tbody>
                    {data.positions.map((t: PaperTrade) => {
                      const opt = t.entry_context && (t.entry_context as { kind?: string }).kind === "option";
                      return (
                        <tr key={t.id} title={t.entry_context?.summary ?? undefined}>
                          <td className="font-medium">{t.symbol}{(t.entry_context as { override?: unknown } | null)?.override ? <span className="badge badge-warn ml-2" title="Taken although the risk check blocked it">override</span> : null}</td><td><Side side={t.side} /></td>
                          <td className="r">{t.qty}</td><td className="r">{px(t.entry)}</td><td className="r">{px(t.last)}</td>
                          <td className="r text-down/90">{opt ? <>{t.underlying_stop != null && <span className="block">{px(t.underlying_stop)}</span>}<span className="text-2xs text-subtle">at {inr(t.stop ?? 0)}</span></> : px(t.stop)}</td>
                          <td className="r text-up/90">{opt ? <>{t.underlying_target != null && <span className="block">{px(t.underlying_target)}</span>}<span className="text-2xs text-subtle">at {inr(t.target ?? 0)}</span></> : px(t.target)}</td>
                          <td className="r"><Pnl v={t.unrealized} /></td><td className="text-muted">{time(t.entry_ts)}</td>
                          <td className="text-right whitespace-nowrap">
                            <Link href={`/monitor?trade=${t.id}`} className="btn btn-sm btn-ghost mr-1">Monitor</Link>
                            <button onClick={async () => {
                              if (!(await confirm({ title: `Close ${t.symbol}?`, body: "An exit order fills at the next 1-minute bar's open. Exits are never blocked.", confirm: "Close position", tone: "primary" }))) return;
                              if (await post(`/paper/positions/${t.id}/close`)) toast("success", `Closing ${t.symbol}`, "Exit order working.");
                            }} className="btn btn-sm">Close</button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </Panel>

          <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Working orders</h2>{data && <Badge>{working.length}</Badge>}</div>} flush>
            {!data ? <SkeletonRows rows={2} /> : !working.length ? (
              <Empty title="No working orders">Paper-trade a signal from <Link href="/signals" className="link">Signals</Link>, or use the order ticket.</Empty>
            ) : (
              <TableWrap>
                <table className="dtable">
                  <thead><tr><th>State</th><th>Order</th><th className="r">Qty</th><th>Symbol</th><th className="r">Limit</th><th>Placed</th><th></th></tr></thead>
                  <tbody>
                    {working.map((o) => (
                      <tr key={o.id}>
                        <td><StateBadge state={o.state} /></td>
                        <td>{o.params.leg === "exit" ? <span className="text-warn">Exit · {o.params.reason}</span> : <Side side={o.params.side} />}</td>
                        <td className="r">{o.params.qty}</td><td className="font-medium">{o.params.symbol}</td>
                        <td className="r">{o.params.limit != null ? px(o.params.limit) : (o.params as { kind?: string }).kind === "option" ? <span className="text-subtle">at bid / ask</span> : <span className="text-subtle">next open</span>}</td>
                        <td className="text-muted">{time(o.approved_at ?? o.created_at)}</td>
                        <td className="text-right">
                          {o.state === "proposed" ? (
                            <button onClick={() => setTicket(o)} className="btn btn-sm">Review</button>
                          ) : o.params.leg === "entry" && (
                            <button onClick={async () => { if (await post(`/paper/orders/${o.id}/cancel`)) toast("info", "Order cancelled"); }} className="btn btn-sm btn-danger">Cancel</button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4 xl:col-span-4">
          <Panel title="Order ticket">
            <p className="mb-3 text-xs text-muted">Size comes from your max loss per trade; the risk engine checks it like any other order.</p>
            <div className="grid grid-cols-2 gap-2">
              <label className="col-span-2"><span className="field-label">Symbol</span>
                <input placeholder="RELIANCE or NIFTY-FUT" value={manual.symbol} onChange={(e) => setManual({ ...manual, symbol: e.target.value.toUpperCase() })} className="field num uppercase" /></label>
              <div className="col-span-2 grid grid-cols-2 gap-1 rounded-md border border-line-strong bg-canvas p-0.5" role="group" aria-label="Side">
                {(["buy", "sell"] as const).map((s) => (
                  <button key={s} type="button" aria-pressed={manual.side === s} onClick={() => setManual({ ...manual, side: s })}
                    className={`h-7 cursor-pointer rounded-[4px] text-xs font-semibold transition-colors ${manual.side === s ? (s === "buy" ? "bg-up text-[#03140d]" : "bg-down text-white") : "text-muted hover:text-fg"}`}>
                    {s === "buy" ? "Buy" : "Sell (short)"}
                  </button>
                ))}
              </div>
              <label><span className="field-label">Stop</span>
                <input type="number" step="any" inputMode="decimal" value={manual.stop} onChange={(e) => setManual({ ...manual, stop: e.target.value })} className="field field-num" /></label>
              <label><span className="field-label">Target <span className="text-subtle">(optional)</span></span>
                <input type="number" step="any" inputMode="decimal" value={manual.target} onChange={(e) => setManual({ ...manual, target: e.target.value })} className="field field-num" /></label>
              <button
                disabled={!manual.symbol || !manual.stop}
                onClick={async () => {
                  const o = await post<PaperOrder>("/paper/tickets/manual", { symbol: manual.symbol, side: manual.side, stop: Number(manual.stop), target: manual.target ? Number(manual.target) : null });
                  if (o) setTicket(o);
                }}
                className="btn btn-primary col-span-2 mt-1"
              >
                Get ticket
              </button>
            </div>
            {ticket && <div className="mt-3"><OrderTicket order={ticket} onDone={(o) => {
              setTicket(null); load();
              if (o?.state === "working") toast("success", "Paper order working", `${o.params.side.toUpperCase()} ${o.params.qty} ${o.params.symbol}`);
            }} /></div>}
          </Panel>

          <Panel title="Risk usage">
            {!a || !l ? <SkeletonRows rows={4} cols={1} /> : (
              <div className="space-y-3.5">
                <Meter label="Daily loss + open risk" used={Math.max(0, -a.realized_today) + a.open_risk} limit={l.max_daily_loss} format={inr} />
                <Meter label="Entries today" used={a.entries_today} limit={l.max_trades_per_day} />
                <Meter label="Losses in a row" used={a.consecutive_losses} limit={l.max_consecutive_losses} />
                <Meter label="Open positions" used={data!.positions.length} limit={l.max_open_positions} />
              </div>
            )}
          </Panel>
        </div>
      </div>

      {l && (
        <Panel id="limits" title="Your limits" actions={
          <button
            disabled={!Object.keys(limits).length}
            onClick={async () => {
              const body = Object.fromEntries(Object.entries(limits).map(([k, v]) => [k,
                k === "allow_naked_short_options" ? v === "true" : k === "max_loss_per_trade_fno" && v.trim() === "" ? null :
                /^\d{1,2}:\d{2}$/.test(v) ? v : Number(v)]));
              setMsg(null);
              try {
                await api("/paper/limits", { method: "PUT", body });
                setLimits({});
                await load();
                toast("success", "Limits saved");
              } catch (e) {
                setMsg((e as Error).message);
                toast("error", "Limits not saved", (e as Error).message);
              }
            }}
            className="btn btn-primary btn-sm"
          >
            Save limits
          </button>
        }>
          <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
            {(Object.keys(l) as (keyof typeof l)[]).filter((k) => k !== "allow_naked_short_options").map((k) => {
              const fno = k === "max_loss_per_trade_fno";
              const changed = k in limits;
              return (
                <label key={k} title={fno ? "Futures and options. Empty = same as stocks." : undefined}>
                  <span className="field-label">{LIMIT_LABEL[k] ?? k.replaceAll("_", " ")}{changed && <span className="ml-1 text-accent">•</span>}</span>
                  <input value={limits[k] ?? (l[k] == null ? "" : String(l[k]))} placeholder={fno ? "same as stocks" : undefined}
                    disabled={fno && !data?.fno_limit_available}
                    onChange={(e) => setLimits({ ...limits, [k]: e.target.value })}
                    className="field field-num" />
                </label>
              );
            })}
          </div>
          {!data?.fno_limit_available && <p className="mt-2 text-xs text-muted">Run supabase/migrations/0003_fno_limits.sql to set a separate F&amp;O max loss.</p>}
          <label className="mt-4 flex items-start gap-2 rounded-md border border-line bg-canvas/50 p-2.5 text-xs">
            <input type="checkbox" className="mt-0.5" checked={limits.allow_naked_short_options ? limits.allow_naked_short_options === "true" : l.allow_naked_short_options}
              onChange={async (e) => {
                const on = e.target.checked;
                if (on && !(await confirm({ title: "Allow naked short options?", body: "Their loss is NOT capped: a big move can cost far more than your max loss per trade.", confirm: "Allow naked shorts" }))) return;
                setLimits({ ...limits, allow_naked_short_options: String(on) });
              }} />
            <span><span className="font-medium text-fg">Allow naked short options</span> <span className="text-muted">(unlimited loss). Off by default.</span></span>
          </label>
        </Panel>
      )}

      <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Closed trades</h2>{data && <Badge>{data.trades.length}</Badge>}</div>} flush>
        {!data ? <SkeletonRows rows={4} /> : !data.trades.length ? <Empty title="No closed trades yet" /> : (
          <TableWrap max="520px">
            <table className="dtable">
              <thead><tr><th>Entry</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Exit</th><th>Why out</th><th className="r">Charges</th><th className="r">Net</th><th className="r"><span title={TERMS.R}>R</span></th><th>Strategy</th></tr></thead>
              <tbody>
                {data.trades.map((t) => (
                  <tr key={t.id} title={t.entry_context?.summary ?? undefined}>
                    <td className="text-muted">{time(t.entry_ts)}</td><td className="font-medium">{t.symbol}{(t.entry_context as { override?: unknown } | null)?.override ? <span className="badge badge-warn ml-2" title="Taken although the risk check blocked it">override</span> : null}</td><td><Side side={t.side} /></td>
                    <td className="r">{t.qty}</td><td className="r">{px(t.entry)}</td><td className="r">{px(t.exit)}</td>
                    <td className="text-muted">{t.exit_reason}</td><td className="r text-muted">{t.costs?.toFixed(0)}</td>
                    <td className="r"><Pnl v={t.net_pnl} /></td><td className="r"><RValue v={t.r_multiple} /></td>
                    <td className="text-muted">{t.entry_context?.strategy ?? t.entry_context?.source ?? "manual"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>
    </Page>
  );
}
