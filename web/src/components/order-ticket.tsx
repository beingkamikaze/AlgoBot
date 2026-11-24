"use client";

import { useState } from "react";

import { LiveStep } from "@/components/live-step";
import { IconCheck, IconShield, IconX } from "@/components/ui/icons";
import { Badge, HoldButton, Side } from "@/components/ui/kit";
import { api, inr, type PaperOrder } from "@/lib/api";

const fmt = (n: number | null | undefined) => (n == null ? "–" : n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

/** The confirm step: the exact order, the risk engine's verdict, and two buttons. Nothing is placed before "Confirm". */
export function OrderTicket({ order, onDone }: { order: PaperOrder; onDone: (o: PaperOrder | null) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const p = order.params;
  const v = order.risk_verdict;
  const src = (v?.numbers ?? {}) as Record<string, unknown>;     // margin_source / charges_source: "Kite", "estimate", "full value"
  const pass = v?.verdict === "PASS";
  // an option structure ticket: premium paid and exits in rupees of P&L, not share prices
  const raw = p as unknown as { kind?: string; structure?: { net_premium: number; qty: number; as_of?: string; source?: string; legs?: { tradingsymbol?: string; price: number }[] }; exits?: { type?: string; stop_pnl: number; target_pnl: number; underlying_stop?: number | null; underlying_target?: number | null }; data_symbol?: string };
  const legs = raw.structure?.legs ?? [];
  // one contract: name it exactly (the structure label leaves out the strike); spreads keep the label
  const opt = raw.kind === "option" ? {
    premium: raw.structure ? Math.abs(raw.structure.net_premium) * raw.structure.qty : null,
    stop: raw.exits?.stop_pnl ?? null, target: raw.exits?.target_pnl ?? null,
    contract: legs.length === 1 ? legs[0].tradingsymbol ?? null : null,
    unit: legs.length === 1 ? legs[0].price : null,
    debit: raw.exits?.type === "debit",
    levels: raw.exits?.underlying_stop != null ? `${raw.data_symbol} crosses ${raw.exits.underlying_stop}${raw.exits.underlying_target != null ? ` (stop) or ${raw.exits.underlying_target} (target)` : " (stop)"}` : null,
  } : null;

  const [note, setNote] = useState("");
  async function act(what: "approve" | "cancel", override = false) {
    setBusy(true);
    setError(null);
    try {
      onDone(await api<PaperOrder>(`/paper/orders/${order.id}/${what}`, { method: "POST", body: what === "approve" ? { override, reason: note } : undefined }));
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }
  const raw_v = v as (typeof v & { hard?: string[]; overridable?: boolean }) | null;
  const canOverride = !!raw_v && raw_v.verdict === "BLOCK" && !!raw_v.overridable && order.state === "proposed";

  return (
    <div className="overflow-hidden rounded-md border border-line-strong bg-canvas" style={{ animation: "tb-in 160ms ease-out" }}>
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <Badge tone="warn">Paper</Badge>
        <Side side={p.side} />
        <span className="num text-[14px] font-semibold">{p.qty}{opt ? " lot ×" : " ×"} {opt?.contract ?? p.symbol}</span>
        {opt?.contract && <span className="text-xs text-muted">{p.symbol.toLowerCase()}</span>}
        <span className="ml-auto text-2xs text-subtle">intraday · out by 15:15</span>
      </div>
      <dl className="grid grid-cols-3 divide-x divide-line border-b border-line">
        {(opt
          ? [["Premium", opt.premium != null ? inr(opt.premium) : "–", ""], [opt.debit ? "Stop (premium −50%)" : "Stop", opt.stop != null ? inr(opt.stop) : "–", "text-down"], [opt.debit ? "Target (premium doubles)" : "Target", opt.target != null ? inr(opt.target) : "–", "text-up"]]
          : [["Limit", fmt(p.limit), ""], ["Stop", fmt(p.stop), "text-down"], ["Target", fmt(p.target), "text-up"]]).map(([k, val, ink]) => (
          <div key={k} className="px-3 py-2"><dt className="eyebrow">{k}</dt><dd className={`num mt-0.5 text-[14px] ${ink}`}>{val}</dd></div>
        ))}
      </dl>
      {opt?.levels && <p className="border-b border-line px-3 py-1.5 text-xs text-muted">Also exits if {opt.levels}, or at 15:15. The index levels usually come first; the premium stop and target are backstops.</p>}
      {opt?.unit != null && <p className="border-b border-line px-3 py-1.5 text-xs text-muted">Priced now at <span className="num text-fg">{fmt(opt.unit)}</span> a unit{raw.structure?.as_of ? ` (${raw.structure.source === "kite" ? "Kite" : "NSE"} ${raw.structure.as_of})` : ""}: this can differ from the card above, which was priced earlier.</p>}
      {v && (
        <div className={`space-y-1.5 px-3 py-2.5 text-[12.5px] ${pass ? "bg-up/[0.05]" : "bg-down/[0.06]"}`}>
          <p className={`flex items-center gap-1.5 font-semibold ${pass ? "text-up" : "text-down"}`}>
            <IconShield size={14} /> Risk check: {v.verdict}
          </p>
          {v.numbers.max_loss != null && (
            <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs sm:grid-cols-4">
              {([["Max loss", v.numbers.max_loss], [srcLabel("Charges at stop", src.charges_source), v.numbers.charges_at_stop],
                 ["Position", v.numbers.value], [srcLabel("Margin", src.margin_source), v.numbers.margin]] as const)
                .filter(([, n]) => n != null && !(opt && n === 0))
                .map(([k, n]) => <div key={k}><dt className="text-subtle">{k}</dt><dd className="num text-fg">{inr(n as number)}</dd></div>)}
              {v.numbers.reward_risk != null && <div><dt className="text-subtle">Reward : risk</dt><dd className="num text-fg">{v.numbers.reward_risk}</dd></div>}
            </dl>
          )}
          {v.reasons.length > 0 && (
            <ul className="space-y-0.5 text-down">{v.reasons.map((r) => <li key={r} className="flex gap-1.5"><IconX size={13} className="mt-0.5 shrink-0" />{r}</li>)}</ul>
          )}
          {v.warnings.map((w) => <p key={w} className="text-xs text-muted">Note: {w}</p>)}
        </div>
      )}
      {raw_v?.verdict === "BLOCK" && order.state === "proposed" && (
        canOverride ? (
          <div className="space-y-2 border-t border-line px-3 py-2.5">
            <p className="text-xs text-muted">
              Paper only: you can take it anyway. It is recorded as <span className="text-warn">rules not followed</span>, with these reasons,
              and still counts toward your daily and monthly losses. Live orders always need a PASS.
            </p>
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} placeholder="Why you are taking it anyway (optional, goes in the journal)"
              aria-label="Why you are overriding the risk check" className="field text-xs" />
            <HoldButton onDone={() => act("approve", true)} disabled={busy} className="w-full border-warn/60 text-warn">
              Hold to take it anyway (paper)
            </HoldButton>
          </div>
        ) : !!raw_v?.hard?.length && (
          <p className="border-t border-line px-3 py-2 text-xs text-muted">Cannot be taken even on paper: {raw_v.hard.join("; ")}.</p>
        )
      )}
      {error && <p className="px-3 pb-2 text-xs text-down">{error}</p>}
      <div className="px-3 pb-3 empty:hidden"><LiveStep order={order} onDone={onDone} /></div>
      <div className="flex gap-2 border-t border-line px-3 py-2.5">
        {pass && order.state === "proposed" && (
          <button disabled={busy} onClick={() => act("approve")} className="btn btn-primary flex-1">
            <IconCheck size={14} />{busy ? "Confirming…" : "Confirm paper order"}
          </button>
        )}
        <button disabled={busy} onClick={() => (order.state === "proposed" ? act("cancel") : onDone(null))} className={`btn ${pass ? "" : "flex-1"}`}>
          {pass ? "Cancel" : "Close"}
        </button>
      </div>
    </div>
  );
}

/** "Margin (Kite)" when Kite's margin API priced it, "Margin (est.)" for the app's estimate. */
function srcLabel(label: string, source: unknown): string {
  return source === "Kite" ? `${label} (Kite)` : source === "estimate" ? `${label} (est.)` : label;
}
