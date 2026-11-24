"use client";

import type { Session } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { TvInbox } from "@/components/tv-inbox";
import { useFeedback } from "@/components/ui/feedback";
import { IconAlert, IconCopy, IconPlus } from "@/components/ui/icons";
import { Empty, Notice, Page, PageHeader, PageLoading, Panel, SignInRequired, TableWrap } from "@/components/ui/kit";
import { api, type NewWebhookSource, type WebhookSource } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const PINE = `//@version=6
indicator("Breakout alert", overlay=true)
level = ta.highest(high, 20)[1]          // your rule here: the last 20 bars' high
longStop = ta.lowest(low, 5)
if ta.crossover(close, level)
    alert('{"secret":"PASTE_SECRET","symbol":"' + syminfo.ticker + '","side":"buy","price":' + str.tostring(close) +
          ',"stop":' + str.tostring(longStop) + ',"strategy":"Breakout 20","note":"close crossed the 20-bar high"}',
          alert.freq_once_per_bar_close)`;


function Step({ n, title }: { n: number; title: string }) {
  return <div className="flex items-center gap-2"><span className="num grid size-5 place-items-center rounded-full bg-accent/15 text-2xs font-semibold text-accent">{n}</span><h2 className="panel-title">{title}</h2></div>;
}

export default function TradingViewPage() {
  const sb = supabase();
  const { toast } = useFeedback();
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [sources, setSources] = useState<WebhookSource[]>([]);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<NewWebhookSource | null>(null);
  const [tunnel, setTunnel] = useState("");
  const [test, setTest] = useState({ source: "", symbol: "RELIANCE", side: "buy", price: "", stop: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { sb?.auth.getSession().then(({ data }) => setSession(data.session)); }, [sb]);
  useEffect(() => {
    if (!session) return;
    api<WebhookSource[]>("/webhooks/sources").then((s) => { setSources(s); setTest((t) => ({ ...t, source: t.source || s[0]?.id || "" })); })
      .catch((e: Error) => setError(e.message));
  }, [session, created]);

  async function create() {
    setError(null);
    try { setCreated(await api<NewWebhookSource>("/webhooks/sources", { method: "POST", body: { name } })); setName(""); }
    catch (e) { setError((e as Error).message); }
  }
  async function toggle(s: WebhookSource) {
    try {
      await api(`/webhooks/sources/${s.id}`, { method: "PUT", body: { enabled: !s.enabled } });
      setSources(await api<WebhookSource[]>("/webhooks/sources"));
    } catch (e) { setError((e as Error).message); }
  }
  async function sendTest() {
    setError(null); setMsg(null);
    try {
      await api(`/webhooks/sources/${test.source}/test`, { method: "POST", body: {
        symbol: test.symbol, side: test.side, stop: Number(test.stop), price: test.price ? Number(test.price) : null } });
      setMsg("Sent. The ticket appears in the inbox below in a few seconds (prices and news are checked first).");
    } catch (e) { setError((e as Error).message); }
  }

  if (session === undefined) return <PageLoading />;
  if (!session) return <SignInRequired what="TradingView alerts" />;

  const url = (path: string) => `${tunnel.replace(/\/$/, "") || "https://YOUR-TUNNEL.trycloudflare.com"}${"/api"}${path.replace(/^\/api/, "")}`;
  const copy = (text: string, what: string) => navigator.clipboard?.writeText(text).then(() => toast("success", `${what} copied`)).catch(() => toast("error", "Copy failed", "Select the text and copy it by hand."));
  return (
    <Page wide>
      <PageHeader title="TradingView alerts"
        subtitle="A TradingView alert becomes a risk-checked paper ticket with a “why” card. It never trades by itself: you confirm, or it expires in 15 minutes." />
      {error && <Notice tone="error">{error}</Notice>}

      <div className="grid gap-4 xl:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-7">
          <Panel title={<Step n={1} title="Make your Mac reachable" />}>
            <p className="mb-2 text-xs text-muted">TradingView posts only to https on port 443 and waits 3 seconds. Run a free Cloudflare tunnel in a terminal and paste the address it prints:</p>
            <pre className="num mb-2 overflow-x-auto rounded-md border border-line bg-canvas p-2.5 text-xs text-fg">brew install cloudflared{"\n"}cloudflared tunnel --url http://localhost:8100</pre>
            <label className="block"><span className="field-label">Tunnel address</span>
              <input value={tunnel} onChange={(e) => setTunnel(e.target.value)} placeholder="https://something.trycloudflare.com" className="field num" /></label>
            <p className="mt-2 text-2xs text-subtle">The quick tunnel address changes each time it starts: update the webhook URL in TradingView when it does. Only TradingView&apos;s four addresses are accepted.</p>
          </Panel>

          <Panel title={<Step n={2} title="Create a source (one per TradingView strategy)" />}>
            <div className="flex gap-2">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. ORB 15m" className="field max-w-xs" aria-label="Source name" />
              <button disabled={name.trim().length < 2} onClick={create} className="btn btn-primary"><IconPlus size={14} />Create</button>
            </div>
            {created && (
              <div className="mt-3 space-y-2.5 rounded-md border border-warn/40 bg-warn/[0.06] p-3" style={{ animation: "tb-in 160ms ease-out" }}>
                <p className="flex items-center gap-1.5 text-[12.5px] font-semibold text-warn"><IconAlert size={14} />Copy these now: the secret is shown only once (it is stored as a hash).</p>
                <div>
                  <p className="field-label">Webhook URL</p>
                  <div className="flex gap-2"><code className="num field flex h-auto min-h-8 items-center py-1.5 text-xs break-all">{url(created.path)}</code>
                    <button onClick={() => copy(url(created.path), "Webhook URL")} className="btn btn-sm shrink-0" aria-label="Copy webhook URL"><IconCopy size={13} /></button></div>
                </div>
                <div>
                  <div className="flex items-center justify-between"><p className="field-label">Alert &quot;Message&quot; box <span className="text-subtle">· set stop and target to your values</span></p>
                    <button onClick={() => copy(created.message_template, "Message")} className="btn btn-ghost btn-sm"><IconCopy size={13} />Copy</button></div>
                  <pre className="num overflow-x-auto rounded-md border border-line bg-canvas p-2.5 text-xs text-fg">{created.message_template}</pre>
                </div>
                <button onClick={() => setCreated(null)} className="btn btn-sm">I have copied it</button>
              </div>
            )}
          </Panel>

          <Panel title="Sources" flush>
            {sources.length === 0 ? <Empty title="No sources yet">Create one above for each TradingView strategy that should send alerts.</Empty> : (
              <TableWrap>
                <table className="dtable">
                  <thead><tr><th>Name</th><th>Webhook URL</th><th className="r">Accepted</th><th className="r">Rejected</th><th>Last alert</th><th></th></tr></thead>
                  <tbody>
                    {sources.map((s) => (
                      <tr key={s.id}>
                        <td className="font-medium"><span className="flex items-center gap-2"><span className={`size-1.5 rounded-full ${s.enabled ? "bg-up" : "bg-subtle"}`} />{s.name}</span></td>
                        <td className="max-w-64"><button onClick={() => copy(url(`/api/webhooks/tradingview/${s.id}`), "Webhook URL")} title="Copy" className="num flex max-w-full cursor-pointer items-center gap-1.5 text-xs text-muted hover:text-fg"><span className="truncate">{url(`/api/webhooks/tradingview/${s.id}`)}</span><IconCopy size={12} className="shrink-0" /></button></td>
                        <td className="r text-up">{s.alerts_received}</td><td className="r text-down">{s.alerts_rejected}</td>
                        <td className="num text-xs text-muted">{s.last_seen_at ? new Date(s.last_seen_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" }) : "never"}</td>
                        <td className="text-right"><button onClick={() => toggle(s)} className={`btn btn-sm ${s.enabled ? "btn-danger" : ""}`}>{s.enabled ? "Disable" : "Enable"}</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </Panel>

          <Panel title={<Step n={3} title="In TradingView" />}>
            <p className="mb-2 text-xs text-muted">Create an alert, tick &quot;Webhook URL&quot; in Notifications and paste the URL; paste the message into &quot;Message&quot;. A stop is required: an alert without one is rejected. From Pine, build the message with your own stop:</p>
            <div className="relative">
              <pre className="num overflow-x-auto rounded-md border border-line bg-canvas p-2.5 text-xs leading-relaxed text-fg">{PINE}</pre>
              <button onClick={() => copy(PINE, "Pine example")} className="btn btn-sm absolute top-2 right-2"><IconCopy size={13} />Copy</button>
            </div>
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4 xl:col-span-5">
          <Panel title="Send a test alert" actions={<span className="text-2xs text-subtle">without TradingView</span>}>
            {sources.length === 0 ? <p className="text-xs text-muted">Create a source first.</p> : (
              <div className="grid grid-cols-2 gap-2">
                <label className="col-span-2"><span className="field-label">Source</span>
                  <select value={test.source} onChange={(e) => setTest({ ...test, source: e.target.value })} className="field">
                    {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select></label>
                <label><span className="field-label">Symbol</span>
                  <input value={test.symbol} onChange={(e) => setTest({ ...test, symbol: e.target.value })} placeholder="RELIANCE or NIFTY1!" className="field num" /></label>
                <label><span className="field-label">Side</span>
                  <select value={test.side} onChange={(e) => setTest({ ...test, side: e.target.value })} className="field"><option value="buy">Buy</option><option value="sell">Sell</option></select></label>
                <label><span className="field-label">Price <span className="text-subtle">(optional)</span></span>
                  <input value={test.price} onChange={(e) => setTest({ ...test, price: e.target.value })} inputMode="decimal" className="field field-num" /></label>
                <label><span className="field-label">Stop</span>
                  <input value={test.stop} onChange={(e) => setTest({ ...test, stop: e.target.value })} inputMode="decimal" className="field field-num" /></label>
                <button disabled={!test.source || !Number(test.stop)} onClick={sendTest} className="btn col-span-2">Send test alert</button>
                {msg && <div className="col-span-2"><Notice tone="success">{msg}</Notice></div>}
              </div>
            )}
          </Panel>
          <TvInbox />
        </div>
      </div>
    </Page>
  );
}
