"use client";

import { useCallback, useEffect, useState } from "react";

import { useFeedback } from "@/components/ui/feedback";
import { Badge, Panel, SkeletonRows } from "@/components/ui/kit";
import { api, type TelegramCode, type TelegramStatus } from "@/lib/api";

/** Link your Telegram chat so agent ideas and paper tickets reach your phone, with buttons to confirm. */
export function TelegramPanel() {
  const { toast } = useFeedback();
  const [st, setSt] = useState<TelegramStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState<TelegramCode | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api<TelegramStatus>("/telegram").then((s) => { setSt(s); setError(null); if (s.linked) setCode(null); })
    .catch((e: Error) => setError(e.message)), []);
  useEffect(() => {
    load();
    const id = setInterval(load, code ? 4_000 : 30_000);          // while a code is out, notice the link quickly
    return () => clearInterval(id);
  }, [load, code]);

  async function run(fn: () => Promise<unknown>, ok?: string) {
    setBusy(true);
    try {
      await fn();
      if (ok) toast("success", ok);
      await load();
    } catch (e) {
      toast("error", "Telegram", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Approve from Telegram" actions={st && (st.linked ? <Badge tone="up">Linked</Badge> : <Badge>Not linked</Badge>)}>
      {error ? <p className="text-[12.5px] text-down">{error}</p> : !st ? <SkeletonRows rows={2} /> : !st.configured ? (
        <div className="space-y-2 text-[12.5px] text-muted">
          <p>Get each live idea and your paper tickets on your phone, and confirm with a tap.</p>
          <ol className="list-decimal space-y-0.5 pl-4">
            <li>In Telegram, message <span className="text-fg">@BotFather</span>, send /newbot and follow its steps.</li>
            <li>Put the token it gives you in <span className="num text-fg">backend/.env</span> as TELEGRAM_BOT_TOKEN, then restart the backend.</li>
            <li>Come back here and press Connect Telegram.</li>
          </ol>
        </div>
      ) : st.linked ? (
        <div className="space-y-2 text-[12.5px]">
          <p className="text-muted">
            Linked{st.username ? <> to <span className="text-fg">@{st.username}</span></> : null}. Live ideas, tickets, fills and exits arrive there. A ticket takes two taps
            to confirm, and the risk check runs again when you do. Paper overrides stay on this app.
          </p>
          {st.error && <p className="text-warn">Last problem: {st.error}</p>}
          <div className="flex gap-2">
            <button className="btn btn-ghost" disabled={busy} onClick={() => run(() => api("/telegram/test", { method: "POST" }), "Test message sent")}>Send a test</button>
            <button className="btn btn-ghost" disabled={busy} onClick={() => run(() => api("/telegram/unlink", { method: "POST" }), "Unlinked")}>Unlink</button>
          </div>
        </div>
      ) : (
        <div className="space-y-2 text-[12.5px] text-muted">
          <p>Get each live idea and your paper tickets on your phone, and confirm with a tap. Nothing trades without your taps.</p>
          {code ? (
            <div className="rounded border border-line px-2.5 py-2">
              {code.link ? (
                <a className="font-medium text-accent hover:underline" href={code.link} target="_blank" rel="noreferrer">Open @{code.bot_username} in Telegram and press Start</a>
              ) : <p>Send this to your bot:</p>}
              <p className="num mt-1 text-fg">/start {code.code}</p>
              <p className="mt-1 text-xs">The code works once, for 10 minutes. This panel updates when the chat is linked.</p>
            </div>
          ) : null}
          {st.error && <p className="text-warn">The bot has a problem: {st.error}</p>}
          <button className="btn btn-primary" disabled={busy}
            onClick={() => run(async () => setCode(await api<TelegramCode>("/telegram/link", { method: "POST" })))}>
            {code ? "New code" : "Connect Telegram"}
          </button>
        </div>
      )}
    </Panel>
  );
}
