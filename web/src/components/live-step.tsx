"use client";

import { useEffect, useState } from "react";

import { IconAlert } from "@/components/ui/icons";
import { Side } from "@/components/ui/kit";
import { api, inr, liveReady, type LiveOverview, type LivePreview, type PaperOrder } from "@/lib/api";

/** The real-money path on a ticket: shown only when every live gate is open. Two steps: preview, then send. */
export function LiveStep({ order, onDone }: { order: PaperOrder; onDone: (o: PaperOrder | null) => void }) {
  const [live, setLive] = useState<LiveOverview | null>(null);
  const [pv, setPv] = useState<LivePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    api<LiveOverview>("/live").then((o) => { if (!stop) setLive(o); }).catch(() => {});   // not configured: no live step
    return () => { stop = true; };
  }, []);

  if (!liveReady(live) || order.state !== "proposed" || (order.params as { kind?: string }).kind === "option") return null;

  async function preview() {
    setBusy(true); setError(null);
    try { setPv(await api<LivePreview>(`/live/orders/${order.id}/preview`, { method: "POST" })); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function send() {
    if (!pv) return;
    setBusy(true); setError(null);
    try { onDone(await api<PaperOrder>(`/live/orders/${order.id}/approve`, { method: "POST", body: { qty: pv.qty } })); }
    catch (e) { setError((e as Error).message); setPv(null); } finally { setBusy(false); }
  }

  const ok = pv?.verdict.verdict === "PASS";
  return (
    <div className="mt-3 space-y-2 rounded-md border border-live/70 bg-live/[0.06] p-3 text-[12.5px]">
      <p className="flex items-center gap-1.5 text-2xs font-semibold tracking-wide text-live uppercase"><IconAlert size={13} />Real money · Zerodha</p>
      {!pv && (
        <button disabled={busy} onClick={preview} className="btn btn-danger w-full">
          {busy ? "Checking with Kite…" : "Check this as a live order"}
        </button>
      )}
      {pv && (
        <div className="space-y-2">
          <p className="flex flex-wrap items-center gap-2">
            <Side side={pv.side} />
            <span className="num font-semibold">{pv.qty} × {pv.exchange}:{pv.symbol}</span>
            <span className="num text-muted">limit {pv.limit} · stop {pv.stop}{pv.target ? ` · target ${pv.target}` : ""}</span>
          </p>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
            <dt className="text-subtle">Kite price</dt><dd className="num text-right">{pv.last_price ?? "–"}</dd>
            <dt className="text-subtle">Free funds</dt><dd className="num text-right">{inr(pv.funds.available)}</dd>
            {pv.margin != null && <><dt className="text-subtle">Margin</dt><dd className="num text-right">{inr(pv.margin)}</dd></>}
            <dt className="text-subtle">Live max loss / order cap</dt><dd className="num text-right">{inr(pv.live_limits.max_loss_per_trade)} / {inr(pv.live_limits.max_order_value)}</dd>
          </dl>
          {pv.management && (
            <p className="text-xs text-muted">
              Trade management, confirmed with this order: {pv.management}. The stop order at Kite is moved and
              partial / time exits are sent by the app, without asking again.
            </p>
          )}
          <p className={`font-semibold ${ok ? "text-up" : "text-down"}`}>
            Live risk check: {pv.verdict.verdict}
            {pv.verdict.numbers.max_loss != null && <span className="num font-normal text-muted"> · max loss {inr(pv.verdict.numbers.max_loss)} incl. charges</span>}
          </p>
          {pv.verdict.reasons.map((r) => <p key={r} className="text-down">{r}</p>)}
          {ok && (
            <button disabled={busy} onClick={send} className="btn btn-live w-full">
              {busy ? "Sending…" : `Send to Zerodha: ${pv.side} ${pv.qty} ${pv.symbol}`}
            </button>
          )}
          <p className="text-2xs text-subtle">After the fill a stop-loss order goes to Zerodha at once. Target, 15:15 and Close all use that same order.</p>
        </div>
      )}
      {error && <p className="text-down">{error}</p>}
    </div>
  );
}
