-- Global markets journal (XAUUSD): a post-mortem per closed paper trade, your notes, and daily / weekly reviews.
-- Run in the Supabase SQL editor after 0006. Safe to run again. No existing table changes.
-- The backend (service role) is the only writer; you can read your own rows.

create table if not exists public.global_trade_journal (
  trade_id uuid primary key references public.global_trades on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  ai_postmortem text,
  mistake_tags text[] not null default '{}',
  lessons text,
  user_notes text,
  ai_model text,                       -- the model that wrote it, or 'template' when the plain version was used
  facts jsonb not null default '{}',   -- the facts the post-mortem was written from (computed by code)
  updated_at timestamptz not null default now()
);
create index if not exists global_trade_journal_user on public.global_trade_journal (user_id);

create table if not exists public.global_reviews (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  kind text not null check (kind in ('daily', 'weekly')),
  period_start date not null,
  period_end date not null,
  stats jsonb not null default '{}',   -- the numbers, computed by code
  body jsonb not null default '{}',    -- the written review, built only from `stats`
  model text,
  created_at timestamptz not null default now()
);
create index if not exists global_reviews_user on public.global_reviews (user_id, created_at desc);

do $$
declare t text;
begin
  foreach t in array array['global_trade_journal', 'global_reviews'] loop
    execute format('alter table public.%I enable row level security', t);
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = t and policyname = 'read own rows') then
      execute format('create policy "read own rows" on public.%I for select using (user_id = auth.uid())', t);
    end if;
  end loop;
end $$;

notify pgrst, 'reload schema';
