-- Run this in Supabase SQL Editor

-- Monitors table
create table monitors (
  id text primary key,
  user_id text not null,
  name text not null,
  keywords jsonb not null default '[]',
  location text default 'India',
  interval_minutes int default 15,
  telegram_token text,
  telegram_chat_id text,
  linkedin_cookie text,
  status text default 'active',
  posts_found int default 0,
  created_at timestamptz default now()
);

-- Alerts table
create table alerts (
  id uuid primary key default gen_random_uuid(),
  monitor_id text references monitors(id),
  user_id text not null,
  name text,
  keyword text,
  post_text text,
  post_url text,
  created_at timestamptz default now()
);

-- Enable Row Level Security
alter table monitors enable row level security;
alter table alerts enable row level security;

-- Policies (allow all for service role)
create policy "Service role access monitors" on monitors for all using (true);
create policy "Service role access alerts" on alerts for all using (true);
