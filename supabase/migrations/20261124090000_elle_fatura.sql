-- =====================================================================
-- Elle girilen (belgesiz) faturalar
--
-- Kullanıcı PDF yüklemeden fatura bilgilerini doğrudan girebilir. Bu
-- kayıtların mime_turu 'manual' olur ve bucket'ta nesnesi bulunmaz.
-- Belgesi olan kayıtlarda belge_yolu zorunluluğu kısıtla korunur.
--
-- Kodda: fatura.storage.MANUAL, Store.create_manual / PgStore.create_manual.
-- Geri alma: supabase/rollback/20261124090000_elle_fatura_down.sql
-- =====================================================================

alter table fatura.belgeler alter column belge_yolu drop not null;

-- belgeler_belge_yolu (yol biçimi) kısıtı aynen kalır; boş yol onu ihlal etmez.
alter table fatura.belgeler add constraint belgeler_belge_zorunlu
  check ((mime_turu = 'manual') = (belge_yolu is null));

comment on column fatura.belgeler.belge_yolu is
  'fatura-belgeler bucket''ındaki nesne yolu; elle girilen (mime_turu = manual) kayıtta boş.';
