-- TradingBot schema, Phase 0. Guide section 4.
-- Run in the Supabase SQL editor (or `supabase db push` once the CLI is linked).
-- Every user-owned table has Row Level Security: a user sees only their own rows.
-- The backend uses the service-role key and is the only writer of orders and fills.

create extension if not exists vector;
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- identity and settings
create table public.profiles (
  user_id uuid primary key references auth.users on delete cascade,
  display_name text,
  tz text not null default 'Asia/Kolkata',
  created_at timestamptz not null default now()
);

create table public.broker_accounts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  broker text not null check (broker in ('paper', 'zerodha')),
  label text not null default 'Paper',
  starting_capital numeric,
  api_key_ref text,                  -- a reference into Supabase Vault, never the key itself
  access_token_enc bytea,            -- today's Kite token, encrypted; expires daily
  token_expires_at timestamptz,
  is_live boolean not null default false,
  created_at timestamptz not null default now()
);

create table public.risk_limits (
  user_id uuid primary key references auth.users on delete cascade,
  max_loss_per_trade numeric not null default 2000,
  max_daily_loss numeric not null default 6000,
  max_monthly_loss numeric not null default 30000,
  max_trades_per_day int not null default 5,
  max_consecutive_losses int not null default 3,
  max_open_positions int not null default 3,
  max_margin_used_pct numeric not null default 50,
  entry_start time not null default '09:30',
  entry_end time not null default '14:45',
  flat_by time not null default '15:15',
  allow_naked_short_options boolean not null default false,
  updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------- market reference (shared, read-only to users)
create table public.instruments (
  instrument_token bigint primary key,
  exchange text not null,
  tradingsymbol text not null,
  segment text,
  instrument_type text,
  name text,
  expiry date,
  strike numeric,
  lot_size int,
  tick_size numeric,
  as_of date not null
);
create index on public.instruments (tradingsymbol);
create index on public.instruments (name, expiry);

create table public.candles (
  instrument_token bigint not null,
  tf text not null,                  -- '1m', '5m', '1d'
  ts timestamptz not null,
  o numeric not null, h numeric not null, l numeric not null, c numeric not null,
  v bigint, oi bigint,
  primary key (instrument_token, tf, ts)
);

create table public.market_holidays (
  day date primary key,
  description text not null,
  segment text not null default 'CM'
);

-- ---------------------------------------------------------------- strategies and evidence (shared definitions)
create table public.strategies (
  id text primary key,
  name text not null,
  description text,
  market text not null                -- 'equity_intraday', 'index_futures', 'index_options'
);

create table public.strategy_versions (
  id uuid primary key default gen_random_uuid(),
  strategy_id text not null references public.strategies,
  version text not null,
  params jsonb not null default '{}',
  code_hash text,
  status text not null default 'research'
    check (status in ('research', 'backtested', 'paper', 'live', 'retired')),
  created_at timestamptz not null default now(),
  unique (strategy_id, version)
);

create table public.backtests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  strategy_version_id uuid not null references public.strategy_versions,
  universe jsonb,
  start_date date, end_date date,
  cost_model_version text,
  in_sample boolean not null default true,
  metrics jsonb,
  created_at timestamptz not null default now()
);

create table public.backtest_trades (
  id bigserial primary key,
  backtest_id uuid not null references public.backtests on delete cascade,
  instrument_token bigint, symbol text,
  entry_ts timestamptz, exit_ts timestamptz,
  side text, qty int, entry numeric, exit numeric,
  costs numeric, r_multiple numeric, exit_reason text,
  regime jsonb
);

-- ---------------------------------------------------------------- signals, orders, trades
create table public.signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  strategy_version_id uuid references public.strategy_versions,
  source text not null check (source in ('strategy', 'tradingview', 'manual', 'ai_proposal')),
  symbol text not null,
  instrument_token bigint,
  side text not null check (side in ('buy', 'sell')),
  entry numeric, stop numeric, target numeric,
  context jsonb,                     -- indicator values and regime tags at signal time
  explanation jsonb not null,        -- the "why this trade" card, guide section 3.12
  status text not null default 'new'
    check (status in ('new', 'approved', 'rejected', 'expired', 'ordered')),
  created_at timestamptz not null default now()
);

create table public.orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  signal_id uuid references public.signals,
  account_id uuid not null references public.broker_accounts,
  mode text not null check (mode in ('paper', 'live')),
  broker_order_id text,
  params jsonb not null,
  state text not null default 'proposed',
  risk_verdict jsonb,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz
);

create table public.fills (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references public.orders on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  ts timestamptz not null,
  qty int not null, price numeric not null,
  costs jsonb
);

create table public.trades (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  mode text not null check (mode in ('paper', 'live')),
  signal_id uuid references public.signals,
  strategy_version_id uuid references public.strategy_versions,
  symbol text not null,
  instrument_token bigint,
  side text not null, qty int not null,
  entry_ts timestamptz, exit_ts timestamptz,
  entry numeric, exit numeric,
  gross_pnl numeric, costs numeric, net_pnl numeric, r_multiple numeric,
  mae numeric, mfe numeric,
  exit_reason text,
  rules_followed boolean,
  regime jsonb, entry_context jsonb
);

-- ---------------------------------------------------------------- journal and memory
create table public.trade_journal (
  trade_id uuid primary key references public.trades on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  ai_postmortem text,
  user_notes text,
  mistake_tags text[] not null default '{}',
  emotion text,
  chart_entry_path text, chart_exit_path text,
  updated_at timestamptz not null default now()
);

create table public.trade_memory (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  trade_id uuid references public.trades on delete cascade,
  content text not null,
  embedding vector(1536),            -- match the embedding model chosen in Phase 4
  metadata jsonb,
  created_at timestamptz not null default now()
);
create index on public.trade_memory using hnsw (embedding vector_cosine_ops);

create table public.proposals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  strategy_id text references public.strategies,
  hypothesis text not null,
  change jsonb not null,
  evidence jsonb,
  oos_backtest_id uuid references public.backtests,
  paper_result jsonb,
  status text not null default 'new'
    check (status in ('new', 'testing', 'passed', 'failed', 'approved', 'rejected')),
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------- safety
create table public.risk_events (
  id bigserial primary key,
  user_id uuid not null references auth.users on delete cascade,
  ts timestamptz not null default now(),
  kind text not null,                -- 'block', 'kill_switch', 'reconcile_mismatch', ...
  detail jsonb
);

create table public.audit_log (
  id bigserial primary key,
  user_id uuid references auth.users on delete set null,
  ts timestamptz not null default now(),
  actor text not null,               -- 'user', 'system', 'ai'
  action text not null,
  detail jsonb
);

-- ---------------------------------------------------------------- row level security
do $$
declare t text;
begin
  -- user-owned tables: a user reads and writes only their own rows
  foreach t in array array['profiles', 'risk_limits', 'trade_journal', 'proposals'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('create policy "own rows" on public.%I for all using (user_id = auth.uid()) with check (user_id = auth.uid())', t);
  end loop;
  -- read-only to their owner: only the backend (service role) writes these
  foreach t in array array['broker_accounts', 'backtests', 'signals', 'orders', 'fills', 'trades',
                           'trade_memory', 'risk_events', 'audit_log'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('create policy "read own rows" on public.%I for select using (user_id = auth.uid())', t);
  end loop;
  -- shared reference data: any signed-in user may read; only the backend writes
  foreach t in array array['instruments', 'candles', 'market_holidays', 'strategies', 'strategy_versions'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('create policy "signed-in read" on public.%I for select to authenticated using (true)', t);
  end loop;
end $$;

alter table public.backtest_trades enable row level security;
create policy "read own backtest trades" on public.backtest_trades for select using (
  exists (select 1 from public.backtests b where b.id = backtest_id and b.user_id = auth.uid()));

-- broker credentials never reach the browser, even for their owner
revoke select on public.broker_accounts from authenticated, anon;
grant select (id, user_id, broker, label, starting_capital, token_expires_at, is_live, created_at)
  on public.broker_accounts to authenticated;

-- ---------------------------------------------------------------- new user: profile, limits, paper account
create function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (user_id, display_name) values (new.id, split_part(new.email, '@', 1));
  insert into public.risk_limits (user_id) values (new.id);
  insert into public.broker_accounts (user_id, broker, label, starting_capital)
    values (new.id, 'paper', 'Paper', 1000000);
  return new;
end $$;

create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- NSE 2026 holidays (capital market)
insert into public.market_holidays (day, description) values
  ('2026-01-15', 'Municipal Corporation Election - Maharashtra'), ('2026-01-26', 'Republic Day'),
  ('2026-03-03', 'Holi'), ('2026-03-26', 'Shri Ram Navami'), ('2026-03-31', 'Shri Mahavir Jayanti'),
  ('2026-04-03', 'Good Friday'), ('2026-04-14', 'Dr. Baba Saheb Ambedkar Jayanti'),
  ('2026-05-01', 'Maharashtra Day'), ('2026-05-28', 'Bakri Id'), ('2026-06-26', 'Muharram'),
  ('2026-09-14', 'Ganesh Chaturthi'), ('2026-10-02', 'Mahatma Gandhi Jayanti'), ('2026-10-20', 'Dussehra'),
  ('2026-11-10', 'Diwali-Balipratipada'), ('2026-11-24', 'Prakash Gurpurb Sri Guru Nanak Dev'),
  ('2026-12-25', 'Christmas');
