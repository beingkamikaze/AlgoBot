"use client";

import type { Session } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { useFeedback } from "@/components/ui/feedback";
import { IconCheck, IconPower, IconRadio, IconRefresh, IconShield } from "@/components/ui/icons";
import { Badge, Check, Empty, Notice, Page, PageHeader, PageLoading, Panel, Pnl, RValue, Side, SignInRequired, SkeletonRows, TableWrap, px } from "@/components/ui/kit";
import { api, inr, type LiveOverview } from "@/lib/api";
import { supabase } from "@/lib/supabase";


/** Two-factor sign-in (TOTP) for this app: needed to arm live trading and to send live orders. */
function TwoFactor({ onChange }: { onChange: () => void }) {
  const sb = supabase();
  const [level, setLevel] = useState<{ current: string | null; next: string | null } | null>(null);
  const [factorId, setFactorId] = useState<string | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    if (!sb) return;
    const { data } = await sb.auth.mfa.getAuthenticatorAssuranceLevel();
    setLevel({ current: data?.currentLevel ?? null, next: data?.nextLevel ?? null });
    const f = await sb.auth.mfa.listFactors();
    setFactorId(f.data?.totp[0]?.id ?? null);
  }
  useEffect(() => {
    let stop = false;
    (async () => {
      if (!sb) return;
      const { data } = await sb.auth.mfa.getAuthenticatorAssuranceLevel();
      const f = await sb.auth.mfa.listFactors();
      if (stop) return;
      setLevel({ current: data?.currentLevel ?? null, next: data?.nextLevel ?? null });
      setFactorId(f.data?.totp[0]?.id ?? null);
    })();
    return () => { stop = true; };
  }, [sb]);

  async function enroll() {
    setError(null);
    const { data, error } = await sb!.auth.mfa.enroll({ factorType: "totp", friendlyName: `TradingBot ${Date.now()}` });
    if (error) return setError(error.message);
    setFactorId(data.id); setQr(data.totp.qr_code);
  }
  async function verify() {
    setError(null);
    const ch = await sb!.auth.mfa.challenge({ factorId: factorId! });
    if (ch.error) return setError(ch.error.message);
    const v = await sb!.auth.mfa.verify({ factorId: factorId!, challengeId: ch.data.id, code });
    if (v.error) return setError(v.error.message);
    setQr(null); setCode(""); await refresh(); onChange();
  }

  if (!level) return <div className="skeleton h-8 w-48" />;
  if (level.current === "aal2") return <p className="flex items-center gap-1.5 text-[12.5px] text-up"><IconShield size={14} />Two-factor verified for this sign-in.</p>;
  return (
    <div className="space-y-3 text-[12.5px]">
      {!factorId && !qr && (
        <>
          <p className="text-muted">Live trading needs two-factor sign-in for this app, using an authenticator app such as Google Authenticator.</p>
          <button onClick={enroll} className="btn"><IconShield size={14} />Set up two-factor</button>
        </>
      )}
      {qr && (
        <div className="flex items-center gap-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={qr} alt="QR code: scan it with your authenticator app" className="size-36 rounded-md bg-white p-2" />
          <p className="text-xs text-muted">Scan this with your authenticator app, then enter the 6-digit code it shows.</p>
        </div>
      )}
      {factorId && (
        <div className="flex items-center gap-2">
          <input value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="000000" inputMode="numeric" autoComplete="one-time-code"
            aria-label="6-digit code" className="field num w-32 text-center tracking-[0.3em]" />
          <button disabled={code.length !== 6} onClick={verify} className="btn btn-primary">Verify</button>
        </div>
      )}
      {error && <p className="text-xs text-down">{error}</p>}
    </div>
  );
}

export default function LivePage() {
  const sb = supabase();
  const { toast, confirm: askConfirm } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [data, setData] = useState<LiveOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [confirm, setConfirm] = useState("");
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    sb?.auth.getSession().then(({ data }) => setSession(data.session));
    const q = new URLSearchParams(window.location.search);
    const k = q.get("kite");
    if (k) {
      const msg = k === "ok" ? "Logged in to Kite for today." : k === "cancelled" ? "Kite login cancelled." : `Kite login failed: ${q.get("detail") ?? ""}`;
      queueMicrotask(() => setNote(msg));
      window.history.replaceState(null, "", "/live");
    }
  }, [sb]);

  useEffect(() => {
    if (!session) return;
    let stop = false;
    const load = () => api<LiveOverview>("/live").then((d) => {
      if (stop) return;
      setData(d); setError(null);
      setLimits((l) => Object.keys(l).length ? l : Object.fromEntries(
        (["live_max_loss_per_trade", "live_max_daily_loss", "live_max_trades_per_day", "live_max_order_value"] as const)
          .map((k) => [k, String(d.limits[k] ?? "")])));
    }).catch((e: Error) => !stop && setError(e.message));
    load();
    const id = setInterval(load, 5000);
    return () => { stop = true; clearInterval(id); };
  }, [session, tick]);

  async function run(path: string, body?: unknown, method: "POST" | "PUT" = "POST") {
    setBusy(true); setError(null);
    try {
      const out = await api<Record<string, unknown>>(path, { method, body: body ?? {} });
      setTick((t) => t + 1);
      return out;
    } catch (e) { setError((e as Error).message); toast("error", "Action failed", (e as Error).message); } finally { setBusy(false); }
  }
  async function kiteLogin() {
    const out = await api<{ url: string }>("/live/kite/login").catch((e: Error) => { setError(e.message); return null; });
    if (out) window.location.href = out.url;
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="Live trading" />;

  const g = Object.fromEntries((data?.gates ?? []).map((x) => [x.gate, x]));
  const armed = g.armed?.ok;
  const allOk = !!data && data.gates.every((x) => x.ok);
  const okCount = data?.gates.filter((x) => x.ok).length ?? 0;

  async function arm() {
    const ok = await askConfirm({ title: "Arm live trading for today?", tone: "live", confirm: "Arm live trading",
      body: "Tickets will offer a real-money step that sends orders to Zerodha. Each order still needs your confirmation. Arming ends at midnight, with Disarm, or with the kill switch." });
    if (!ok) return;
    if (await run("/live/arm", { confirm })) toast("success", "Live trading armed for today");
  }
  async function kill() {
    const ok = await askConfirm({ title: "Activate the kill switch?", confirm: "Activate kill switch", requireText: "KILL",
      body: "Cancels live entries at Zerodha, closes every live position (and paper ones) at a marketable price, disarms live trading and blocks new entries for today." });
    if (ok && await run("/live/kill")) toast("success", "Kill switch on", "Live entries cancelled, positions closing.");
  }

  return (
    <Page wide>
      <PageHeader title="Live trading" subtitle="Real money through Zerodha Kite. Every live order still needs your confirmation on its ticket; nothing is sent by itself. Options stay paper for now."
        meta={armed ? <Badge tone="live">Armed {data?.limits.live_armed_on}</Badge> : <Badge tone="neutral">Not armed</Badge>}
        actions={<>
          <button disabled={busy} onClick={async () => { if (await run("/live/sync")) toast("info", "Synced with Kite"); }} className="btn"><IconRefresh size={14} />Sync with Kite</button>
          <button disabled={busy} onClick={kill} className="btn btn-danger"><IconPower size={14} />Kill switch</button>
        </>} />
      {note && <Notice tone={note.startsWith("Logged in") ? "success" : note.includes("failed") ? "error" : "info"}>{note}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      {!!data?.alerts.length && (
        <Notice tone="error" title={`${data.alerts.length} alert${data.alerts.length > 1 ? "s" : ""} today`}>
          <ul className="num mt-1 space-y-0.5 text-xs">
            {data.alerts.map((a) => <li key={a.id}><span className="text-subtle">{new Date(a.ts).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })}</span> <span className="text-fg">{a.kind}</span> {JSON.stringify(a.detail)}</li>)}
          </ul>
        </Notice>
      )}

      <div className="grid gap-4 xl:grid-cols-12">
        <Panel className="xl:col-span-5" title="Before a live order can be sent"
          actions={data && <span className={`num text-xs ${allOk ? "text-up" : "text-muted"}`}>{okCount}/{data.gates.length} ready</span>}>
          {!data ? <SkeletonRows rows={7} cols={1} /> : (
            <ul className="space-y-2 text-[12.5px]">
              {data.gates.map((x) => <Check key={x.gate} ok={x.ok}><span className={x.ok ? "text-muted" : "text-fg"}>{x.detail}</span></Check>)}
            </ul>
          )}
        </Panel>

        <div className="grid gap-4 sm:grid-cols-2 xl:col-span-7">
          <Panel title="1 · Kite login (every trading day)">
            {data?.session.logged_in ? (
              <div className="space-y-2 text-[12.5px]">
                <p className="flex items-center gap-1.5 text-up"><IconCheck size={14} />Logged in as <span className="num text-fg">{data.session.kite_user_id}</span></p>
                <p className="text-xs text-muted">Session ends at 06:00 tomorrow.</p>
                {data.funds && (
                  <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line text-xs">
                    <div className="bg-surface px-2.5 py-2"><dt className="eyebrow">Funds</dt><dd className="num mt-0.5 text-[14px]">{inr(data.funds.net)}</dd></div>
                    <div className="bg-surface px-2.5 py-2"><dt className="eyebrow">Free</dt><dd className="num mt-0.5 text-[14px]">{inr(data.funds.available)}</dd></div>
                  </dl>
                )}
                {data.funds_error && <p className="text-xs text-down">{data.funds_error}</p>}
                <button onClick={() => run("/live/kite/logout")} className="btn btn-ghost btn-sm -ml-2">Forget today&apos;s Kite session</button>
              </div>
            ) : (
              <div className="space-y-3 text-[12.5px]">
                <p className="text-muted">You log in on kite.zerodha.com (password and 2FA there). This app only receives a token that expires at 06:00.</p>
                <button disabled={!g.kite_app?.ok || !g.database?.ok} onClick={kiteLogin} className="btn btn-primary">Log in to Kite</button>
              </div>
            )}
          </Panel>
          <Panel title="2 · Two-factor for this app">
            <TwoFactor onChange={() => setTick((t) => t + 1)} />
          </Panel>

          <section className={`panel sm:col-span-2 ${armed ? "border-live/70" : ""}`}>
            <div className="panel-head"><h2 className="panel-title">3 · Arm and limits</h2>
              <div className="ml-auto">{armed ? <button disabled={busy} onClick={() => run("/live/disarm")} className="btn btn-sm">Disarm</button> : null}</div>
            </div>
            <div className="space-y-3 p-3">
              {armed ? (
                <Notice tone="live" title={`Live trading armed for ${data?.limits.live_armed_on}`}>Tickets show a real-money step. It ends at midnight, with Disarm, or with the kill switch.</Notice>
              ) : (
                <div className="flex flex-wrap items-end gap-2">
                  <label><span className="field-label">Type LIVE to arm for today</span>
                    <input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="LIVE" className="field num w-28" autoComplete="off" /></label>
                  <button disabled={busy || confirm.trim().toUpperCase() !== "LIVE"} onClick={arm} className="btn btn-live">Arm for today</button>
                </div>
              )}
              <div className="grid grid-cols-2 gap-2 border-t border-line pt-3 md:grid-cols-4">
                {([["live_max_loss_per_trade", "Max loss / trade (₹)"], ["live_max_daily_loss", "Max loss / day (₹)"],
                   ["live_max_trades_per_day", "Max trades / day"], ["live_max_order_value", "Max order value (₹)"]] as const).map(([k, label]) => (
                  <label key={k}><span className="field-label">{label}</span>
                    <input value={limits[k] ?? ""} onChange={(e) => setLimits({ ...limits, [k]: e.target.value })} className="field field-num" inputMode="decimal" /></label>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <button disabled={busy} onClick={async () => { if (await run("/live/limits", Object.fromEntries(Object.entries(limits).map(([k, v]) => [k, Number(v)])), "PUT")) toast("success", "Live limits saved"); }}
                  className="btn btn-sm">Save live limits</button>
                <p className="text-2xs text-subtle">Live limits apply on top of your paper limits (the smaller wins). Start small: one Nifty lot needs a much bigger loss limit than ₹500.</p>
              </div>
            </div>
          </section>
        </div>
      </div>

      <Panel title={<div className="flex items-center gap-2"><h2 className="panel-title">Live positions</h2>{data && <Badge tone={data.positions.length ? "live" : "neutral"}>{data.positions.length}</Badge>}</div>} flush>
        {!data ? <SkeletonRows rows={2} /> : data.positions.length === 0 ? <Empty title="No live positions" icon={<IconRadio size={16} />} /> : (
          <TableWrap>
            <table className="dtable">
              <thead><tr><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Stop</th><th className="r">Target</th><th>Protection</th><th>State</th><th></th></tr></thead>
              <tbody>
                {data.positions.map((t) => (
                  <tr key={t.id}>
                    <td className="font-medium">{t.symbol}</td><td><Side side={t.side} /></td><td className="r">{t.qty}</td><td className="r">{px(t.entry)}</td>
                    <td className="r text-down">{px(t.entry_context?.stop)}</td><td className="r text-up">{px(t.entry_context?.target)}</td>
                    <td>{t.entry_context?.protected ? <Badge tone="up">Stop at Zerodha</Badge> : <Badge tone="live">No stop at Zerodha</Badge>}</td>
                    <td className="text-xs text-warn">{t.entry_context?.exiting ? `exiting: ${t.entry_context.exiting}` : ""}</td>
                    <td className="text-right">{!t.entry_context?.exiting && (
                      <button disabled={busy} onClick={async () => {
                        if (await askConfirm({ title: `Close ${t.symbol} at Zerodha?`, tone: "live", confirm: "Close live position", body: "Its stop order becomes a marketable limit order and is re-priced until it fills." }))
                          run(`/live/positions/${t.id}/close`);
                      }} className="btn btn-sm">Close</button>
                    )}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>

      <Panel title="Live trades" flush>
        {!data ? <SkeletonRows rows={3} /> : data.trades.length === 0 ? <Empty title="No live trades yet" /> : (
          <TableWrap max="420px">
            <table className="dtable">
              <thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Exit</th><th className="r">Net</th><th className="r">R</th><th>Why it ended</th></tr></thead>
              <tbody>
                {data.trades.map((t) => (
                  <tr key={t.id}>
                    <td className="num text-xs text-muted">{t.exit_ts ? new Date(t.exit_ts).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "short", timeStyle: "short" }) : ""}</td>
                    <td className="font-medium">{t.symbol}</td><td><Side side={t.side} /></td><td className="r">{t.qty}</td><td className="r">{px(t.entry)}</td><td className="r">{px(t.exit)}</td>
                    <td className="r"><Pnl v={t.net_pnl} /></td><td className="r"><RValue v={t.r_multiple} /></td><td className="text-muted">{t.exit_reason}</td>
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
