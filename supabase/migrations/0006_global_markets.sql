-- Global markets (XAUUSD first): a USD paper account, its limits, orders and trades, kept apart from the NSE tables.
-- Run in the Supabase SQL editor after 0005. Safe to run again. No existing table changes.
-- The backend (service role) is the only writer; you can read your own rows.

create table if not exists public.global_accounts (
  user_id uuid primary key references auth.users on delete cascade,
  currency text not null default 'USD',
  starting_capital numeric not null default 10000,
  created_at timestamptz not null default now()
);

-- times are in the market's own clocks so they mean the same thing all year (London and New York change clocks)
create table if not exists public.global_limits (
  user_id uuid primary key references auth.users on delete cascade,
  max_loss_per_trade numeric not null default 20,
  max_daily_loss numeric not null default 60,
  max_monthly_loss numeric not null default 300,
  max_trades_per_day int not null default 5,
  max_consecutive_losses int not null default 3,
  max_open_positions int not null default 1,
  max_position_value numeric not null default 10000,
  entry_start_london time not null default '08:00',
  entry_end_new_york time not null default '11:00',
  flat_by_new_york time not null default '13:00',
  updated_at timestamptz not null default now()
);

create table if not exists public.global_orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  symbol text not null,
  side text not null check (side in ('buy', 'sell')),
  lots numeric not null,
  leg text not null check (leg in ('entry', 'exit')),
  state text not null default 'proposed'
    check (state in ('proposed', 'working', 'filled', 'expired', 'rejected', 'cancelled')),
  params jsonb not null,
  risk_verdict jsonb,
  trade_id uuid,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz
);
create index if not exists global_orders_user_state on public.global_orders (user_id, state);

create table if not exists public.global_trades (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  mode text not null default 'paper' check (mode in ('paper', 'live')),
  symbol text not null,
  side text not null check (side in ('buy', 'sell')),
  lots numeric not null,
  units numeric not null,
  price_source text not null,          -- 'spot' or 'futures': a trade is priced from one source only
  entry_ts timestamptz, exit_ts timestamptz,
  entry numeric, exit numeric,
  gross_pnl numeric, costs numeric, net_pnl numeric, r_multiple numeric,
  mae numeric, mfe numeric,
  exit_reason text,
  rules_followed boolean,
  trading_day date,                    -- the market's day (it rolls at 17:00 New York): daily limits count by it
  entry_context jsonb
);
create index if not exists global_trades_user_open on public.global_trades (user_id, exit_ts);

do $$
declare t text;
begin
  foreach t in array array['global_accounts', 'global_limits', 'global_orders', 'global_trades'] loop
    execute format('alter table public.%I enable row level security', t);
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = t and policyname = 'read own rows') then
      execute format('create policy "read own rows" on public.%I for select using (user_id = auth.uid())', t);
    end if;
  end loop;
end $$;

notify pgrst, 'reload schema';
