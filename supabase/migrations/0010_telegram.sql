-- 0010: Telegram approval. Links your Telegram chat to your account so the bot can send gold agent ideas and paper
-- tickets, and take your confirmation with a tap. Safe to re-run.

create table if not exists public.telegram_links (
  user_id         uuid primary key references auth.users (id) on delete cascade,
  chat_id         bigint unique,              -- your private chat with the bot, set when you send it the link code
  username        text,
  link_code       text unique,                -- one-time code from the app, good for 10 minutes
  code_expires_at timestamptz,
  linked_at       timestamptz,
  updated_at      timestamptz not null default now()
);

-- only the backend (service role) reads or writes this table
alter table public.telegram_links enable row level security;

notify pgrst, 'reload schema';
