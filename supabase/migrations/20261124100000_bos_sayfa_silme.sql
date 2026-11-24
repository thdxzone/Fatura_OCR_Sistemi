-- =====================================================================
-- Fatura içermeyen sayfaların (boş arka yüz, imza/tarih sayfası) kaldırılması
--
-- Okuma sonunda fatura içeriği bulunmayan belge kalıcı olarak silinir;
-- kalemler ve OCR çalıştırmaları FK cascade ile gider. Backend yalnızca
-- henüz okunmakta olan (durum = 'processing') kaydı siler; diğer tüm
-- silmeler eskisi gibi yumuşak silmedir (durum = 'deleted').
--
-- Kodda: PgStore.discard.
-- Geri alma: supabase/rollback/20261124100000_bos_sayfa_silme_down.sql
-- =====================================================================

grant delete on fatura.belgeler to fatura_backend;
