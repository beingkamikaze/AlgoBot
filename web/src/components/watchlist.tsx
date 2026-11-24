"use client";

import { useEffect, useState } from "react";

import { IconX } from "@/components/ui/icons";
import { api, type Quote } from "@/lib/api";

export function Watchlist({
  symbols,
  active,
  onPick,
  onRemove,
}: {
  symbols: string[];
  active: string;
  onPick: (s: string) => void;
  onRemove: (s: string) => void;
}) {
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!symbols.length) return;
    const load = () =>
      api<Quote[]>(`/market/quotes?symbols=${encodeURIComponent(symbols.join(","))}`)
        .then((qs) => {
          setQuotes(Object.fromEntries(qs.map((q) => [q.symbol, q])));
          setError(null);
        })
        .catch((e: Error) => setError(e.message));
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [symbols]);

  return (
    <aside className="panel flex flex-col" aria-label="Watchlist">
      <div className="panel-head">
        <h2 className="panel-title">Watchlist</h2>
        <span className="ml-auto text-2xs text-subtle">Yahoo · 30 s</span>
      </div>
      <div className="grid grid-cols-[1fr_auto_auto] px-3 pt-2 pb-1 text-2xs font-medium tracking-wide text-subtle uppercase">
        <span>Symbol</span><span className="text-right">Last</span><span className="w-16 text-right">Chg</span>
      </div>
      <ul className="pb-1">
        {symbols.map((s) => {
          const q = quotes[s];
          const up = (q?.change_pct ?? 0) >= 0;
          const on = s === active;
          return (
            <li key={s} className="group relative">
              <button onClick={() => onPick(s)} aria-current={on ? "true" : undefined}
                className={`grid w-full cursor-pointer grid-cols-[1fr_auto_auto] items-center gap-2 py-1.5 pr-8 pl-3 text-left transition-colors ${on ? "bg-raised" : "hover:bg-hover"}`}>
                {on && <span className="absolute top-1 bottom-1 left-0 w-0.5 rounded-full bg-accent" />}
                <span className="truncate text-[13px] font-medium">{s}</span>
                <span className="num text-right text-[12.5px]">{q ? q.last.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : <span className="skeleton inline-block h-3 w-12" />}</span>
                <span className={`num w-16 text-right text-xs ${q?.change_pct == null ? "text-subtle" : up ? "text-up" : "text-down"}`}>
                  {q?.change_pct != null ? `${up ? "+" : "−"}${Math.abs(q.change_pct).toFixed(2)}%` : "–"}
                </span>
              </button>
              <button
                onClick={() => onRemove(s)}
                aria-label={`Remove ${s} from the watchlist`}
                className="absolute top-1/2 right-1.5 grid size-6 -translate-y-1/2 cursor-pointer place-items-center rounded text-subtle opacity-0 transition-opacity group-hover:opacity-100 hover:bg-overlay hover:text-fg focus-visible:opacity-100"
              >
                <IconX size={13} />
              </button>
            </li>
          );
        })}
      </ul>
      {error && <p className="px-3 pb-2 text-xs text-down">{error}</p>}
      <p className="border-t border-line px-3 py-2 text-2xs text-subtle">Free Yahoo quotes, can lag. Search a symbol to add it.</p>
    </aside>
  );
}
