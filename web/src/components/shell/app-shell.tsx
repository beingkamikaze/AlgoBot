"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { FeedbackProvider } from "@/components/ui/feedback";
import {
  IconBell, IconBolt, IconBook, IconCandles, IconCap, IconChevronDown, IconDashboard, IconFlask, IconHistory, IconLayers,
  IconLogout, IconMenu, IconRadio, IconShield, IconSparkle, IconTarget, IconUser, IconWallet, IconX,
} from "@/components/ui/icons";
import { useFeedback } from "@/components/ui/feedback";
import { cx } from "@/components/ui/kit";
import { api, type Health, type LiveOverview, type MonitorSummary } from "@/lib/api";
import { supabase } from "@/lib/supabase";

const NAV: { group: string; items: { href: string; label: string; icon: (p: { size?: number }) => ReactNode }[] }[] = [
  { group: "Overview", items: [{ href: "/", label: "Dashboard", icon: IconDashboard }] },
  { group: "Markets", items: [{ href: "/chart", label: "Chart", icon: IconCandles }, { href: "/options", label: "Options", icon: IconLayers }] },
  { group: "Global", items: [{ href: "/global/chart", label: "XAUUSD chart", icon: IconCandles }, { href: "/global/signals", label: "XAUUSD signals", icon: IconBolt }, { href: "/global/agent", label: "XAUUSD agent", icon: IconSparkle }, { href: "/global/backtests", label: "XAUUSD backtests", icon: IconHistory }, { href: "/global/paper", label: "XAUUSD paper", icon: IconWallet }] },
  { group: "Trade", items: [{ href: "/signals", label: "Signals", icon: IconBolt }, { href: "/monitor", label: "Trade monitor", icon: IconTarget }, { href: "/paper", label: "Paper trading", icon: IconWallet }, { href: "/live", label: "Live", icon: IconRadio }] },
  { group: "Research", items: [{ href: "/backtests", label: "Backtests", icon: IconHistory }, { href: "/lab", label: "Lab", icon: IconFlask }, { href: "/tradingview", label: "TradingView alerts", icon: IconBell }] },
  { group: "Review", items: [{ href: "/journal", label: "Journal", icon: IconBook }, { href: "/forward", label: "Forward results", icon: IconShield }, { href: "/learn", label: "Indicator guide", icon: IconCap }] },
];

function Logo() {
  return (
    <Link href="/" className="flex items-center gap-2 px-1" aria-label="TradingBot dashboard">
      <span className="grid size-6 place-items-center rounded-[5px] bg-accent text-accent-ink">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M3 17l5-5 4 3 8-9" /><path d="M15 6h5v5" /></svg>
      </span>
      <span className="text-[14px] font-semibold tracking-tight text-fg">TradingBot</span>
    </Link>
  );
}

function SideNav({ onNavigate }: { onNavigate?: () => void }) {
  const path = usePathname();
  return (
    <nav aria-label="Main" className="flex-1 space-y-4 overflow-y-auto px-2 py-3">
      {NAV.map((g) => (
        <div key={g.group}>
          <p className="eyebrow px-2 pb-1">{g.group}</p>
          <ul className="space-y-px">
            {g.items.map((it) => {
              const active = it.href === "/" ? path === "/" : path.startsWith(it.href);
              const Icon = it.icon;
              return (
                <li key={it.href}>
                  <Link href={it.href} onClick={onNavigate} aria-current={active ? "page" : undefined}
                    className={cx("relative flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] transition-colors duration-150",
                      active ? "bg-raised font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg")}>
                    {active && <span className="absolute top-1.5 bottom-1.5 left-0 w-0.5 rounded-full bg-accent" />}
                    <span className={active ? "text-accent" : "text-subtle"}><Icon size={16} /></span>
                    {it.label}
                    {it.href === "/live" && <span className="ml-auto text-2xs text-subtle">real ₹</span>}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function IstClock() {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    const tick = () => setNow(new Date());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  if (!now) return <span className="num w-[68px] text-xs text-subtle">--:--:--</span>;
  return (
    <span className="num text-xs text-muted" title="India Standard Time">
      {now.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} <span className="text-subtle">IST</span>
    </span>
  );
}

const PHASE: Record<Health["market"]["phase"], { label: string; dot: string }> = {
  open: { label: "NSE open", dot: "bg-up" },
  "pre-open": { label: "Pre-open", dot: "bg-warn" },
  "post-close": { label: "NSE closed", dot: "bg-subtle" },
  closed: { label: "NSE closed", dot: "bg-subtle" },
};

function MarketStatus({ health, error }: { health: Health | null; error: boolean }) {
  if (error) return <span className="flex items-center gap-1.5 text-xs text-down"><span className="size-1.5 rounded-full bg-down" />Backend offline</span>;
  if (!health) return <span className="skeleton h-4 w-24" />;
  const m = health.market;
  const p = PHASE[m.phase];
  const next = !m.is_open ? new Date(m.next_open).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit" }) : null;
  return (
    <span className="flex items-center gap-1.5 text-xs" title={m.reason ?? undefined}>
      <span className="relative flex size-1.5">
        {m.is_open && <span className="absolute inline-flex size-full animate-ping rounded-full bg-up opacity-60" />}
        <span className={`relative inline-flex size-1.5 rounded-full ${p.dot}`} />
      </span>
      <span className="font-medium text-fg">{p.label}</span>
      {next && <span className="hidden text-subtle md:inline">· opens {next}</span>}
    </span>
  );
}

function AccountMenu({ session }: { session: Session }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const email = session.user.email ?? "account";
  return (
    <div ref={ref} className="relative">
      <button onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-haspopup="menu" className="btn btn-ghost h-8 gap-2 px-1.5">
        <span className="grid size-6 place-items-center rounded-full bg-raised text-2xs font-semibold text-fg uppercase ring-1 ring-line-strong">{email[0]}</span>
        <span className="hidden max-w-40 truncate text-xs lg:inline">{email}</span>
        <IconChevronDown size={14} />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 z-50 mt-1 w-60 rounded-md border border-line-strong bg-overlay p-1 shadow-[var(--shadow-pop)]" style={{ animation: "tb-in 120ms ease-out" }}>
          <div className="flex items-center gap-2 px-2 py-2">
            <IconUser size={15} />
            <span className="truncate text-xs text-muted">{email}</span>
          </div>
          <div className="my-1 h-px bg-line" />
          <button role="menuitem" onClick={() => supabase()?.auth.signOut()} className="flex h-8 w-full cursor-pointer items-center gap-2 rounded px-2 text-left text-[13px] text-fg hover:bg-hover">
            <IconLogout size={15} /> Sign out
          </button>
        </div>
      )}
    </div>
  );
}

/** Watches open trades from any page: a new alert shows as a notification (and a desktop one if you allowed it). */
function TradeWatcher() {
  const { toast } = useFeedback();
  const router = useRouter();
  const seen = useRef<Set<string> | null>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => {
      if (!(await supabase()?.auth.getSession())?.data.session) return;
      let list: MonitorSummary[];
      try { list = await api<MonitorSummary[]>("/monitor"); } catch { return; }
      if (stop) return;
      const alerts = list.flatMap((t) => t.alerts);
      if (seen.current === null) { seen.current = new Set(alerts.map((a) => a.id)); return; }   // first load: history, not news
      for (const a of alerts) {
        if (seen.current.has(a.id)) continue;
        seen.current.add(a.id);
        toast(a.level === "high" ? "error" : "info", a.title, a.detail);
        try {
          if (typeof Notification !== "undefined" && Notification.permission === "granted" && document.visibilityState !== "visible") {
            const n = new Notification(`TradingBot: ${a.title}`, { body: a.detail, tag: a.id });
            n.onclick = () => { window.focus(); router.push(`/monitor?trade=${a.trade_id}`); };
          }
        } catch { /* notifications unavailable */ }
      }
    };
    load();
    const id = setInterval(load, 20_000);
    return () => { stop = true; clearInterval(id); };
  }, [toast, router]);
  return null;
}

export function AppShell({ children }: { children: ReactNode }) {
  const [drawer, setDrawer] = useState(false);
  const [session, setSession] = useState<Session | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [offline, setOffline] = useState(false);
  const [armed, setArmed] = useState(false);
  const path = usePathname();

  useEffect(() => {
    const sb = supabase();
    if (!sb) return;
    sb.auth.getSession().then(({ data }) => setSession(data.session));
    const { data } = sb.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);

  useEffect(() => {
    const load = () => api<Health>("/health").then((h) => { setHealth(h); setOffline(false); }).catch(() => setOffline(true));
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {        // LIVE ARMED shows in the top bar only when every live gate is open
    if (!session) return;
    let stop = false;
    const load = () => api<LiveOverview>("/live").then((o) => !stop && setArmed(o.gates.every((g) => g.ok))).catch(() => !stop && setArmed(false));
    load();
    const id = setInterval(load, 60_000);
    return () => { stop = true; clearInterval(id); };
  }, [session]);

  useEffect(() => {
    document.body.style.overflow = drawer ? "hidden" : "";
  }, [drawer]);

  return (
    <FeedbackProvider>
      <TradeWatcher />
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[80] focus:rounded focus:bg-accent focus:px-3 focus:py-1.5 focus:text-accent-ink">Skip to content</a>
      <div className="flex min-h-dvh">
        <aside className="sticky top-0 hidden h-dvh w-[216px] shrink-0 flex-col border-r border-line bg-surface lg:flex">
          <div className="flex h-12 items-center border-b border-line px-3"><Logo /></div>
          <SideNav />
          <div className="border-t border-line px-3 py-2.5 text-2xs text-subtle">NSE equity · Nifty F&amp;O<br />Zerodha charges included</div>
        </aside>

        {drawer && (
          <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
            <div className="absolute inset-0 bg-black/60" onClick={() => setDrawer(false)} style={{ animation: "tb-fade 150ms ease-out" }} />
            <aside className="absolute inset-y-0 left-0 flex w-[260px] flex-col border-r border-line bg-surface" style={{ animation: "tb-slide 200ms ease-out" }}>
              <div className="flex h-12 items-center justify-between border-b border-line px-3">
                <Logo />
                <button onClick={() => setDrawer(false)} className="btn btn-ghost size-8 px-0" aria-label="Close navigation"><IconX size={16} /></button>
              </div>
              <SideNav onNavigate={() => setDrawer(false)} />
            </aside>
          </div>
        )}

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-40 flex h-12 items-center gap-3 border-b border-line bg-canvas/90 px-3 backdrop-blur-md sm:px-4">
            <button onClick={() => setDrawer(true)} className="btn btn-ghost size-8 px-0 lg:hidden" aria-label="Open navigation"><IconMenu size={18} /></button>
            <div className="lg:hidden"><Logo /></div>
            <div className="hidden items-center gap-4 sm:flex">
              <MarketStatus health={health} error={offline} />
              <span className="h-4 w-px bg-line" />
              <IstClock />
            </div>
            <div className="ml-auto flex items-center gap-2">
              {armed ? (
                <Link href="/live" className="badge badge-live h-6 px-2" title="Live trading is armed: tickets can send real orders">Live armed</Link>
              ) : (
                <span className="badge badge-warn h-6 px-2" title="Orders are simulated; no real money moves">Paper</span>
              )}
              {session && <AccountMenu session={session} />}
            </div>
          </header>
          <div className="flex items-center gap-3 border-b border-line px-3 py-1.5 sm:hidden">
            <MarketStatus health={health} error={offline} />
            <span className="ml-auto"><IstClock /></span>
          </div>
          <div id="main" key={path} className="flex flex-1 flex-col">{children}</div>
        </div>
      </div>
    </FeedbackProvider>
  );
}
