-- Phase 4: journal, reviews, AI usage and trade-memory search.
-- Run in the Supabase SQL editor after 0001_init.sql.

-- daily / weekly reviews written by the AI analyst from SQL statistics
create table if not exists public.reviews (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  kind text not null check (kind in ('daily', 'weekly', 'brief')),
  period_start date not null,
  period_end date not null,
  stats jsonb not null default '{}',     -- the numbers, computed by code
  body jsonb not null default '{}',      -- the model's structured text, built only from `stats`
  model text,
  created_at timestamptz not null default now()
);
create index if not exists reviews_user_kind on public.reviews (user_id, kind, period_end desc);

-- every model call: what it cost, so a daily cap can be enforced
create table if not exists public.ai_usage (
  id bigserial primary key,
  user_id uuid references auth.users on delete set null,
  ts timestamptz not null default now(),
  purpose text not null,                 -- 'summary', 'postmortem', 'review', 'brief', 'ask', 'embedding'
  model text,
  prompt_tokens int, completion_tokens int, total_tokens int,
  ok boolean not null default true,
  detail jsonb
);
create index if not exists ai_usage_ts on public.ai_usage (ts desc);

alter table public.reviews enable row level security;
create policy "read own reviews" on public.reviews for select using (user_id = auth.uid());
alter table public.ai_usage enable row level security;
create policy "read own ai usage" on public.ai_usage for select using (user_id = auth.uid());

-- the user's own notes on a trade: writable from the browser too (RLS: own rows only)
alter table public.trade_journal add column if not exists ai_model text;
alter table public.trade_journal add column if not exists lessons text;

-- nearest past trades by meaning, for "similar trades from your journal"
create or replace function public.match_trade_memory(
  query_embedding vector(1536), match_user uuid, match_count int default 8)
returns table (id uuid, trade_id uuid, content text, metadata jsonb, similarity float)
language sql stable
as $$
  select m.id, m.trade_id, m.content, m.metadata, 1 - (m.embedding <=> query_embedding) as similarity
  from public.trade_memory m
  where m.user_id = match_user and m.embedding is not null
  order by m.embedding <=> query_embedding
  limit match_count;
$$;

-- only the backend (service role) may call it, with the user id it has verified
revoke execute on function public.match_trade_memory(vector, uuid, int) from public, anon, authenticated;
