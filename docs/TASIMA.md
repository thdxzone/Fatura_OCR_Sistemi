# Başka bilgisayara aktarım

## Kaynak kodunu alma

```sh
git clone https://github.com/thdxzone/Fatura_OCR_Sistemi.git
cd Fatura_OCR_Sistemi
```

ZIP indiriyorsanız bütün klasörleri birlikte çıkarın. Yerel OCR ağırlıkları `models/` içindedir. Faturalar ve veritabanı (`data/`) Git'te tutulmaz; mevcut bilgisayardan yedek ZIP ile taşınır (aşağıya bakın).

## Yerel kurulum

Python 3.12 kurun. Windows'ta `kurulum/Kurulum.bat`, macOS'ta `kurulum/Kurulum.command` veya terminalde:

```sh
python3 kurulum/setup_local.py
python3 kurulum/start_local.py --open
```

Kurulum, hedef bilgisayara uygun paketleri `.venv-ocr` içine kurar. Sanal ortamı başka bilgisayardan kopyalamayın. PDF önizleme ve OCR için Poppler kurup `pdftoppm` komutunu PATH'e ekleyin. Tesseract küçük miktar hücrelerinde isteğe bağlı yardımcıdır. macOS'ta `brew install poppler tesseract` kullanılabilir.

Arayüz http://127.0.0.1:8765 adresindedir. Ubuntu ARM64 ve Coolify kurulumu [COOLIFY.md](COOLIFY.md) içinde anlatılır. Windows'ta uçtan uca çalışma henüz doğrulanmadı; yerel macOS ve Linux ARM64 OCR ortamları test edildi.

## Güncelleme ve veri

Veriyi başka bilgisayara taşımak veya güncellemeden önce korumak için `data/` dizininin tutarlı yedeğini alın:

```sh
python -m scripts.backup_data --source data --output /yedekler/fatura.zip
```

Yeni bilgisayarda ZIP'i boş bir `data/` klasörüne çıkarın. Yedek gerçek fatura içerir; e-posta veya herkese açık bir yer üzerinden paylaşmayın. Kaynak kodunu güncellemek için `git pull` yeterlidir; kod güncellemesi `data/` klasörüne dokunmaz. Coolify'da `/data` kalıcı diski yeni imajdan bağımsızdır.

Ana sistem Türkçe PaddleOCR kullanır; kod `fatura/ocr/`, ağırlıklar `models/paddlex/official_models/` içindedir. Eski deney arayüzü ve deney kayıtları güncel repoda yer almaz.
