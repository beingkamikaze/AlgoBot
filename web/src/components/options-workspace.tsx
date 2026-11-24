"use client";

import { useEffect, useState } from "react";

import { PayoffChart } from "@/components/payoff-chart";
import { Badge, HoldButton, Notice, PageHeader, Panel, Side, SkeletonRows, Stat, StatStrip, TableWrap, Term, TERMS } from "@/components/ui/kit";
import { api, inr, type ChainData, type PaperOrder, type StructureData } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const TEMPLATES: Record<string, string> = {
  long_call: "Buy call",
  long_put: "Buy put",
  bull_call_spread: "Bull call spread",
  bear_put_spread: "Bear put spread",
  bull_put_spread: "Bull put spread (credit)",
  bear_call_spread: "Bear call spread (credit)",
  iron_condor: "Iron condor (credit)",
  iron_fly: "Iron fly (credit)",
  short_straddle: "Short straddle (naked)",
  short_strangle: "Short strangle (naked)",
};
const MOBILE_HIDE = new Set(["Chg OI", "IV", "Δ"]);
const CREDIT = new Set(["bull_put_spread", "bear_call_spread", "iron_condor", "iron_fly", "short_straddle", "short_strangle"]);
const fmtK = (n: number | null | undefined) => (n == null ? "–" : n >= 1e5 ? `${(n / 1e5).toFixed(1)}L` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}k` : String(n));

type RecorderStatus = { enabled: boolean; running: boolean; today: number; last_snapshot: string | null; days_recorded: number; market_open: boolean; last_error: string | null };

// One quiet line: is the NIFTY chain recorder (research data, saved on this computer) working today?
function RecorderLine() {
  const [r, setR] = useState<RecorderStatus | null>(null);
  useEffect(() => {
    const load = () => api<RecorderStatus>("/options/recorder").then(setR).catch(() => setR(null));
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, []);
  if (!r) return null;
  const last = r.last_snapshot ? new Date(r.last_snapshot).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) : null;
  return (
    <p className="text-xs text-subtle">
      Chain recorder: {!r.enabled ? "off (CHAIN_RECORDER_ENABLED=false)" : `${r.today} NIFTY snapshot${r.today === 1 ? "" : "s"} today${last ? `, last at ${last} IST` : ""}${r.market_open ? "" : " · market closed"}`}
      {r.days_recorded ? ` · ${r.days_recorded} day${r.days_recorded === 1 ? "" : "s"} saved` : ""}
      {r.enabled && r.last_error ? <span className="text-warn"> · last try failed: {r.last_error}</span> : null}
    </p>
  );
}

export default function OptionsWorkspace() {
  const q = new URLSearchParams(window.location.search);
  const [underlying, setUnderlying] = useState(q.get("underlying") ?? "NIFTY");
  const [stockInput, setStockInput] = useState("");
  const [expiry, setExpiry] = useState<string>("");
  const [chain, setChain] = useState<ChainData | null>(null);
  const [template, setTemplate] = useState(q.get("template") ?? "iron_condor");
  const [params, setParams] = useState({ lots: 1, offset: 0, width: 4, short_delta: 0.2 });
  const [exits, setExits] = useState({ stop_pct: 0.5, target_pct: 1.0, stop_mult: 1.0, credit_target_pct: 0.5 });
  const fromSignal = q.get("ustop") ? { underlying_stop: Number(q.get("ustop")), underlying_target: Number(q.get("utarget")), side_hint: q.get("side") ?? undefined } : null;
  const [s, setS] = useState<StructureData | null>(null);
  const [ticket, setTicket] = useState<PaperOrder | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    const load = () => {
      const p = new URLSearchParams({ underlying, width: "12" });
      if (expiry) p.set("expiry", expiry);
      api<ChainData>(`/options/chain?${p}`).then((c) => { setChain(c); setError(null); }).catch((e: Error) => setError(e.message));
    };
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [underlying, expiry]);

  async function build() {
    setBusy("build"); setError(null); setTicket(null); setMsg(null);
    try {
      setS(await api<StructureData>("/options/build", { method: "POST", body: { underlying, expiry: chain?.expiry, template, params, exits: { ...exits, ...(fromSignal ?? {}) } } }));
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  async function paper() {
    setBusy("paper"); setError(null); setMsg(null);
    try {
      if (!(await supabase()?.auth.getSession())?.data.session) { setMsg("Sign in on the Home page first."); return; }
      setTicket(await api<PaperOrder>("/paper/tickets/option", { method: "POST", body: { underlying, expiry: chain?.expiry, template, params, exits: { ...exits, ...(fromSignal ?? {}) } } }));
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  async function act(what: "approve" | "cancel", override = false) {
    if (!ticket) return;
    setBusy(what);
    try {
      const o = await api<PaperOrder>(`/paper/orders/${ticket.id}/${what}`, { method: "POST", body: what === "approve" ? { override } : undefined });
      setTicket(null);
      setMsg(o.state === "working" ? (override ? "Taken anyway (risk check overridden, recorded as rules not followed). " : "") + "Paper order working: it fills at NSE's bid/ask on the next check (market hours). See Paper trading."
        : o.state === "rejected" && o.risk_verdict?.verdict === "BLOCK" ? `Blocked: ${o.risk_verdict.reasons.join("; ")}` : "Cancelled.");
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  const maxOi = Math.max(1, ...(chain?.rows.flatMap((r) => [r.CE?.oi ?? 0, r.PE?.oi ?? 0]) ?? [1]));
  const credit = CREDIT.has(template);
  const v = ticket?.risk_verdict;
  const spot = chain?.spot ?? 0;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={["NIFTY", "BANKNIFTY"].includes(underlying) ? "Options" : `Options · ${underlying}`}
        subtitle="Kite's real-time option chain when you are logged in to Kite today, else NSE's (delayed). Index options are cash-settled; stock options are monthly and physically settled at expiry (this app trades them intraday only). Paper only."
        meta={chain?.source === "modeled" ? <Badge tone="warn">Modeled prices</Badge> : chain?.source === "kite" ? <Badge tone="live" title="Kite quotes: real time">Kite live · {chain.as_of}</Badge>
          : chain ? <Badge tone="warn" title="NSE's public chain: delayed. Log in to Kite on the Live page for real-time quotes">NSE, delayed · {chain.as_of}</Badge> : null}
        actions={<>
          <div className="seg" role="group" aria-label="Underlying">
            {[["NIFTY", "Nifty"], ["BANKNIFTY", "Bank Nifty"]].map(([k, label]) => (
              <button key={k} aria-pressed={underlying === k} onClick={() => { setUnderlying(k); setExpiry(""); setS(null); setStockInput(""); }}>{label}</button>
            ))}
          </div>
          <form onSubmit={(e) => { e.preventDefault(); const v = stockInput.trim().toUpperCase(); if (v) { setUnderlying(v); setExpiry(""); setS(null); setChain(null); } }}>
            <input value={stockInput} onChange={(e) => setStockInput(e.target.value.toUpperCase())} placeholder={!["NIFTY", "BANKNIFTY"].includes(underlying) ? underlying : "F&O stock, e.g. TITAN"}
              aria-label="F&O stock" className={`field num w-44 ${!["NIFTY", "BANKNIFTY"].includes(underlying) ? "border-accent" : ""}`} />
          </form>
          <label className="flex items-center gap-2 text-xs text-muted">Expiry
            <select value={expiry || chain?.expiry || ""} onChange={(e) => { setExpiry(e.target.value); setS(null); }} className="field num w-36">
              {(chain?.expiries ?? []).map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
          </label>
        </>}
      />
      <RecorderLine />

      {fromSignal && <Notice tone="info" title="Built from a signal">The position also closes if {underlying} crosses <span className="num text-fg">{fromSignal.underlying_stop}</span> (stop) or <span className="num text-fg">{fromSignal.underlying_target}</span> (target).</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      {chain?.source === "modeled" && <Notice tone="warn" title="Modeled prices">NSE did not answer, so prices are modeled from India VIX. Do not trade on them.</Notice>}

      <StatStrip cols={6}>
        <Stat label="Spot" loading={!chain} value={chain?.spot.toLocaleString("en-IN")} />
        <Stat label="Forward" loading={!chain} value={chain?.forward.toLocaleString("en-IN")} />
        <Stat label={<Term tip={TERMS.pcr}>PCR (OI)</Term>} loading={!chain} value={chain?.pcr ?? "–"} />
        <Stat label={<Term tip={TERMS.maxPain}>Max pain</Term>} loading={!chain} value={chain?.max_pain?.toLocaleString("en-IN") ?? "–"} />
        <Stat label={<Term tip={TERMS.iv}>ATM IV</Term>} loading={!chain} value={chain?.atm_iv ? `${chain.atm_iv}%` : "–"} />
        <Stat label="Lot size" loading={!chain} value={chain?.lot_size} />
      </StatStrip>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Panel title="Option chain" actions={<span className="text-2xs text-subtle">shaded = in the money · bars = open interest</span>} flush>
          {!chain ? <SkeletonRows rows={12} cols={7} /> : (
            <TableWrap max="72vh">
              <table className="dtable dtable-compact text-xs max-md:[&_td]:px-1.5 max-md:[&_th]:px-1.5">
                <thead>
                  <tr className="max-md:hidden">
                    <th colSpan={5} className="text-center text-info">Calls</th>
                    <th className="text-center">Strike</th>
                    <th colSpan={5} className="text-center text-warn">Puts</th>
                  </tr>
                  <tr className="md:[&>th]:top-7">
                    {["OI", "Chg OI", "IV", "Δ", "Bid / Ask"].map((h) => <th key={`c${h}`} className={`r ${MOBILE_HIDE.has(h) ? "max-md:hidden" : ""} ${h === "OI" ? "max-md:text-info" : ""}`}>{h === "Δ" ? <Term tip={TERMS.delta}>Δ</Term> : h === "OI" ? <><span className="md:hidden">Call </span>OI</> : h}</th>)}
                    <th className="text-center"><span className="md:hidden">Strike</span></th>
                    {["Bid / Ask", "Δ", "IV", "Chg OI", "OI"].map((h) => <th key={`p${h}`} className={`r ${MOBILE_HIDE.has(h) ? "max-md:hidden" : ""} ${h === "OI" ? "max-md:text-warn" : ""}`}>{h === "Δ" ? <Term tip={TERMS.delta}>Δ</Term> : h === "OI" ? <><span className="md:hidden">Put </span>OI</> : h}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {chain.rows.map((r) => {
                    const atm = r.strike === chain.atm;
                    const ce = r.strike < spot ? "bg-info/[0.05]" : "";
                    const pe = r.strike > spot ? "bg-warn/[0.05]" : "";
                    return (
                      <tr key={r.strike} className={atm ? "bg-accent/[0.08]" : ""}>
                        <td className={`r relative ${ce}`}><span className="absolute inset-y-1.5 right-0 rounded-l-sm bg-info/20" style={{ width: `${((r.CE?.oi ?? 0) / maxOi) * 100}%` }} /><span className="relative">{fmtK(r.CE?.oi)}</span></td>
                        <td className={`r max-md:hidden ${ce} ${(r.CE?.chg_oi ?? 0) < 0 ? "text-muted" : ""}`}>{fmtK(r.CE?.chg_oi)}</td>
                        <td className={`r max-md:hidden ${ce}`}>{r.CE?.iv ?? "–"}</td>
                        <td className={`r max-md:hidden ${ce}`}>{r.CE?.delta?.toFixed(2) ?? "–"}</td>
                        <td className={`r ${ce}`}>{r.CE?.bid ?? "–"} <span className="text-subtle">/</span> {r.CE?.ask ?? "–"}</td>
                        <td className={`num border-x border-line text-center font-semibold ${atm ? "text-accent" : "text-fg"}`}>{r.strike}</td>
                        <td className={`r ${pe}`}>{r.PE?.bid ?? "–"} <span className="text-subtle">/</span> {r.PE?.ask ?? "–"}</td>
                        <td className={`r max-md:hidden ${pe}`}>{r.PE?.delta?.toFixed(2) ?? "–"}</td>
                        <td className={`r max-md:hidden ${pe}`}>{r.PE?.iv ?? "–"}</td>
                        <td className={`r max-md:hidden ${pe} ${(r.PE?.chg_oi ?? 0) < 0 ? "text-muted" : ""}`}>{fmtK(r.PE?.chg_oi)}</td>
                        <td className={`r relative ${pe}`}><span className="absolute inset-y-1.5 left-0 rounded-r-sm bg-warn/20" style={{ width: `${((r.PE?.oi ?? 0) / maxOi) * 100}%` }} /><span className="relative">{fmtK(r.PE?.oi)}</span></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
          )}
          <p className="border-t border-line px-3 py-2 text-2xs text-subtle">IV is NSE&apos;s; Δ is this app&apos;s, priced off the chain&apos;s own forward.</p>
        </Panel>

        <Panel title="Build a position" className="self-start xl:sticky xl:top-16">
          <div className="space-y-3">
            <label className="block"><span className="field-label">Structure</span>
              <select value={template} onChange={(e) => { setTemplate(e.target.value); setS(null); }} className="field">
                {Object.entries(TEMPLATES).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
            </label>
            <div className="grid grid-cols-2 gap-2">
              {(["lots", "offset", "width", "short_delta"] as const).map((k) => (
                <label key={k}>
                  <span className="field-label">{{ lots: "Lots", offset: "Strikes from ATM", width: "Width (strikes)", short_delta: "Short leg delta" }[k]}</span>
                  <input type="number" step={k === "short_delta" ? 0.05 : 1} value={params[k]} onChange={(e) => setParams({ ...params, [k]: Number(e.target.value) })} className="field field-num" />
                </label>
              ))}
              {(credit ? (["stop_mult", "credit_target_pct"] as const) : (["stop_pct", "target_pct"] as const)).map((k) => (
                <label key={k}>
                  <span className="field-label">{{ stop_mult: "Stop: loss = credit ×", credit_target_pct: "Take profit: credit share", stop_pct: "Stop: premium lost", target_pct: "Target: premium gained" }[k]}</span>
                  <input type="number" step={0.1} value={exits[k]} onChange={(e) => setExits({ ...exits, [k]: Number(e.target.value) })} className="field field-num" />
                </label>
              ))}
            </div>
            <button onClick={build} disabled={busy !== null || !chain} className="btn w-full">{busy === "build" ? "Pricing…" : "Preview position"}</button>
          </div>

          {s && (
            <div className="mt-4 space-y-3 border-t border-line pt-3" style={{ animation: "tb-in 160ms ease-out" }}>
              <p className="text-xs text-muted">{s.description}</p>
              <table className="dtable dtable-compact text-xs">
                <thead><tr><th>Leg</th><th>Contract</th><th className="r">Price</th><th className="r">Spread</th></tr></thead>
                <tbody>
                  {s.legs.map((l) => (
                    <tr key={l.tradingsymbol}>
                      <td><Side side={l.side} /></td>
                      <td className="num">{l.lots} × {l.tradingsymbol}</td>
                      <td className="r">{l.price}</td>
                      <td className={`r ${(l.spread_pct ?? 0) > 10 ? "text-down" : "text-muted"}`}>{l.spread_pct ?? "–"}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line text-xs">
                {([
                  [s.net_premium >= 0 ? "Credit" : "Debit", <><span className="num">{inr(Math.abs(s.net_premium) * s.qty)}</span> <span className="text-subtle num">({Math.abs(s.net_premium)} × {s.qty})</span></>],
                  ["Max profit", <span key="mp" className="num text-up">{s.max_profit == null ? "unlimited" : inr(s.max_profit)}</span>],
                  ["Max loss", <span key="ml" className={`num ${s.naked ? "font-semibold text-down" : "text-down"}`}>{s.max_loss == null ? "UNLIMITED" : inr(s.max_loss)}</span>],
                  ["Breakevens", <span key="be" className="num">{s.breakevens.join(", ") || "–"}</span>],
                  [s.margin_source === "Kite" ? "Margin (Kite)" : "Margin (est.)", <span key="mg" className="num">{inr(s.margin)}</span>],
                  ["Charges, round trip", <span key="ch" className="num">{inr(s.charges_round_trip)}</span>],
                  ["Planned exits", <span key="pe" className="num">stop {inr(s.exits.stop_pnl)} · target {inr(s.exits.target_pnl)}</span>],
                  ["Greeks", <span key="gk" className="num">Δ {s.greeks.delta} · Θ {inr(s.greeks.theta)}/d · V {s.greeks.vega}</span>],
                ] as [string, React.ReactNode][]).map(([k, val]) => (
                  <div key={k} className="bg-surface px-2.5 py-2"><dt className="eyebrow">{k}</dt><dd className="mt-0.5">{val}</dd></div>
                ))}
              </dl>
              {s.naked && <Notice tone="error" title="Naked position">The loss is not capped. The risk check blocks it unless you allowed naked shorts in your limits.</Notice>}
              <div className="rounded-md border border-line bg-canvas p-2">
                <PayoffChart points={s.payoff} spot={s.spot} breakevens={s.breakevens} />
                <div className="flex flex-wrap gap-x-4 gap-y-1 px-1 pt-1 text-2xs text-muted">
                  <span className="flex items-center gap-1.5"><span className="h-0.5 w-4 bg-fg" />At expiry</span>
                  <span className="flex items-center gap-1.5"><span className="h-0 w-4 border-t border-dashed border-warn" />Today, current IV</span>
                  <span className="flex items-center gap-1.5"><span className="h-0 w-4 border-t border-dashed border-info" />Spot</span>
                </div>
              </div>
              <p className="text-2xs text-subtle">Prices: {s.source === "kite" ? `Kite ${s.as_of}` : s.source === "nse" ? `NSE ${s.as_of}` : "MODELED"}; buys at the ask, sells at the bid.</p>
              <button onClick={paper} disabled={busy !== null || ticket !== null} className="btn btn-primary w-full">
                {busy === "paper" ? "Checking risk…" : "Paper trade this"}
              </button>
            </div>
          )}
          {ticket && v && (
            <div className="mt-3 overflow-hidden rounded-md border border-line-strong bg-canvas text-xs" style={{ animation: "tb-in 160ms ease-out" }}>
              <div className="flex items-center gap-2 border-b border-line px-3 py-2"><Badge tone="warn">Paper</Badge><span className="num font-semibold">{ticket.params.symbol}</span><span className="text-muted">· {ticket.params.qty} lot(s)</span></div>
              <div className={`space-y-1 px-3 py-2.5 ${v.verdict === "PASS" ? "bg-up/[0.05]" : "bg-down/[0.06]"}`}>
                <p className={`font-semibold ${v.verdict === "PASS" ? "text-up" : "text-down"}`}>Risk check: {v.verdict}
                  <span className="num font-normal text-muted"> · max loss {inr(v.numbers.max_loss ?? 0)} incl. charges · margin {inr(v.numbers.margin ?? 0)}</span></p>
                {v.reasons.map((r) => <p key={r} className="text-down">{r}</p>)}
                {v.warnings.map((w) => <p key={w} className="text-muted">Note: {w}</p>)}
              </div>
              {v.verdict === "BLOCK" && (v as { overridable?: boolean }).overridable && (
                <div className="space-y-2 border-t border-line px-3 py-2.5">
                  <p className="text-muted">Paper only: take it anyway. Recorded as <span className="text-warn">rules not followed</span>; it still counts toward your daily and monthly losses.</p>
                  <HoldButton onDone={() => act("approve", true)} disabled={busy !== null} className="w-full border-warn/60 text-warn">Hold to take it anyway (paper)</HoldButton>
                </div>
              )}
              <div className="flex gap-2 border-t border-line px-3 py-2.5">
                {v.verdict === "PASS" && <button disabled={busy !== null} onClick={() => act("approve")} className="btn btn-primary flex-1">Confirm paper order</button>}
                <button disabled={busy !== null} onClick={() => act("cancel")} className={`btn ${v.verdict === "PASS" ? "" : "flex-1"}`}>{v.verdict === "PASS" ? "Cancel" : "Close"}</button>
              </div>
            </div>
          )}
          {msg && <div className="mt-3"><Notice tone={msg.startsWith("Blocked") ? "error" : msg.startsWith("Paper order working") ? "success" : "info"}>{msg}</Notice></div>}
        </Panel>
      </div>
    </div>
  );
}
