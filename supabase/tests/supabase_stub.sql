-- =====================================================================
-- supabase_stub.sql — YALNIZCA YEREL TEST İÇİN
-- Sade bir PostgreSQL'de, Supabase'in hazır getirdiği ve migration'ların
-- dayandığı nesneleri taklit eder (roller ve storage.buckets).
-- Gerçek Supabase instance'ında ÇALIŞTIRILMAZ.
-- =====================================================================

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
end;
$$;

create schema if not exists storage;

create table if not exists storage.buckets (
  id                  text primary key,
  name                text not null unique,
  public              boolean default false,
  file_size_limit     bigint,
  allowed_mime_types  text[],
  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);
