"""Her sayfasında ayrı fatura bulunan PDF'leri görüntüyü bozmadan ayırır."""

from contextlib import closing
from pathlib import Path
import threading

import pypdfium2 as pdfium

from fatura.extraction.page_filter import is_blank_image, is_invoice_text

# PDFium aynı süreçte eşzamanlı çağrılmamalı; OCR zaten ayrı süreçte çalışır.
PDF_LOCK = threading.Lock()
MAX_BATCH_PAGES = 30
MAX_SPLIT_BYTES = 100 * 1024 * 1024


def inspect_pdf(data):
    """Kayıt veya OCR işi oluşturmadan sayfa sayısını doğrular."""
    if not data or len(data) > 25 * 1024 * 1024:
        raise ValueError("Dosya en fazla 25 MB olabilir.")
    if not data.startswith(b"%PDF-"):
        raise ValueError("Lütfen PDF biçiminde bir fatura yükleyin.")
    with PDF_LOCK:
        try:
            with pdfium.PdfDocument(data) as document:
                count = len(document)
                if not 1 <= count <= MAX_BATCH_PAGES:
                    raise ValueError("En fazla 30 sayfalık PDF yükleyebilirsiniz.")
                return count
        except pdfium.PdfiumError as error:
            raise ValueError(
                "PDF açılamadı. Şifreli veya bozuk dosyayı kontrol edin."
            ) from error


def is_skippable(page):
    """Kayıt açmaya değmeyen sayfa: metin katmanı fatura belirtisi taşımıyor ya da
    taranmış sayfa boş. İmzalı taramalar OCR sonrası ayrıca elenir."""
    with closing(page.get_textpage()) as textpage:
        text = textpage.get_text_range()
    if text.strip():
        return not is_invoice_text(text)
    bitmap = page.render(scale=0.5, grayscale=True)
    try:
        return is_blank_image(bitmap.to_pil())
    finally:
        bitmap.close()


def split_pages(data, destination):
    """Fatura içerebilecek sayfalar (sayfa no, dosya) olarak hazırlanır; boş sayfa
    için kayıt açılmaz. Bozuk PDF'de kısmi fatura kaydı oluşturulmaz."""
    destination = Path(destination)
    with PDF_LOCK:
        try:
            with pdfium.PdfDocument(data) as source:
                count = len(source)
                if not 2 <= count <= MAX_BATCH_PAGES:
                    raise ValueError("Sayfalara ayırmak için PDF 2–30 sayfa içermeli.")
                paths = []
                total_bytes = 0
                for index in range(count):
                    with closing(source[index]) as page:
                        if is_skippable(page):
                            continue
                    path = destination / f"page-{index + 1}.pdf"
                    with pdfium.PdfDocument.new() as page:
                        page.import_pages(source, [index])
                        page.save(path)
                    total_bytes += path.stat().st_size
                    if total_bytes > MAX_SPLIT_BYTES:
                        raise ValueError(
                            "Ayrılan sayfalar toplamda en fazla 100 MB olabilir."
                        )
                    paths.append((index + 1, path))
                if not paths:
                    raise ValueError("PDF'te fatura içeren sayfa bulunamadı; tüm sayfalar boş.")
                return paths
        except pdfium.PdfiumError as error:
            raise ValueError(
                "PDF açılamadı. Şifreli veya bozuk dosyayı kontrol edin."
            ) from error
