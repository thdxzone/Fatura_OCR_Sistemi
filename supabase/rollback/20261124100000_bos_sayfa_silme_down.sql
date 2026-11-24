-- 20261124100000_bos_sayfa_silme geri alma: backend yeniden yalnızca yumuşak silme yapar.
-- PgStore.discard bu yetki olmadan kaydı yumuşak silmeye (durum = 'deleted') düşer.
revoke delete on fatura.belgeler from fatura_backend;
