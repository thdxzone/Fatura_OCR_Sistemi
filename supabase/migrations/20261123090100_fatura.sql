-- =====================================================================
-- 20261123090100_fatura.sql
-- Fatura OCR Sistemi: "fatura" proje şeması.
--
-- Kurallar
--   * Yalnızca core'a referans verir; başka proje şemalarına asla.
--   * Son kullanıcı / login / profil yoktur; auth.users'a bağlı hiçbir şey yok.
--   * Veriye yalnızca backend erişir: fatura_backend rolü. RLS açıktır ve
--     politikalar yalnızca bu rol içindir. anon / authenticated / service_role
--     rollerine fatura tablolarında yetki verilmez.
--   * Şema PostgREST'e (PGRST_DB_SCHEMAS) açılmaz; backend Supavisor
--     üzerinden doğrudan Postgres'e bağlanır.
--   * Belgeler Supabase Storage'daki private "fatura-belgeler" bucket'ında
--     tutulur; tabloda yalnızca nesne yolu durur.
--
-- Bağımlılık: 20261123090000_core.sql
-- Geri alma:  supabase/rollback/20261123090100_fatura_down.sql
-- Ayrıntılı açıklama ve kod referansları: docs/veritabani.md
-- =====================================================================

begin;

-- ---------------------------------------------------------------------
-- 1. Şema ve backend rolü
-- ---------------------------------------------------------------------
create schema if not exists fatura;

revoke all on schema fatura from public, anon, authenticated;
alter default privileges in schema fatura revoke all on tables    from public, anon, authenticated;
alter default privileges in schema fatura revoke all on sequences from public, anon, authenticated;
alter default privileges in schema fatura revoke all on functions from public, anon, authenticated;

-- Backend'in bağlanacağı rol. LOGIN ve parola migration DIŞINDA verilir
-- (bkz. dosya sonu). Rol RLS'i atlamaz; politikalara tabidir.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'fatura_backend') then
    create role fatura_backend nologin noinherit;
  end if;
end;
$$;

comment on role fatura_backend is 'Fatura OCR backend servisi; yalnızca core ve fatura şemalarına erişir.';


-- ---------------------------------------------------------------------
-- 2. Hizmetler ve zam analizi başlangıç verisi
--    Kaynak: data/reference/increase-analysis.json (Excel aktarımı) ve
--            data/reference/service-tracking.json (elle eklenen / düzeltilen)
--    Kod:    fatura/analysis/services.py
-- ---------------------------------------------------------------------

-- Zam analizinde izlenen hizmetler (Excel satırı veya elle eklenen).
create table fatura.hizmetler (
  id                       uuid primary key default gen_random_uuid(),
  eski_kod                 text unique,          -- SQLite dönemi kimliği: excel-row-N / service-<hex>
  kaynak                   text not null,        -- excel | elle
  excel_satir              integer,              -- source_row
  kurum                    text not null,        -- institution (serbest metin)
  aciklama                 text not null,        -- description (hizmet adı)
  para_birimi              char(3) not null default 'TRY',
  hizmet_turu              text,                 -- service_type (ör. 'Düzenli (aylık)', 'Bitti')
  tek_adet_tarihi          date,                 -- single_date
  baslangic_tarihi         date,                 -- start_date
  bitis_tarihi             date,                 -- end_date
  baslangic_elle           boolean not null default false,  -- start_date anahtarı elle verildi mi
  bitis_elle               boolean not null default false,  -- end_date anahtarı elle verildi mi
  notu                     text,                 -- note
  baslangic_fatura_sayisi  integer not null default 0,      -- invoice_count (Excel başlangıcı)
  birim_fiyat              numeric(18,4),        -- unit_price
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),

  constraint hizmetler_kaynak         check (kaynak in ('excel', 'elle')),
  constraint hizmetler_excel_satiri   check ((kaynak = 'excel') = (excel_satir is not null)),
  constraint hizmetler_kurum          check (length(btrim(kurum)) between 1 and 300),
  constraint hizmetler_aciklama       check (length(btrim(aciklama)) between 1 and 300),
  constraint hizmetler_para_birimi    check (para_birimi ~ '^[A-Z]{3}$'),
  constraint hizmetler_hizmet_turu    check (length(hizmet_turu) <= 100),
  constraint hizmetler_notu           check (length(notu) <= 10000),
  constraint hizmetler_tarih_sirasi   check (bitis_tarihi is null or baslangic_tarihi is null
                                             or bitis_tarihi >= baslangic_tarihi),
  constraint hizmetler_fatura_sayisi  check (baslangic_fatura_sayisi between 0 and 1000000),
  constraint hizmetler_birim_fiyat    check (birim_fiyat >= 0)
);

comment on table  fatura.hizmetler is 'Zam analizinde izlenen hizmetler; Excel başlangıcı + elle eklenenler.';
comment on column fatura.hizmetler.baslangic_elle is
  'true: baslangic_tarihi elle girildi (NULL ise "yok" demektir). false: dönem nottan çıkarılır (period_from_note).';
comment on column fatura.hizmetler.bitis_elle is
  'true: bitis_tarihi elle girildi (NULL ise "yok" demektir). false: dönem nottan çıkarılır (period_from_note).';

-- Hizmetin yıllara göre başlangıç (Excel) bedeli. Zam oranları buradan hesaplanır.
create table fatura.hizmet_yillik_bedeller (
  id          bigint generated always as identity primary key,
  hizmet_id   uuid not null references fatura.hizmetler (id) on delete cascade,
  yil         smallint not null,
  bedel       numeric(18,2),                     -- NULL: o yıl için bedel yok
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  constraint hizmet_yillik_bedeller_tekil unique (hizmet_id, yil),
  constraint hizmet_yillik_bedeller_yil   check (yil between 1900 and 2100),
  constraint hizmet_yillik_bedeller_bedel check (bedel >= 0)
);

-- Geçmiş yıllar için elle girilen ödeme toplamları (yearly_payments).
create table fatura.hizmet_yillik_odemeler (
  id                  bigint generated always as identity primary key,
  hizmet_id           uuid not null references fatura.hizmetler (id) on delete cascade,
  yil                 smallint not null,
  fatura_sayisi       integer not null,
  toplam              numeric(18,2) not null,
  kapsanan_son_tarih  date not null,             -- through_date: bu tarihe kadarki PDF'ler tekrar sayılmaz
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  constraint hizmet_yillik_odemeler_tekil  unique (hizmet_id, yil),
  constraint hizmet_yillik_odemeler_yil    check (yil between 1900 and 2100),
  constraint hizmet_yillik_odemeler_sayi   check (fatura_sayisi between 0 and 1000000),
  constraint hizmet_yillik_odemeler_toplam check (toplam >= 0),
  constraint hizmet_yillik_odemeler_tarih  check (extract(year from kapsanan_son_tarih) = yil)
);

-- Eşleştirmede hizmet adına ek olarak aranan adlar (aliases). Kullanıcı hizmet
-- düzenleme ekranından veya fatura incelemesinde elle seçtiği hizmete faturanın
-- açıklamasını ekleyerek doldurur.
create table fatura.hizmet_takma_adlari (
  id          bigint generated always as identity primary key,
  hizmet_id   uuid not null references fatura.hizmetler (id) on delete cascade,
  takma_ad    text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  constraint hizmet_takma_adlari_tekil unique (hizmet_id, takma_ad),
  constraint hizmet_takma_adlari_dolu  check (length(btrim(takma_ad)) > 0)
);

-- Excel aktarımının bilgisi ve takip başlangıcı (tek satır).
create table fatura.referans_aktarimi (
  id                smallint primary key default 1,
  kaynak_dosya      text,                        -- source_name
  sayfa             text,                        -- sheet
  kaynak_sha256     text,                        -- source_sha256
  aktarim_tarihi    date,                        -- imported_date
  baz_tarihi        date,                        -- baseline_as_of: bu tarihe kadarki PDF'ler Excel bedelini ezmez
  tutar_esasi       text,                        -- basis
  oran_esasi        text,                        -- rates_basis
  kurum_kaynagi     text,                        -- institution_source
  takip_baslangici  timestamptz not null,        -- service-tracking.json started_at
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),

  constraint referans_aktarimi_tek_satir check (id = 1),
  constraint referans_aktarimi_sha256    check (kaynak_sha256 ~ '^[0-9a-f]{64}$')
);

-- Excel'de zaten sayılmış fatura numaraları (baseline_invoice_numbers).
create table fatura.referans_fatura_numaralari (
  id                 bigint generated always as identity primary key,
  aktarim_id         smallint not null default 1 references fatura.referans_aktarimi (id) on delete cascade,
  fatura_no          text not null,
  fatura_no_anahtar  text not null,              -- services.words() ile normalize edilmiş
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  constraint referans_fatura_numaralari_tekil unique (aktarim_id, fatura_no_anahtar)
);


-- ---------------------------------------------------------------------
-- 3. Belgeler (faturalar)
--    Kaynak: SQLite documents tablosu + edited_json başlık alanları
--    Kod:    fatura/storage.py, fatura/web/app.py
--
--    Durum akışı (koddaki değerler korunur):
--      processing → review → approved
--      processing → duplicate        (aynı fatura no aktif bir kayıtta varsa)
--      review     → processing       (yeniden analiz)
--      *          → deleted          (yumuşak silme; processing hariç)
-- ---------------------------------------------------------------------
create table fatura.belgeler (
  id                 uuid primary key default gen_random_uuid(),

  -- Belge ve işlem bilgisi
  dosya_adi          text not null,              -- filename
  mime_turu          text not null default 'application/pdf',
  belge_yolu         text not null unique,       -- fatura-belgeler bucket'ındaki nesne yolu
  durum              text not null default 'processing',
  revizyon           integer not null default 1, -- iyimser kilit (revision)
  hata_mesaji        text,                       -- error
  mukerrer_belge_id  uuid references fatura.belgeler (id) on delete set null,  -- duplicate_of
  grup_id            uuid,                       -- batch_id: sayfalara ayrılmış PDF
  grup_sayfa         integer,                    -- batch_page
  grup_sayfa_sayisi  integer,                    -- batch_count

  -- Fatura başlığı (kullanıcının düzelttiği güncel hâl)
  firma_id           uuid references core.firmalar (id) on delete set null,
  satici_unvan       text,                       -- seller.name
  satici_vkn         text,                       -- seller.tax_id (okunduğu hâliyle)
  satici_adres       text,                       -- seller.address
  fatura_no          text,                       -- invoice_number
  fatura_no_anahtar  text,                       -- storage.invoice_key(): NFKC + büyük harf + boşluksuz
  fatura_tarihi      date,                       -- invoice_date
  para_birimi        char(3),                    -- currency
  fatura_notu        text,                       -- invoice_note
  hizmet_id          uuid references fatura.hizmetler (id) on delete set null,  -- service_id

  -- Toplamlar
  toplam_net         numeric(18,2),              -- totals.net
  toplam_kdv         numeric(18,2),              -- totals.vat
  toplam_brut        numeric(18,2),              -- totals.gross
  toplam_iskonto     numeric(18,2),              -- totals.discount
  toplam_tevkifat    numeric(18,2),              -- totals.withholding
  odenecek_tutar     numeric(18,2),              -- totals.payable

  -- İnceleme
  inceleme_gerekli   boolean not null default true,       -- needs_review
  uyarilar           jsonb not null default '[]'::jsonb,  -- issues: [{code, detail}]
  kontroller         jsonb not null default '[]'::jsonb,  -- checks: [{field, observed, calculated, ok}]
  onaylanma_zamani   timestamptz,                         -- approved_at

  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  constraint belgeler_durum check (durum in ('processing', 'review', 'approved', 'duplicate', 'deleted')),
  constraint belgeler_revizyon check (revizyon >= 1),
  constraint belgeler_dosya_adi check (length(dosya_adi) between 1 and 200),
  constraint belgeler_belge_yolu check (belge_yolu ~ '^[0-9]{4}/[0-9]{2}/[0-9a-f-]{36}\.pdf$'),
  constraint belgeler_para_birimi check (para_birimi ~ '^[A-Z]{3}$'),
  constraint belgeler_fatura_no_anahtar check ((fatura_no is null) = (fatura_no_anahtar is null)),
  constraint belgeler_uyarilar_dizi check (jsonb_typeof(uyarilar) = 'array'),
  constraint belgeler_kontroller_dizi check (jsonb_typeof(kontroller) = 'array'),
  constraint belgeler_grup check (
    (grup_id is null and grup_sayfa is null and grup_sayfa_sayisi is null)
    or (grup_id is not null and grup_sayfa between 1 and grup_sayfa_sayisi)
  ),
  -- duplicate durumundaki kayıt hangi kaydın tekrarı olduğunu gösterir.
  -- Silinen mükerrer kayıt, neyin mükerreri olduğunu korur (SQLite'taki gibi).
  constraint belgeler_mukerrer check (
    case durum
      when 'duplicate' then mukerrer_belge_id is not null
      when 'deleted'   then true
      else mukerrer_belge_id is null
    end
  ),
  -- Onay için zorunlu başlık alanları (app.approval_errors ile aynı).
  constraint belgeler_onay_zorunlu check (
    durum <> 'approved' or (
      onaylanma_zamani is not null
      and fatura_no is not null and fatura_tarihi is not null and para_birimi is not null
      and satici_unvan is not null
      and toplam_net is not null and toplam_kdv is not null and toplam_brut is not null
    )
  )
);

comment on table  fatura.belgeler is 'Yüklenen fatura belgeleri ve kullanıcının onayladığı/düzelttiği güncel başlık bilgisi.';
comment on column fatura.belgeler.belge_yolu is 'fatura-belgeler bucket''ında {yil}/{ay}/{id}.pdf';
comment on column fatura.belgeler.fatura_no_anahtar is 'Backend hesaplar (storage.invoice_key); mükerrer kontrolünün anahtarı.';

-- Mükerrer kontrolü: fatura no satıcıdan bağımsız, tüm aktif kayıtlarda tekildir.
-- (deleted ve duplicate kayıtlar kontrol dışıdır; storage.duplicate ile aynı kural.)
create unique index belgeler_fatura_no_tekil
  on fatura.belgeler (fatura_no_anahtar)
  where fatura_no_anahtar is not null and durum in ('processing', 'review', 'approved');

-- Liste ekranı: created_at'e göre azalan sıralama, durum ve para birimi filtresi.
create index belgeler_liste      on fatura.belgeler (created_at desc) where durum not in ('deleted', 'duplicate');
create index belgeler_durum      on fatura.belgeler (durum);
-- Analiz ekranı: onaylı faturalar, para birimi ve fatura tarihine göre.
create index belgeler_analiz     on fatura.belgeler (para_birimi, fatura_tarihi) where durum = 'approved';
create index belgeler_tarih      on fatura.belgeler (fatura_tarihi);
create index belgeler_firma      on fatura.belgeler (firma_id);
create index belgeler_hizmet     on fatura.belgeler (hizmet_id);
create index belgeler_grup       on fatura.belgeler (grup_id, grup_sayfa) where grup_id is not null;
create index belgeler_mukerrer   on fatura.belgeler (mukerrer_belge_id) where mukerrer_belge_id is not null;

-- Fatura kalemleri (edited_json.items).
create table fatura.kalemler (
  id             uuid primary key default gen_random_uuid(),
  belge_id       uuid not null references fatura.belgeler (id) on delete cascade,
  sira           integer not null,
  ad             text,                           -- name
  aciklama       text,                           -- description
  birim          text,                           -- unit
  miktar         numeric(18,4),                  -- quantity
  birim_fiyat    numeric(18,4),                  -- unit_price
  kdv_orani      numeric(5,2),                   -- vat_rate
  satir_toplami  numeric(18,2),                  -- line_total
  iskonto        numeric(18,2),                  -- discount
  fiyat_esasi    text not null default 'unknown',-- price_basis: birim fiyat KDV hariç/dahil
  toplam_esasi   text not null default 'unknown',-- total_basis: satır toplamı KDV hariç/dahil
  kanit          jsonb,                          -- evidence: OCR'ın belgede bulduğu konum; elle düzenlenince NULL
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  constraint kalemler_sira_tekil  unique (belge_id, sira),
  constraint kalemler_sira        check (sira >= 1),
  constraint kalemler_miktar      check (miktar > 0),
  constraint kalemler_kdv_orani   check (kdv_orani between 0 and 100),
  constraint kalemler_fiyat_esasi check (fiyat_esasi  in ('net', 'gross', 'unknown')),
  constraint kalemler_toplam_esasi check (toplam_esasi in ('net', 'gross', 'unknown'))
);

-- Her OCR çalıştırması (ilk okuma + yeniden analizler). İlk okuma =
-- belgenin en eski kaydı (SQLite'taki original_json).
create table fatura.ocr_calismalari (
  id           bigint generated always as identity primary key,
  -- Ertelenebilir: veri taşıması belgeyi kaynak_ocr_id'siyle tek INSERT'te yazabilsin
  -- (sonradan UPDATE, tetikleyici yüzünden özgün updated_at'i ezerdi). Uygulama ertelemez.
  belge_id     uuid not null
               constraint ocr_calismalari_belge_id_fkey references fatura.belgeler (id)
               on delete cascade deferrable initially immediate,
  motor        text,                           -- record.reader (ör. 'paddle'); prototip okuyucuda NULL
  sure_saniye  numeric(10,3),                    -- metrics.elapsed_seconds
  hata_mesaji  text,                             -- okuma tamamlanamadıysa
  ham_cikti    jsonb not null,                   -- tüm kayıt: evidence, pages, checks, issues, metrics...
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  constraint ocr_calismalari_ham_cikti check (jsonb_typeof(ham_cikti) = 'object')
);

create index ocr_calismalari_belge on fatura.ocr_calismalari (belge_id, created_at);

-- Güncel kaydın kanıt, sayfa metni ve ölçümleri hangi OCR çalıştırmasından
-- geliyor: yeniden analizden sonra son çalıştırma, kullanıcı kaydından sonra
-- ilk okuma (storage.analyzed / app.normalized_record).
alter table fatura.belgeler
  add column kaynak_ocr_id bigint references fatura.ocr_calismalari (id) on delete set null;

comment on column fatura.belgeler.kaynak_ocr_id is
  'Güncel kaydın evidence/pages/metrics alanlarının alındığı OCR çalıştırması.';

-- Her onayın anlık kopyası (denetim izi; approvals tablosu).
create table fatura.onaylar (
  id                bigint generated always as identity primary key,
  belge_id          uuid not null references fatura.belgeler (id) on delete cascade,
  onaylanma_zamani  timestamptz not null,
  kayit             jsonb not null,              -- onaylanan kaydın tamamı
  kontroller        jsonb not null default '[]'::jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),

  constraint onaylar_kayit      check (jsonb_typeof(kayit) = 'object'),
  constraint onaylar_kontroller check (jsonb_typeof(kontroller) = 'array')
);

create index onaylar_belge on fatura.onaylar (belge_id, onaylanma_zamani);


-- ---------------------------------------------------------------------
-- 4. Hesaplanan değerler (tabloda tutulmaz)
--    services.ratio(): (bu yıl - önceki yıl) / önceki yıl, 4 ondalık.
--    Not: Bu görünümler yalnızca başlangıç bedellerini kullanır; onaylı
--    yeni faturaların yıllık bedele yansıtılması uygulama mantığında kalır
--    (services.analysis).
-- ---------------------------------------------------------------------
create view fatura.hizmet_zam_oranlari
with (security_invoker = true) as
select
  b.hizmet_id,
  b.yil,
  b.bedel,
  o.bedel as onceki_yil_bedeli,
  case
    when b.bedel is null or o.bedel is null or o.bedel = 0 then null
    else round((b.bedel - o.bedel) / o.bedel, 4)
  end as zam_orani
from fatura.hizmet_yillik_bedeller b
left join fatura.hizmet_yillik_bedeller o
  on o.hizmet_id = b.hizmet_id and o.yil = b.yil - 1;

comment on view fatura.hizmet_zam_oranlari is 'Yıllık zam oranı (rates); services.ratio ile aynı formül.';

create view fatura.hizmet_toplam_artis
with (security_invoker = true) as
select
  h.id as hizmet_id,
  ilk.yil   as ilk_yil,
  ilk.bedel as ilk_bedel,
  son.yil   as son_yil,
  son.bedel as son_bedel,
  case
    when ilk.yil is null or ilk.yil = son.yil or ilk.bedel = 0 then null
    else round((son.bedel - ilk.bedel) / ilk.bedel, 4)
  end as toplam_artis
from fatura.hizmetler h
left join lateral (
  select yil, bedel from fatura.hizmet_yillik_bedeller
  where hizmet_id = h.id and bedel is not null order by yil asc limit 1
) ilk on true
left join lateral (
  select yil, bedel from fatura.hizmet_yillik_bedeller
  where hizmet_id = h.id and bedel is not null order by yil desc limit 1
) son on true;

comment on view fatura.hizmet_toplam_artis is 'İlk ve son bedelli yıl arasındaki artış (total_increase).';


-- ---------------------------------------------------------------------
-- 5. updated_at tetikleyicileri
-- ---------------------------------------------------------------------
create trigger hizmetler_updated_at                  before update on fatura.hizmetler                  for each row execute function core.updated_at_ayarla();
create trigger hizmet_yillik_bedeller_updated_at     before update on fatura.hizmet_yillik_bedeller     for each row execute function core.updated_at_ayarla();
create trigger hizmet_yillik_odemeler_updated_at     before update on fatura.hizmet_yillik_odemeler     for each row execute function core.updated_at_ayarla();
create trigger hizmet_takma_adlari_updated_at        before update on fatura.hizmet_takma_adlari        for each row execute function core.updated_at_ayarla();
create trigger referans_aktarimi_updated_at          before update on fatura.referans_aktarimi          for each row execute function core.updated_at_ayarla();
create trigger referans_fatura_numaralari_updated_at before update on fatura.referans_fatura_numaralari for each row execute function core.updated_at_ayarla();
create trigger belgeler_updated_at                   before update on fatura.belgeler                   for each row execute function core.updated_at_ayarla();
create trigger kalemler_updated_at                   before update on fatura.kalemler                   for each row execute function core.updated_at_ayarla();
create trigger ocr_calismalari_updated_at            before update on fatura.ocr_calismalari            for each row execute function core.updated_at_ayarla();
create trigger onaylar_updated_at                    before update on fatura.onaylar                    for each row execute function core.updated_at_ayarla();


-- ---------------------------------------------------------------------
-- 6. Yetkiler (GRANT) — en az yetki
--    anon / authenticated / service_role: hiçbir şey.
--    fatura_backend: kodun gerçekten yaptığı işlemler kadar.
-- ---------------------------------------------------------------------
revoke all on all tables    in schema fatura from public, anon, authenticated, service_role;
revoke all on all sequences in schema fatura from public, anon, authenticated, service_role;

grant usage on schema core, fatura to fatura_backend;

-- core: firma kartı okunur, eklenir, güncellenir; silinmez.
grant select, insert, update on core.firmalar to fatura_backend;

-- Belgeler yumuşak silinir (durum = 'deleted'); fiziksel DELETE yetkisi yok.
grant select, insert, update on fatura.belgeler to fatura_backend;
-- Kalemler kayıt her kaydedildiğinde yeniden yazılır.
grant select, insert, update, delete on fatura.kalemler to fatura_backend;
-- Denetim izi: yalnızca eklenir ve okunur.
grant select, insert on fatura.ocr_calismalari, fatura.onaylar to fatura_backend;
-- Hizmet verisi elle düzenlenir.
grant select, insert, update on fatura.hizmetler to fatura_backend;
grant select, insert, update, delete on fatura.hizmet_yillik_bedeller,
                                       fatura.hizmet_yillik_odemeler,
                                       fatura.hizmet_takma_adlari to fatura_backend;
grant select, insert, update on fatura.referans_aktarimi to fatura_backend;
grant select, insert on fatura.referans_fatura_numaralari to fatura_backend;
grant select on fatura.hizmet_zam_oranlari, fatura.hizmet_toplam_artis to fatura_backend;

grant usage on all sequences in schema fatura to fatura_backend;


-- ---------------------------------------------------------------------
-- 7. RLS — tüm tablolarda açık; politikalar yalnızca fatura_backend için.
--    Politikası olmayan rol erişemez. Satır bazlı ayrım (kullanıcı) yoktur;
--    yetki sınırını GRANT'ler belirler.
-- ---------------------------------------------------------------------
alter table fatura.hizmetler                  enable row level security;
alter table fatura.hizmet_yillik_bedeller     enable row level security;
alter table fatura.hizmet_yillik_odemeler     enable row level security;
alter table fatura.hizmet_takma_adlari        enable row level security;
alter table fatura.referans_aktarimi          enable row level security;
alter table fatura.referans_fatura_numaralari enable row level security;
alter table fatura.belgeler                   enable row level security;
alter table fatura.kalemler                   enable row level security;
alter table fatura.ocr_calismalari            enable row level security;
alter table fatura.onaylar                    enable row level security;

create policy fatura_backend_tum on fatura.hizmetler                  for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.hizmet_yillik_bedeller     for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.hizmet_yillik_odemeler     for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.hizmet_takma_adlari        for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.referans_aktarimi          for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.referans_fatura_numaralari for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.belgeler                   for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.kalemler                   for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.ocr_calismalari            for all to fatura_backend using (true) with check (true);
create policy fatura_backend_tum on fatura.onaylar                    for all to fatura_backend using (true) with check (true);

-- core.firmalar: fatura backend'i okur, ekler, günceller.
create policy fatura_backend_okur      on core.firmalar for select to fatura_backend using (true);
create policy fatura_backend_ekler     on core.firmalar for insert to fatura_backend with check (true);
create policy fatura_backend_gunceller on core.firmalar for update to fatura_backend using (true) with check (true);


-- ---------------------------------------------------------------------
-- 8. Storage: fatura belgeleri (private bucket)
--    Yalnızca kaynak PDF saklanır; önizleme PNG'leri yerel önbellektir.
--    Nesne yolu: {yil}/{ay}/{belge_id}.pdf  (yıl/ay: belgenin created_at'i)
--    Boyut ve tür sınırı koddaki yükleme kuralıyla aynıdır
--    (app.MAX_UPLOAD = 25 MB, yalnızca PDF).
--    Erişim backend'den Storage API ile yapılır; storage.objects üzerinde
--    anon/authenticated için politika tanımlanmaz (erişim yok).
-- ---------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('fatura-belgeler', 'fatura-belgeler', false, 26214400, array['application/pdf'])
on conflict (id) do nothing;

commit;

-- =====================================================================
-- Migration sonrası (SQL dışında, sırlar dosyaya yazılmadan) yapılacaklar:
--
-- 1) Backend rolüne giriş izni ve parola (parola Coolify secret'ında):
--      alter role fatura_backend with login password '<FATURA_DB_PASSWORD>';
--
-- 2) Backend bağlantısı (Supavisor, transaction modu; tenant: Fatura_Sistemi):
--      FATURA_DATABASE_URL=postgresql://fatura_backend.Fatura_Sistemi:<FATURA_DB_PASSWORD>@<SUPAVISOR_HOST>:6543/postgres
--
-- 3) PGRST_DB_SCHEMAS değişkenine fatura ve core EKLENMEZ.
--
-- 4) Storage API için backend'de SUPABASE_URL ve SUPABASE_SERVICE_ROLE_KEY
--    ortam değişkenleri tanımlanır; bu anahtar yalnızca sunucuda kalır.
-- =====================================================================
