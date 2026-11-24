"""Fatura içermeyen sayfaları (çift taraflı taramanın boş arka yüzü, yalnızca
imza/kaşe/tarih bulunan sayfa) ayırt eder.

Kural ihtiyatlıdır: şüphede sayfa fatura sayılır ve okunur. Bir sayfa yalnızca
fatura anahtar kelimesi, tutar biçiminde sayı ve kayda değer miktarda yazı
içermiyorsa atlanır.
"""

import re

# Kenarlardaki tarayıcı gölgesi ve delgeç izleri mürekkep sayılmaz.
MARGIN = 0.05
# Bu oranın altında koyu piksel içeren sayfa OCR'a gönderilmeden boş sayılır.
# Fatura sayfası bunun çok üstündedir; arada kalan (imza vb.) sayfalar OCR'la elenir.
BLANK_INK_RATIO = 0.003
DARK = 128
# İmza, isim, kaşe ve tarih bu sınırın altında kalır.
MAX_NON_INVOICE_WORDS = 25

# OCR Türkçe harfleri düşürebildiği için karşılaştırma ASCII'ye indirgenmiş metinde yapılır.
KEYWORDS = (
    "FATURA", "ARSIV", "ETTN", "VKN", "TCKN", "VERGI", "KDV", "MATRAH",
    "TOPLAM", "TUTAR", "ODENECEK", "HIZMET", "SENARYO", "IRSALIYE", "MIKTAR",
    "BIRIM FIYAT", "INVOICE", "TOTAL",
)
# 1.234,56 / 12,50 — tarihler (12.03.2024) eşleşmez.
AMOUNT = re.compile(r"(?<![\d,.])\d{1,3}(?:\.\d{3})*,\d{2}(?![\d,])|(?<![\d,.])\d+,\d{2}(?![\d,])")
FOLD = str.maketrans("İIıiŞşĞğÜüÖöÇç", "IIIISSGGUUOOCC")


def fold(text):
    return (text or "").translate(FOLD).upper()


def is_invoice_text(text):
    """Metinde fatura belirtisi var mı; belirsizlikte True döner."""
    if len(text.split()) >= MAX_NON_INVOICE_WORDS:
        return True
    if AMOUNT.search(text):
        return True
    folded = fold(text)
    return any(keyword in folded for keyword in KEYWORDS)


def ocr_text(ocr):
    return " ".join(line.get("text") or "" for line in ocr.get("lines", []))


def page_text(page):
    """extract() çıktısındaki metin katmanlı sayfanın düz metni."""
    return " ".join(line.get("text") or "" for line in page.get("lines", []))


def is_blank_image(image):
    """Görüntü (yol veya PIL) neredeyse hiç mürekkep içermiyorsa True."""
    from PIL import Image

    source = Image.open(image) if not isinstance(image, Image.Image) else image
    try:
        gray = source.convert("L")
        width, height = gray.size
        dx, dy = int(width * MARGIN), int(height * MARGIN)
        gray = gray.crop((dx, dy, width - dx, height - dy))
        gray.thumbnail((600, 600))
        histogram = gray.histogram()
        total = sum(histogram)
        return total > 0 and sum(histogram[:DARK]) / total < BLANK_INK_RATIO
    finally:
        if source is not image:
            source.close()
