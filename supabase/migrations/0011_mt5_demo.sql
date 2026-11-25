-- 0011: MT5 demo routing for the gold desk. Safe to re-run.
--   global_accounts.route: where your confirmed XAUUSD tickets go: 'paper' (the app's simulator, the default) or 'mt5_demo'
--   global_trades.mode:    adds 'demo' for trades filled at the MT5 demo account

alter table public.global_accounts add column if not exists route text not null default 'paper';
alter table public.global_accounts drop constraint if exists global_accounts_route_check;
alter table public.global_accounts add constraint global_accounts_route_check check (route in ('paper', 'mt5_demo'));

alter table public.global_trades drop constraint if exists global_trades_mode_check;
alter table public.global_trades add constraint global_trades_mode_check check (mode in ('paper', 'live', 'demo'));

notify pgrst, 'reload schema';
