-- =====================================================================
-- 20261123090000_core_down.sql
-- 20261123090000_core.sql migration'ını geri alır.
--
-- DİKKAT: core şemasındaki ortak veri (firma kartları) silinir ve core'a
-- referans veren tüm proje şemaları etkilenir. Önce bütün proje
-- migration'ları geri alınmalıdır (ör. 20261123090100_fatura_down.sql).
-- Yedek: pg_dump --schema=core
-- =====================================================================

begin;

-- Proje şeması hâlâ core'a bağlıysa CASCADE onların yabancı anahtarlarını da
-- düşürür; bu yüzden önce proje şemalarının kaldırıldığını doğrulayın.
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'fatura') then
    raise exception 'Önce fatura şemasını geri alın (20261123090100_fatura_down.sql).';
  end if;
end;
$$;

drop schema if exists core cascade;

commit;
