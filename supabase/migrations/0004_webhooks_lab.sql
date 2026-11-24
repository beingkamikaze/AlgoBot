-- Phase 7: TradingView webhook sources and the learning loop.
-- Run in the Supabase SQL editor after 0003. Safe to run again.

-- one row per TradingView alert source; the secret is stored only as a SHA-256 hash
create table if not exists public.webhook_sources (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  name text not null,                         -- e.g. "My Nifty 15m Supertrend"
  secret_hash text not null,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz,
  alerts_received int not null default 0,
  alerts_rejected int not null default 0
);
create index if not exists webhook_sources_user on public.webhook_sources (user_id);

-- every alert as it arrived, accepted or not (for debugging a TradingView setup)
create table if not exists public.webhook_events (
  id bigserial primary key,
  source_id uuid not null references public.webhook_sources on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  received_at timestamptz not null default now(),
  remote_ip text,
  payload jsonb,
  outcome text not null,                      -- 'accepted', 'rejected', 'duplicate', 'rate_limited'
  detail text,
  signal_id uuid references public.signals on delete set null,
  order_id uuid references public.orders on delete set null
);
create index if not exists webhook_events_source on public.webhook_events (source_id, received_at desc);

alter table public.webhook_sources enable row level security;
drop policy if exists "read own webhook sources" on public.webhook_sources;
create policy "read own webhook sources" on public.webhook_sources for select using (user_id = auth.uid());
revoke select on public.webhook_sources from authenticated, anon;      -- the hash never reaches the browser
grant select (id, user_id, name, enabled, created_at, last_seen_at, alerts_received, alerts_rejected)
  on public.webhook_sources to authenticated;
alter table public.webhook_events enable row level security;
drop policy if exists "read own webhook events" on public.webhook_events;
create policy "read own webhook events" on public.webhook_events for select using (user_id = auth.uid());

-- the learning loop: a proposal's test results and the variant it created
alter table public.proposals add column if not exists test jsonb;                 -- baseline vs candidate, OOS, bootstrap
alter table public.proposals add column if not exists strategy_version_id uuid references public.strategy_versions;
alter table public.proposals add column if not exists decided_at timestamptz;
alter table public.strategy_versions add column if not exists user_id uuid references auth.users on delete cascade;
alter table public.strategy_versions add column if not exists note text;

-- make the API see the new columns now
notify pgrst, 'reload schema';
