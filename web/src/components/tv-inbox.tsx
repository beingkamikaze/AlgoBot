"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { OrderTicket } from "@/components/order-ticket";
import { IconBell } from "@/components/ui/icons";
import { Badge, Empty, Notice, Panel, TableWrap, cx } from "@/components/ui/kit";
import { api, type Inbox } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const OUTCOME: Record<string, string> = {
  accepted: "badge-up", rejected: "badge-down", duplicate: "badge-neutral", rate_limited: "badge-warn",
};

/** TradingView alerts waiting for you. Each is a risk-checked ticket; nothing is placed until you confirm. */
export function TvInbox() {
  const [data, setData] = useState<Inbox | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      const session = (await supabase()?.auth.getSession())?.data.session;
      if (stop) return;
      setSignedIn(!!session);
      if (!session) return;
      api<Inbox>("/webhooks/inbox").then((d) => { if (!stop) { setData(d); setError(null); } }).catch((e: Error) => !stop && setError(e.message));
    };
    load();
    const id = setInterval(load, 5000);
    return () => { stop = true; clearInterval(id); };
  }, []);

  if (!signedIn) return null;
  const waiting = data?.tickets.filter((t) => t.state === "proposed") ?? [];
  const recent = data?.events.slice(0, 8) ?? [];
  return (
    <Panel
      title={<div className="flex items-center gap-2"><IconBell size={15} className="text-subtle" /><h2 className="panel-title">TradingView alerts</h2>{waiting.length > 0 && <Badge tone="warn">{waiting.length} waiting</Badge>}</div>}
      actions={<><span className="hidden text-2xs text-subtle sm:inline">tickets expire after 15 min</span><Link href="/tradingview" className="btn btn-ghost btn-sm">Set up</Link></>}
    >
      {error && <div className="mb-2"><Notice tone="error">{error}</Notice></div>}
      {waiting.length === 0 && <Empty title="No alerts waiting">Alerts from your TradingView sources appear here as risk-checked tickets.</Empty>}
      <div className="space-y-3">
        {waiting.map((t) => (
          <div key={t.id} className="space-y-2">
            {t.explanation && (
              <div className="space-y-1 text-[12.5px]">
                <p className="text-fg">{t.explanation.summary}</p>
                <ul className="space-y-0.5 text-xs text-warn">{t.explanation.risks.map((r) => <li key={r}>· {r}</li>)}</ul>
              </div>
            )}
            <OrderTicket order={t} onDone={() => api<Inbox>("/webhooks/inbox").then(setData).catch(() => {})} />
          </div>
        ))}
      </div>
      {recent.length > 0 && (
        <details className="group mt-3 border-t border-line pt-2">
          <summary className="cursor-pointer text-xs text-muted hover:text-fg">Last {recent.length} alerts received</summary>
          <TableWrap>
            <table className="dtable dtable-compact mt-2">
              <tbody>
                {recent.map((e) => (
                  <tr key={e.id}>
                    <td className="num text-muted">{new Date(e.received_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })}</td>
                    <td><span className={cx("badge", OUTCOME[e.outcome] ?? "badge-neutral")}>{e.outcome}</span></td>
                    <td className="num">{String(e.payload.side ?? "")} {String(e.payload.symbol ?? "")}</td>
                    <td className="max-w-[320px] truncate text-muted" title={e.detail ?? undefined}>{e.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        </details>
      )}
    </Panel>
  );
}
