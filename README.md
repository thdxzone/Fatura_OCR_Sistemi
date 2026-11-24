# Fatura Yönetim Sistemi

PDF faturaları yerel PaddleOCR ile okuyan, kullanıcı kontrolü sonrası kaydeden ve hizmet bazında ödeme analizi sunan uygulama.

## Başlatma

Python 3.12 ve Poppler gerekir. İlk kurulum:

```sh
python3 kurulum/setup_local.py
python3 kurulum/start_local.py --open
```

Windows'ta `kurulum/Kurulum.bat` ve `kurulum/Baslat.bat`; macOS'ta `kurulum/Kurulum.command` ve `kurulum/Baslat.command` kullanılabilir. Yerel arayüz: http://127.0.0.1:8765.

Ubuntu ARM64 / Coolify kurulumu için [docs/COOLIFY.md](docs/COOLIFY.md), bilgisayarlar arası aktarım için [docs/TASIMA.md](docs/TASIMA.md), ekranların kullanımı için [docs/KULLANIM.md](docs/KULLANIM.md).

## Özellikler

- PDF yükleme, belge önizleme ve düzenlenebilir fatura/kalem alanları.
- Her sayfası ayrı fatura olan PDF'leri sayfa sırasıyla okuma ve ayrı onaylama.
- Fatura numarasıyla arama, mükerrer kayıt engelleme ve silme.
- Kurum ve hizmet eşleştirme, hizmet türü, başlangıç ve bitiş tarihleri.
- Yılın son hizmet bedeli üzerinden zam analizi ve fiyat grafiği.
- Yıllık ödeme toplamı ve fatura sayısı; geçmiş yıllar için elle giriş ve bedel × adet hesabı.
- Yeni faturalarla güncellenen yıllık ödeme grafiği ve mobil arayüz.

## Dosya düzeni

| Konum | Görevi |
| --- | --- |
| `fatura/web/app.py` | Yerel HTTP sunucusu ve kayıt akışı (`python -m fatura.web.app`) |
| `fatura/web/production.py`, `Dockerfile` | Kimlik doğrulamalı Coolify sunucusu (`python -m fatura.web.production`) |
| `fatura/web/static/` | Arayüz, CSS, JavaScript ve görseller |
| `fatura/ocr/` | PaddleOCR/Tesseract motorları, görüntü ön işleme, ayrı süreçte çalışan OCR worker'ı (`worker.py`, `runtime.py`) |
| `fatura/extraction/` | PDF/OCR verisini ortak fatura şemasına dönüştüren ayrıştırıcılar, PDF sayfa bölme (`pdf_batch.py`) |
| `fatura/analysis/` | Hizmet eşleştirme, zam ve ödeme hesapları |
| `fatura/invoice.py`, `fatura/storage.py` | Fatura modeli ve SQLite deposu |
| `fatura/paths.py` | Proje klasörlerinin tek tanımı (veri, model, statik dosyalar) |
| `models/paddlex/official_models/` | Türkçe PaddleOCR mobil ağırlıkları |
| `models/tessdata/` | Yardımcı Tesseract ağırlıkları |
| `data/` | Ana veritabanı, gerçek belgeler ve Excel başlangıç verileri. Git'te tutulmaz; yedek ZIP ile taşınır |
| `supabase/`, `fatura/supabase/` | Supabase migration'ları ve Postgres/Storage arka ucu ([docs/veritabani.md](docs/veritabani.md)) |
| `kurulum/` | Kurulum ve başlatma dosyaları, Python bağımlılıkları (`requirements*.txt`), Coolify ortam değişkeni örneği |
| `scripts/` | OCR kontrolü, veri aktarımı ve yedekleme |
| `docs/` | Kullanım, Coolify kurulumu ve taşıma belgeleri |
| `tests/`, `examples/`, `profiles/` | Doğrulamalar, örnek belgeler ve tarama profilleri |

Eski deney (lab) arayüzü, kodu ve modelleri projeden kaldırıldı.

## Doğrulama

Kurulu OCR ortamının Python'u ile:

```sh
python -m unittest discover -s tests -q
python -m scripts.ocr_smoke
```

Yerel başlatma yalnızca loopback adresini dinler. Sunucu dağıtımında kalıcı veri diski ve yönetici parolası için Coolify belgesini izleyin. OCR sonucu otomatik doğruluk garantisi taşımaz; fatura kullanıcı kontrolünden sonra onaylanır.
