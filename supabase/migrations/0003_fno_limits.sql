-- Phase 6: a separate max loss per trade for futures and options.
-- NULL (the default) means "use max_loss_per_trade", so nothing changes until you set it.
alter table public.risk_limits add column if not exists max_loss_per_trade_fno numeric;
