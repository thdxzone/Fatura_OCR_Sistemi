# Veri taşıma planı: SQLite + JSON → Supabase

Taşıma betiği: [`fatura/supabase/migrate.py`](../fatura/supabase/migrate.py). Şema için bkz. [veritabani.md](veritabani.md).

Betik yerel bir PostgreSQL 16 üzerinde, yerel verinin kopyasıyla sınandı. Gerçek Supabase'e henüz hiçbir şey uygulanmadı.

## Kullanım

Uygulama durdurulmuşken, proje kökünden çalıştırılır:

```sh
# 1. Yalnızca ön kontrol. Veritabanına bağlanmaz.
python -m fatura.supabase.migrate --data-dir data

# 2. Prova: her şeyi yazar, doğrular ve GERİ ALIR.
python -m fatura.supabase.migrate --data-dir data --prova

# 3. Gerçek taşıma: yazar, doğrular, PDF'leri yükleyip SHA-256 ile karşılaştırır, sonra commit eder.
python -m fatura.supabase.migrate --data-dir data --uygula
```

| Seçenek | Anlamı |
| --- | --- |
| `--cakismalar dur` (varsayılan) | Aynı fatura numaralı aktif kayıtlar varsa taşıma durur. |
| `--cakismalar mukerrer` | Bu kayıtlardan onaylı olan korunur. Diğerleri `duplicate` işaretlenir ve `mukerrer_belge_id` onaylı kaydı gösterir. Onaylı kayıt yoksa en eski kayıt korunur; uygulamanın yeni yüklemelerde uyguladığı kural da budur. |
| `--haric <id>,<id>` | Belirtilen belgeler taşınmaz. Yalnızca kullanıcı kararıyla kullanılır. |

**Ortam değişkenleri:**
- `FATURA_DATABASE_URL`: `fatura_backend` rolü; betik uygulamanın yetkileriyle çalışır.
- `SUPABASE_URL` ve `SUPABASE_SERVICE_ROLE_KEY`: yalnızca `--uygula` için.

Kaynak veri salt okunur açılır; betik `data/` klasörüne hiçbir şey yazmaz.

## Yerel veride ön kontrol sonucu (23.11.2026)

Canlı ortamın `/data` diskindeki sayılar farklı olabilir; taşıma günü ön kontrol yeniden çalıştırılmalı.

| Kaynak | Sayı | Hedef |
| --- | --- | --- |
| Belgeler (7 onaylı, 5 incelemede, 4 mükerrer, 8 silinmiş) | 24 | `fatura.belgeler` |
| OCR çalıştırmaları | 29 | `fatura.ocr_calismalari` |
| Onaylar | 7 | `fatura.onaylar` |
| Hizmetler (Excel 20, elle 0; 2'sinde düzeltme var) | 20 | `fatura.hizmetler` ve alt tabloları |
| Başlangıç fatura numaraları | 143 | `fatura.referans_fatura_numaralari` |

**Görsel kayıtlar: 4 kayıt** (Karar 23.11.2026: taşıma dışında bırakılacak)

| Belge | Durum | Tür |
| --- | --- | --- |
| `67d354f77e2644bd87b128206a013e5d` | review | JPEG |
| `bcd61b1c258c4d01a1d55374d206d46b` | review | PNG; onaylı `23cad0ca…` ile aynı fatura numarası |
| `d8a5c5869bd445b497debc5451383016` | review | PNG; onaylı `6863cdee…` ile aynı fatura numarası |
| `10601b1e85274be09dfe216ba10150f7` | review | PNG; onaylı `ba47902f…` ile aynı fatura numarası |

- **Kaynak:** Bu kayıtlar kaldırılan laboratuvar aracından kalma. Uygulama yeni yüklemelerde yalnızca PDF kabul ediyor ([app.py:181-185](../fatura/web/app.py#L181-L185)) ve bucket da yalnızca PDF'e izin veriyor.
- **Önizleme:** Görsel kayıtların önizlemesi, laboratuvarın yerel önbelleğe koyduğu `page-1.png` dosyasına dayanıyor. Bu dosya Supabase modunda yeniden üretilemez.
- **Çakışma bağlantısı:** Daha önce bulunan 3 numara çakışmasının üçü de bu PNG kayıtlarından kaynaklanıyor.
- **Karar:** Dört kayıt `--haric` ile taşıma dışında bırakılır; kayıtlar SQLite yedeğinde kalır. Böylece numara çakışması da kalmaz ve `--cakismalar` varsayılan değerinde (`dur`) bırakılabilir.

  ```sh
  python -m fatura.supabase.migrate --data-dir data --prova \
    --haric 67d354f77e2644bd87b128206a013e5d,bcd61b1c258c4d01a1d55374d206d46b,d8a5c5869bd445b497debc5451383016,10601b1e85274be09dfe216ba10150f7
  ```

**Otomatik çözülen bulgu:** Silinmiş `9a2dc40b…` belgesinin OCR çalıştırması yok ama ilk okuması (`original_json`) var. İlk okuma, OCR çalıştırması olarak taşınır ve veri kaybolmaz.

**Prova sonucu:** 4 görsel kayıt hariç tutuldu.
- Yazılan satırlar: 20 belge, 36 kalem, 26 OCR çalıştırması, 7 onay, 20 hizmet, 3 takma ad, 1 firma kartı.
- Doğrulama sorunsuz; 20 PDF'in SHA-256 değeri tuttu.
- Bilgi notu: 3 hizmetin Excel'deki zam oranı, bedellerden yeniden hesaplanan orandan farklı. Bedeller aynı; uygulama oranları zaten bedellerden hesaplıyor.

## Ön kontroller

Betik bunları her çalıştırmada yapar. Rapor yalnızca **sayı ve kayıt kimliği** gösterir; fatura içeriği, satıcı adı veya VKN yazılmaz. Veritabanı hatalarında yalnızca SQLSTATE kodu ve kısıt adı gösterilir, çünkü DETAIL satırı değer içerebilir.

1. `service-tracking.json` içinde `aliases` var mı? Yoksa uygulama yeni sürümle bir kez başlatılmalı.
2. Okuma süren (`processing`) kayıt var mı? Varsa uygulama durdurulmamış olabilir.
3. Her belgenin PDF'i var mı, `%PDF-` ile başlıyor mu, 25 MB'ı aşıyor mu?
4. Okunmuş kaydın OCR çalıştırması var mı? `original_json` en eski çalıştırmayla aynı mı?
5. Onaylı kaydın onay kopyası var mı?
6. **Hassasiyet ve biçim:** Kolonlara sığmayan tutar, tarih ve para birimi var mı? Uygulamanın kayıt kuralı kullanılır ([columns.py](../fatura/supabase/columns.py)); değerler yuvarlanmaz, rapor edilir.
7. Mükerrer kayıt var olan ve mükerrer olmayan bir kayda mı işaret ediyor?
8. Aynı fatura numaralı aktif kayıt var mı? Varsa `belgeler_fatura_no_tekil` index'i ihlal edilir; çözüm için `--cakismalar` seçeneğine bakın.
9. **Veritabanı kısıtları:** Onay zorunlulukları, kalem aralıkları ve hizmet hassasiyeti prova sırasında kontrol edilir. Her belge kendi savepoint'inde yazılır; böylece bütün sorunlar tek seferde listelenir.

## Dönüşüm kuralları

Yazma işlemi uygulamanın kendi yardımcılarını kullanır (`PgStore._header`, `_replace_items`, `PgServices.write_service`). Bu sayede taşınan veri, uygulamanın yazacağı veriyle aynı kurallardan geçer.

| Kaynak | Hedef | Kural |
| --- | --- | --- |
| `documents.id` (32 hex) | `belgeler.id` (`uuid`) | Aynı değer. Kimlikler ve eski bağlantılar değişmez. |
| Zaman damgaları, `revision` | aynı kolonlar | Olduğu gibi aktarılır. Belgeye UPDATE yapılmadığı için tetikleyici `updated_at`'i ezmez. |
| `analyses` | `ocr_calismalari` | `ham_cikti = data_json`, `motor = reader`, `sure_saniye = metrics.elapsed_seconds`. SQLite çalıştırma bazında hata tutmadığı için `hata_mesaji` boş kalır; belgenin hatası `belgeler.hata_mesaji`'ndadır. |
| — | `belgeler.kaynak_ocr_id` | `edited_json` son çalıştırmayla aynıysa son çalıştırma, değilse (kullanıcı kaydetmişse) ilk çalıştırma. Uygulamanın kuralıyla aynı. |
| `edited_json` | `belgeler` + `kalemler` | `Columns(strict=True)`: Kolona sığmayan değer taşımayı durdurur. |
| `approvals` | `onaylar` | `kayit = data_json`, `kontroller = checks_json` |
| PDF | Bucket'ta `{yıl}/{ay}/{uuid}.pdf` | Yıl ve ay belgenin `created_at`'inden alınır. |
| Hizmet satırı + `overrides` + `aliases` | `hizmetler` ve alt tabloları | `Services.rows()` birleştirmesi kullanılır. `eski_kod = id`; `source_row` varsa `kaynak = excel`, yoksa `elle`. |
| Satıcı VKN'si | `core.firmalar` | VKN başına unvan ve adres, en son onaylanan kayıttan; yoksa en son kayıttan alınır. Kart başka bir projeden gelmişse **değiştirilmez**. |
| Başlangıç fatura numaraları | `referans_fatura_numaralari` | `fatura_no_anahtar = words(no)`, uygulamanın eşleştirme anahtarıyla aynı. |

## Sıra

Tamamı tek transaction içinde yürür.

1. Hedef tablolar boş mu? (`belgeler`, `hizmetler`, `ocr_calismalari`, `onaylar`, `referans_fatura_numaralari`). Değilse betik durur.
   - `referans_aktarimi` satırının olması sorun değildir; uygulama ilk açılışta bu satırı oluşturur ve betik üzerine yazar.
2. Referans bilgisi, hizmetler, firma kartları.
3. Belgeler, OCR çalıştırmaları, kalemler ve onaylar.
   - Mükerrer kayıtlar, işaret ettikleri kayıttan sonra yazılır.
   - `ocr_calismalari.belge_id` FK'si transaction sonuna ertelenir.
4. **Doğrulama:** Her belge, uygulamanın okuma koduyla (`PgStore.get`) geri okunur ve kaynakla alan alan karşılaştırılır. Ayrıca şunlar kontrol edilir:
   - hizmet satırları;
   - başlangıç numaraları;
   - takip başlangıcı;
   - tablo satır sayıları;
   - onaylı faturaların para birimi bazında brüt toplamı.

   Tutmazsa rollback yapılır.
5. Prova modunda burada rollback yapılır.
6. **PDF yükleme commit'ten önce yapılır:** Var olan nesnenin üzerine yazılmaz ve her nesne indirilip SHA-256 ile karşılaştırılır.
   - Yükleme yarıda kalırsa veritabanına hiçbir şey yazılmamış olur.
   - Betik yeniden çalıştırıldığında önceden yüklenen nesneler doğrulanıp kullanılır; içeriği farklı olan nesne taşımayı durdurur.
7. Commit.

## Geçiş günü

1. Uygulama durdurulur. `InstanceLock` tek süreç çalışmasını zaten sağlıyor; yeni yükleme olmadığından emin olunur.
2. `scripts/backup_data.py` ile `/data` diski ZIP olarak yedeklenir. Yedek, veri diskinin dışında saklanır.
3. Ön kontrol çalıştırılır. Çıkan kararlar verilir (görsel kayıtlar, çakışmalar).
4. Önce **test ortamında** (aynı migration'larla kurulmuş boş bir Supabase şeması) `--prova`, ardından `--uygula` çalıştırılır.
5. Canlıda `--prova` ve ardından `--uygula` çalıştırılır.
6. Postgres ve Storage ile çalışan uygulama sürümü, `FATURA_DATABASE_URL` tanımlanarak devreye alınır.
7. Eski SQLite dosyası ve `/data` diski en az 30 gün salt okunur tutulur.

## Geri dönüş

- **Taşıma transaction'ı başarısız olursa** veritabanına hiçbir şey yazılmamış olur. Yüklenmiş PDF'ler bucket'ta kalır; tekrar çalıştırmada doğrulanıp kullanılır.
- **Geçişten sonra sorun çıkarsa:**
  1. Eski sürüme dönülür (`FATURA_DATABASE_URL` kaldırılır). SQLite verisi korunduğu için kayıp olmaz.
  2. Geçişten sonra Supabase'e yazılmış yeni kayıtlar elle aktarılmalıdır. Bu yüzden geri dönüş penceresi kısa tutulmalı, örneğin ilk iş günü.
- **Şemanın tamamen kaldırılması gerekirse:** `supabase/rollback/` altındaki betikler sırayla, önce `fatura` sonra `core` olmak üzere çalıştırılır. Bucket Storage API'den boşaltılır.

## Güvenlik ve KVKK

- Betik bağlantı bilgisini yalnızca ortam değişkenlerinden okur. Hiçbir sır dosyaya veya loga yazılmaz.
- Çıktı yalnızca sayı ve kayıt kimliği içerir.
- Testler sentetik veri kullanır.
- `data/` klasörü 23.11.2026'da Git takibinden çıkarıldı. Eski commit'lerde hâlâ duruyor; geçmişin temizlenmesi veya reponun private yapılması ayrıca planlanmalı.
