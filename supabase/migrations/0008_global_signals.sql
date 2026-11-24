-- Global markets live signals (XAUUSD): what the gold strategies find on live prices, each with its why card.
-- Run in the Supabase SQL editor after 0007. Safe to run again. No existing table changes.
-- Signals are market-wide (not per user) and only the backend (service role) reads and writes them: row-level security is on
-- with no policy, so the browser cannot read this table directly.

create table if not exists public.global_signals (
  id uuid primary key default gen_random_uuid(),
  symbol text not null,
  strategy_id text not null,
  strategy_version text not null,
  side text not null check (side in ('buy', 'sell')),
  timeframe text not null,
  signal_ts timestamptz not null,          -- when the signal bar closed
  price_source text not null,              -- 'spot' or 'futures'
  ref_price numeric not null,              -- the signal bar's close
  stop numeric not null,
  target_r numeric not null,
  explanation jsonb not null default '{}', -- rule, conditions, invalidation, evidence, confidence, card: all filled by code
  state text not null default 'new' check (state in ('new', 'expired')),
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (strategy_id, symbol, signal_ts)
);
create index if not exists global_signals_recent on public.global_signals (symbol, signal_ts desc);

alter table public.global_signals enable row level security;

notify pgrst, 'reload schema';
