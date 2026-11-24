-- Phase 8: live trading through Zerodha Kite (assisted: you confirm every order).
-- Run in the Supabase SQL editor after 0004. Safe to run again.

-- live limits start much smaller than paper ones; live is OFF until you arm it (with 2FA) each day
alter table public.risk_limits add column if not exists live_max_loss_per_trade numeric not null default 500;
alter table public.risk_limits add column if not exists live_max_daily_loss numeric not null default 1500;
alter table public.risk_limits add column if not exists live_max_trades_per_day int not null default 3;
alter table public.risk_limits add column if not exists live_max_order_value numeric not null default 50000;
alter table public.risk_limits add column if not exists live_armed_on date;          -- armed for this IST date only

-- the daily Kite session (token encrypted by the backend; it expires at 06:00 IST the next day)
alter table public.broker_accounts add column if not exists kite_user_id text;
alter table public.broker_accounts add column if not exists token_created_at timestamptz;

-- nothing broker-related is readable from the browser
revoke select on public.broker_accounts from authenticated, anon;
grant select (id, user_id, broker, label, starting_capital, is_live, created_at) on public.broker_accounts to authenticated;

create index if not exists orders_live_open on public.orders (user_id, state) where mode = 'live';

notify pgrst, 'reload schema';
