-- =====================================================================
-- 20261123090000_core.sql
-- Ortak "core" şeması: birden fazla projenin paylaştığı veri ve yardımcılar.
--
-- Kurallar
--   * core hiçbir proje şemasına (fatura, thdx, ...) referans vermez.
--   * Proje şemaları yalnızca core'a referans verebilir, birbirine veremez.
--   * core'a hangi projenin erişeceği burada değil, her projenin kendi
--     migration dosyasında (GRANT + RLS politikası) tanımlanır. Böylece yeni
--     bir proje eklemek bu dosyaya dokunmayı gerektirmez.
--   * auth.users'a bağlı tablo, alan veya RLS kuralı yoktur.
--   * anon / authenticated rollerinin core'a hiçbir erişimi yoktur; şema
--     PostgREST'e (PGRST_DB_SCHEMAS) açılmaz.
--
-- Geri alma: supabase/rollback/20261123090000_core_down.sql
-- =====================================================================

begin;

-- ---------------------------------------------------------------------
-- 1. Şema ve varsayılan yetkiler
-- ---------------------------------------------------------------------
create schema if not exists core;

-- Supabase'in genel rollerine şema seviyesinde hiçbir yetki verilmez.
revoke all on schema core from public, anon, authenticated;

-- İleride bu şemada oluşturulacak nesneler de kendiliğinden açılmaz.
alter default privileges in schema core revoke all on tables    from public, anon, authenticated;
alter default privileges in schema core revoke all on sequences from public, anon, authenticated;
alter default privileges in schema core revoke all on functions from public, anon, authenticated;


-- ---------------------------------------------------------------------
-- 2. Yardımcı fonksiyonlar
-- ---------------------------------------------------------------------

-- Tüm şemalardaki tablolarda updated_at alanını günceller.
-- Kullanım: before update ... for each row execute function core.updated_at_ayarla();
create or replace function core.updated_at_ayarla()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

comment on function core.updated_at_ayarla() is
  'BEFORE UPDATE tetikleyicisi: updated_at alanını now() yapar.';

revoke all on function core.updated_at_ayarla() from public, anon, authenticated;


-- ---------------------------------------------------------------------
-- 3. Firma / tedarikçi kartı
--
-- Neden core'da:
--   Tedarikçi firma, fatura dışındaki projelerin de (ör. THDX Dijital
--   Hizmetler Platformu) kullanacağı ortak bir kavramdır. Proje şemaları
--   birbirine referans veremediği için ortak kart core'da tutulur.
--
-- Koddaki karşılığı:
--   Faturadaki satıcı bilgisi (seller.name / tax_id / address,
--   fatura/schema.json). Kodda ayrı bir firma kartı yoktur; kart, VKN/TCKN
--   okunduğunda backend tarafından eşleştirilir (yeni davranış, onaylandı).
--   Kodda karşılığı olmayan alanlar (vergi dairesi, aktiflik vb.) eklenmedi.
-- ---------------------------------------------------------------------
create table core.firmalar (
  id          uuid primary key default gen_random_uuid(),
  vkn         text,                              -- VKN (10 hane) veya TCKN (11 hane)
  unvan       text not null,
  adres       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  constraint firmalar_vkn_bicimi  check (vkn ~ '^[0-9]{10,11}$'),
  constraint firmalar_unvan_dolu  check (length(btrim(unvan)) > 0)
);

comment on table  core.firmalar     is 'Projeler arası ortak firma / tedarikçi kartı.';
comment on column core.firmalar.vkn is 'Boşluksuz VKN/TCKN; eşleştirme anahtarı. Okunamadıysa NULL.';

-- VKN biliniyorsa her firma bir kez kaydedilir; VKN'siz kartlar serbesttir.
create unique index firmalar_vkn_tekil on core.firmalar (vkn) where vkn is not null;

create trigger firmalar_updated_at
  before update on core.firmalar
  for each row execute function core.updated_at_ayarla();

-- RLS açık; politika yok = erişim yok. Erişim politikaları proje
-- migration'larında yalnızca ilgili backend rolü için tanımlanır.
alter table core.firmalar enable row level security;

revoke all on core.firmalar from public, anon, authenticated;

commit;
