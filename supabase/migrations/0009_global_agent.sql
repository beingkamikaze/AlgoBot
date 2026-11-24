-- Gold trading agent (XAUUSD, paper): every time the agent looks at the market, what it saw, what it proposed or why it
-- passed, what the reviewer said, and later how the idea would have turned out.
-- Run in the Supabase SQL editor after 0008. Safe to run again. No existing table changes.
-- Market-wide (not per user); only the backend (service role) reads and writes it: row-level security on, no policy.

create table if not exists public.global_agent_ideas (
  id uuid primary key default gen_random_uuid(),
  symbol text not null,
  kind text not null check (kind in ('trade', 'pass')),     -- a trade idea, or "no trade" with the reason
  side text check (side in ('buy', 'sell')),
  setup text,                                               -- breakout, pullback, reversal, range edge, other
  price_source text not null,                               -- 'spot' or 'futures'
  ref_price numeric,                                        -- the last price the agent saw
  stop numeric,
  target numeric,
  triggers jsonb not null default '[]',                     -- what made the agent look
  facts jsonb not null default '{}',                        -- the snapshot it was given (every number it may use)
  proposal jsonb not null default '{}',                     -- the strategist's answer
  review jsonb not null default '{}',                       -- the reviewer's answer
  state text not null default 'new' check (state in ('new', 'vetoed', 'expired', 'passed')),
  expires_at timestamptz,
  outcome jsonb,                                            -- the scorekeeper: what would have happened (filled later)
  model text,
  created_at timestamptz not null default now()
);
create index if not exists global_agent_ideas_recent on public.global_agent_ideas (symbol, created_at desc);

alter table public.global_agent_ideas enable row level security;

notify pgrst, 'reload schema';
