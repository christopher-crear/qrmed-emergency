-- QRMed Emergency · campañas y tickets de descuento
-- Ejecuta este archivo una sola vez en Supabase > SQL Editor.

create table if not exists public.discount_campaigns (
  id uuid primary key default gen_random_uuid(),
  code text not null,
  title text not null,
  description text,
  discount_type text not null default 'percentage'
    check (discount_type in ('percentage', 'fixed')),
  discount_value numeric(12,2) not null check (discount_value > 0),
  min_order_amount numeric(12,2) not null default 0 check (min_order_amount >= 0),
  max_claims integer not null default 1 check (max_claims > 0),
  starts_at timestamptz,
  expires_at timestamptz,
  is_active boolean not null default true,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint discount_campaign_dates check (
    expires_at is null or starts_at is null or expires_at > starts_at
  ),
  constraint discount_percentage_limit check (
    discount_type <> 'percentage' or discount_value <= 100
  )
);

create unique index if not exists discount_campaigns_code_ci_uidx
  on public.discount_campaigns (upper(code));
create index if not exists discount_campaigns_active_idx
  on public.discount_campaigns (is_active, expires_at);

create table if not exists public.discount_tickets (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.discount_campaigns(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  claimed_at timestamptz not null default now(),
  used_at timestamptz,
  order_id uuid references public.orders(id) on delete set null,
  unique (campaign_id, user_id)
);

create index if not exists discount_tickets_user_idx
  on public.discount_tickets (user_id, used_at);
create index if not exists discount_tickets_campaign_idx
  on public.discount_tickets (campaign_id);

alter table public.discount_campaigns enable row level security;
alter table public.discount_tickets enable row level security;

revoke all on public.discount_campaigns from anon;
revoke all on public.discount_tickets from anon;
revoke insert, update, delete on public.discount_campaigns from authenticated;
revoke insert, update, delete on public.discount_tickets from authenticated;
grant select on public.discount_campaigns to authenticated;
grant select on public.discount_tickets to authenticated;

drop policy if exists "Usuarios ven campañas disponibles" on public.discount_campaigns;
create policy "Usuarios ven campañas disponibles"
on public.discount_campaigns for select
to authenticated
using (
  is_active = true
  and (starts_at is null or starts_at <= now())
  and (expires_at is null or expires_at > now())
);

drop policy if exists "Usuarios ven sus tickets" on public.discount_tickets;
create policy "Usuarios ven sus tickets"
on public.discount_tickets for select
to authenticated
using ((select auth.uid()) = user_id);

comment on table public.discount_campaigns is
  'Campañas limitadas de descuento administradas exclusivamente por el servidor Django.';
comment on table public.discount_tickets is
  'Tickets reclamados por usuario; cada ticket se usa una sola vez.';
