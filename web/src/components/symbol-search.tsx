"use client";

import { useEffect, useRef, useState } from "react";

import { IconSearch } from "@/components/ui/icons";
import { api, type SearchHit } from "@/lib/api";

export function SymbolSearch({ onPick }: { onPick: (symbol: string) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const query = q.trim();
    if (!query) return;
    timer.current = setTimeout(() => {
      api<SearchHit[]>(`/market/search?q=${encodeURIComponent(query)}`)
        .then((h) => {
          setHits(h);
          setOpen(true);
        })
        .catch(() => setHits([]));
    }, 200);
  }, [q]);

  function pick(symbol: string) {
    onPick(symbol);
    setQ("");
    setHits([]);
    setOpen(false);
  }

  return (
    <div className="relative w-full max-w-xs">
      <IconSearch size={14} className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-subtle" />
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && hits[0]) pick(hits[0].symbol);
          if (e.key === "Escape") setOpen(false);
        }}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        placeholder="Search NSE symbol or company"
        aria-label="Search symbol"
        role="combobox"
        aria-expanded={open && hits.length > 0}
        aria-controls="symbol-results"
        aria-autocomplete="list"
        className="field pl-8"
      />
      {open && q.trim() && hits.length > 0 && (
        <ul id="symbol-results" role="listbox" className="absolute z-30 mt-1 max-h-80 w-full overflow-auto rounded-md border border-line-strong bg-overlay p-1 shadow-[var(--shadow-pop)]" style={{ animation: "tb-in 120ms ease-out" }}>
          {hits.map((h) => (
            <li key={h.symbol} role="option" aria-selected={false}>
              <button
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => pick(h.symbol)}
                className="flex w-full cursor-pointer items-baseline gap-2 rounded px-2 py-1.5 text-left hover:bg-hover"
              >
                <span className="num text-[13px] font-medium">{h.symbol}</span>
                <span className="truncate text-xs text-muted">{h.name}</span>
                {h.kind === "index" && <span className="badge badge-neutral ml-auto">index</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
