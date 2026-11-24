"use client";

/** Toasts (success / failure after an action) and a confirm dialog for destructive or real-money actions. */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { IconAlert, IconCheck, IconInfo, IconX } from "./icons";

type Tone = "success" | "error" | "info";
type Toast = { id: number; tone: Tone; title: string; body?: string };
type ConfirmOpts = { title: string; body?: ReactNode; confirm: string; tone?: "danger" | "live" | "primary"; requireText?: string };

const Ctx = createContext<{ toast: (tone: Tone, title: string, body?: string) => void; confirm: (o: ConfirmOpts) => Promise<boolean> } | null>(null);

export function useFeedback() {
  const c = useContext(Ctx);
  if (!c) throw new Error("useFeedback outside <FeedbackProvider>");
  return c;
}

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dialog, setDialog] = useState<(ConfirmOpts & { resolve: (ok: boolean) => void }) | null>(null);
  const n = useRef(0);

  const toast = useCallback((tone: Tone, title: string, body?: string) => {
    const id = ++n.current;
    setToasts((t) => [...t.slice(-3), { id, tone, title, body }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "error" ? 8000 : 4500);
  }, []);
  const confirm = useCallback((o: ConfirmOpts) => new Promise<boolean>((resolve) => setDialog({ ...o, resolve })), []);

  return (
    <Ctx.Provider value={{ toast, confirm }}>
      {children}
      <div aria-live="polite" className="pointer-events-none fixed right-3 bottom-3 z-[60] flex w-[min(380px,calc(100vw-24px))] flex-col gap-2">
        {toasts.map((t) => {
          const [Icon, ink] = t.tone === "success" ? [IconCheck, "text-up"] : t.tone === "error" ? [IconAlert, "text-down"] : [IconInfo, "text-info"];
          return (
            <div key={t.id} role={t.tone === "error" ? "alert" : "status"} className="pointer-events-auto flex items-start gap-2.5 rounded-md border border-line-strong bg-overlay px-3 py-2.5 shadow-[var(--shadow-pop)]"
              style={{ animation: "tb-in 180ms ease-out" }}>
              <Icon size={15} className={`mt-px shrink-0 ${ink}`} />
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-medium text-fg">{t.title}</p>
                {t.body && <p className="mt-0.5 text-xs break-words text-muted">{t.body}</p>}
              </div>
              <button onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))} className="btn-ghost -mr-1 rounded p-0.5 text-subtle hover:text-fg" aria-label="Dismiss">
                <IconX size={14} />
              </button>
            </div>
          );
        })}
      </div>
      {dialog && <ConfirmDialog o={dialog} done={(ok) => { dialog.resolve(ok); setDialog(null); }} />}
    </Ctx.Provider>
  );
}

function ConfirmDialog({ o, done }: { o: ConfirmOpts; done: (ok: boolean) => void }) {
  const [typed, setTyped] = useState("");
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus();                    // the safe choice has focus: Enter does not confirm by accident
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && done(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [done]);
  const ready = !o.requireText || typed.trim().toUpperCase() === o.requireText.toUpperCase();
  const btn = o.tone === "live" ? "btn-live" : o.tone === "primary" ? "btn-primary" : "btn-danger";
  return (
    <div className="fixed inset-0 z-[70] grid place-items-center bg-black/60 p-4 backdrop-blur-[2px]" style={{ animation: "tb-fade 120ms ease-out" }}
      onMouseDown={(e) => e.target === e.currentTarget && done(false)}>
      <div role="alertdialog" aria-modal="true" aria-labelledby="tb-confirm-title" className="w-full max-w-md rounded-lg border border-line-strong bg-surface shadow-[var(--shadow-pop)]"
        style={{ animation: "tb-in 160ms ease-out" }}>
        <div className="flex items-start gap-3 p-4">
          <div className={`grid size-8 shrink-0 place-items-center rounded-md ${o.tone === "primary" ? "bg-accent/12 text-accent" : "bg-down/12 text-down"}`}><IconAlert size={16} /></div>
          <div className="min-w-0 space-y-1.5">
            <h2 id="tb-confirm-title" className="text-sm font-semibold text-fg">{o.title}</h2>
            {o.body && <div className="text-[12.5px] text-muted">{o.body}</div>}
            {o.requireText && (
              <label className="block pt-1">
                <span className="field-label">Type <span className="num font-semibold text-fg">{o.requireText}</span> to confirm</span>
                <input value={typed} onChange={(e) => setTyped(e.target.value)} className="field num" autoComplete="off" />
              </label>
            )}
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-line px-4 py-3">
          <button ref={cancelRef} onClick={() => done(false)} className="btn">Cancel</button>
          <button disabled={!ready} onClick={() => done(true)} className={`btn ${btn}`}>{o.confirm}</button>
        </div>
      </div>
    </div>
  );
}
