"""Gerçek veri içermeyen, iki sayfalı Türkçe test faturalarını üretir."""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).resolve().parent
pdfmetrics.registerFont(TTFont('TR', '/System/Library/Fonts/Supplemental/Arial.ttf'))
HEADER = 'Ürün | Açıklama | Miktar | Birim | Birim Fiyat | KDV | Satır Toplam | Esas'

def generate(path, mismatch=False):
    c = canvas.Canvas(str(path), pagesize=(842, 595))
    def page(lines):
        c.setFont('TR', 10)
        y = 555
        for line in lines:
            c.drawString(35, y, line)
            y -= 24
        c.showPage()
    page(['SENTETİK TEST FATURASI - GERÇEK BELGE DEĞİLDİR',
          'Satıcı: Örnek Bilişim Ltd. Şti.', 'VKN: 0000000000', 'Adres: Örnek Sokak İstanbul',
          'Fatura No: ABC0000000000123', 'Fatura Tarihi: 14.11.2026', 'Para Birimi: TRY', HEADER,
          'Danışmanlık | Teknik destek | 2 | Saat | 1.234,56 | %20 | 2.469,12 | net',
          'Kitap | | 3 | Adet | 100,00 | %10 | 300,00 | net'])
    page([HEADER, 'Bakım | Yıllık | 1 | Adet | 500,00 | %20 | 500,00 | net',
          'KDV Hariç Toplam: 3.269,12', 'KDV Toplam: 623,82',
          'KDV Dahil Toplam: ' + ('3.900,00' if mismatch else '3.892,94'),
          'Ödenecek Tutar: 3.892,94'])
    c.save()

if __name__ == '__main__':
    generate(ROOT / 'multi_page.pdf')
    generate(ROOT / 'mismatch.pdf', True)
