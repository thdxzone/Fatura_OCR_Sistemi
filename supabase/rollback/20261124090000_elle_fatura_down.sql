-- 20261124090000_elle_fatura.sql geri alma.
-- Elle girilmiş kayıt varsa NOT NULL geri konamaz ve betik hata verir; bu bilinçlidir:
-- önce o kayıtların ne olacağına karar verilmelidir (veri silinmez).

alter table fatura.belgeler drop constraint if exists belgeler_belge_zorunlu;
alter table fatura.belgeler alter column belge_yolu set not null;
