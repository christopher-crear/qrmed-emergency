-- QRMed Emergency - Módulo "Bancos y cuentas"
-- Ejecutar una sola vez en Supabase > SQL Editor.
-- El script es idempotente: puede volver a ejecutarse si fuera necesario.

create extension if not exists pgcrypto;

create table if not exists public.bank_accounts (
    id uuid primary key default gen_random_uuid(),
    bank_name text not null,
    account_holder text not null,
    account_number text not null,
    account_type text not null default 'Ahorros',
    tax_id text not null,
    instructions text,
    is_visible boolean not null default true,
    display_order integer not null default 0,
    logo_path text,
    qr_path text,
    created_by uuid references public.profiles(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists bank_accounts_visible_order_idx
    on public.bank_accounts (is_visible, display_order, bank_name);

alter table public.bank_accounts enable row level security;

grant select, insert, update, delete on public.bank_accounts to authenticated;
grant all on public.bank_accounts to service_role;

-- Los pacientes autenticados solo pueden consultar cuentas publicadas.
do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'bank_accounts'
          and policyname = 'bank_accounts_visible_select'
    ) then
        create policy bank_accounts_visible_select
            on public.bank_accounts
            for select
            to authenticated
            using (is_visible = true or public.is_admin());
    end if;
end $$;

-- Los administradores pueden crear, editar, ocultar y eliminar cuentas.
do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'bank_accounts'
          and policyname = 'bank_accounts_admin_all'
    ) then
        create policy bank_accounts_admin_all
            on public.bank_accounts
            for all
            to authenticated
            using (public.is_admin())
            with check (public.is_admin());
    end if;
end $$;

-- Bucket privado para logos y códigos QR de bancos.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'bank-assets',
    'bank-assets',
    false,
    5242880,
    array['image/jpeg','image/png','image/webp']
)
on conflict (id) do update set
    name = excluded.name,
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- Cualquier usuario autenticado puede ver los logos/QR publicados a través del checkout.
do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'storage'
          and tablename = 'objects'
          and policyname = 'bank_assets_authenticated_select'
    ) then
        create policy bank_assets_authenticated_select
            on storage.objects
            for select
            to authenticated
            using (bucket_id = 'bank-assets');
    end if;
end $$;

-- Solo administradores pueden subir nuevos archivos bancarios.
do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'storage'
          and tablename = 'objects'
          and policyname = 'bank_assets_admin_insert'
    ) then
        create policy bank_assets_admin_insert
            on storage.objects
            for insert
            to authenticated
            with check (bucket_id = 'bank-assets' and public.is_admin());
    end if;
end $$;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'storage'
          and tablename = 'objects'
          and policyname = 'bank_assets_admin_update'
    ) then
        create policy bank_assets_admin_update
            on storage.objects
            for update
            to authenticated
            using (bucket_id = 'bank-assets' and public.is_admin())
            with check (bucket_id = 'bank-assets' and public.is_admin());
    end if;
end $$;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'storage'
          and tablename = 'objects'
          and policyname = 'bank_assets_admin_delete'
    ) then
        create policy bank_assets_admin_delete
            on storage.objects
            for delete
            to authenticated
            using (bucket_id = 'bank-assets' and public.is_admin());
    end if;
end $$;
