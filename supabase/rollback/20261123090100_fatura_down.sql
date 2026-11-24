-- =====================================================================
-- 20261123090100_fatura_down.sql
-- 20261123090100_fatura.sql migration'ını geri alır.
--
-- DİKKAT: fatura şemasındaki TÜM veri silinir. Çalıştırmadan önce yedek alın
-- (pg_dump --schema=fatura) ve bucket'taki belgeleri dışa aktarın.
-- Bu dosya supabase/migrations dışında durur; CLI tarafından otomatik
-- uygulanmaz, yalnızca bilinçli olarak elle çalıştırılır.
-- =====================================================================

begin;

-- 1. Storage bucket. İçinde nesne varsa silme başarısız olur; önce
--    belgeler Storage API ile dışa aktarılıp silinmelidir. Bazı storage
--    sürümleri storage tablolarında doğrudan DELETE'i tetikleyiciyle engeller;
--    bu durumda bucket'ı Storage API'den (empty + delete) kaldırın ve bu
--    satırı atlayın.
delete from storage.buckets where id = 'fatura-belgeler';

-- 2. core.firmalar üzerindeki fatura politikaları ve yetkileri.
drop policy if exists fatura_backend_okur      on core.firmalar;
drop policy if exists fatura_backend_ekler     on core.firmalar;
drop policy if exists fatura_backend_gunceller on core.firmalar;
revoke all on core.firmalar from fatura_backend;
revoke usage on schema core from fatura_backend;

-- 3. Fatura şeması (tablolar, görünümler, politikalar, tetikleyiciler).
drop schema if exists fatura cascade;

-- 4. Backend rolü. Başka nesnede yetkisi kalmadıysa silinir.
drop role if exists fatura_backend;

commit;
