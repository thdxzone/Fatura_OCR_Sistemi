"""Çift taraflı taramadaki boş / imzalı arka yüzlerin atlanmasını doğrular."""

from io import BytesIO
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from reportlab.pdfgen.canvas import Canvas

from fatura.extraction.page_filter import is_blank_image, is_invoice_text
from fatura.extraction.service import NoInvoiceContent, empty_record, merge_records
from fatura.web.app import Application


def scan(ink=None):
    """Taranmış sayfa benzeri görüntü: hafif gri zemin, kenar gölgesi, isteğe bağlı mürekkep."""
    image = Image.new("L", (1240, 1754), 235)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 40, 1754], fill=60)  # tarayıcı kenar gölgesi
    if ink == "signature":
        draw.line([(800, 1500), (900, 1460), (1000, 1520), (1080, 1480)], fill=20, width=4)
    if ink == "invoice":
        for row in range(40):
            draw.rectangle([120, 150 + row * 38, 1100, 170 + row * 38], fill=30)
    return image


def pdf(pages):
    output = BytesIO()
    canvas = Canvas(output, pagesize=(300, 400))
    for text in pages:
        if text:
            canvas.drawString(30, 340, text)
        canvas.showPage()
    canvas.save()
    return output.getvalue()


class InvoiceTextTests(unittest.TestCase):
    def test_signature_and_date_page_is_not_invoice(self):
        for text in ("", "Teslim Alan  Ahmet  12.03.2024", "Kaşe / İmza  Tarih: 01.02.2025"):
            with self.subTest(text=text):
                self.assertFalse(is_invoice_text(text))

    def test_any_invoice_signal_keeps_page(self):
        for text in (
            "e-Arşiv Fatura",
            "FATURA NO ABC2024000000001",
            "Ödenecek Tutar",
            "ODENECEK TUTAR",  # OCR Türkçe harfi düşürse de
            "1.234,56",
            "12,50",
            " ".join(["kelime"] * 25),  # çok yazı: emin olunamadığında okunur
        ):
            with self.subTest(text=text):
                self.assertTrue(is_invoice_text(text))


class BlankImageTests(unittest.TestCase):
    def test_blank_scan_with_edge_shadow_is_blank(self):
        self.assertTrue(is_blank_image(scan()))

    def test_invoice_scan_is_never_blank(self):
        self.assertFalse(is_blank_image(scan("invoice")))


class MergeTests(unittest.TestCase):
    def test_skipped_pages_keep_numbering_and_are_noted(self):
        record = empty_record()
        record["invoice_number"] = "ABC1"
        merged = merge_records([record, None])
        self.assertEqual(merged["invoice_number"], "ABC1")
        self.assertTrue(any(i["code"] == "skipped_pages" and "2." in i["detail"] for i in merged["issues"]))

    def test_all_pages_skipped_raises(self):
        with self.assertRaises(NoInvoiceContent):
            merge_records([None, None])


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = Application(temporary.name)
        self.addCleanup(self.app.executor.shutdown)

    def test_split_skips_blank_back_pages_without_records(self):
        data = pdf(["Fatura PAGE-1 Toplam 100,00", None, "Fatura PAGE-3 Toplam 200,00", None])
        with patch.object(self.app.executor, "submit") as queue:
            result = self.app.accept_upload(data, "tarama.pdf", split=True)
        self.assertEqual(queue.call_count, 2)
        self.assertEqual(
            [d["filename"] for d in result["documents"]],
            ["tarama · sayfa 1.pdf", "tarama · sayfa 3.pdf"],
        )
        self.assertEqual(len(list((self.app.store.root / "files").iterdir())), 2)

    def test_split_of_only_blank_pages_is_rejected(self):
        with self.assertRaises(ValueError):
            self.app.accept_upload(pdf([None, None]), "bos.pdf", split=True)
        self.assertEqual(self.app.store.list(), [])

    def test_page_without_invoice_content_leaves_no_trace(self):
        with patch.object(self.app.executor, "submit"):
            doc = self.app.accept_upload(pdf(["Fatura"]), "imza.pdf")
        with patch("fatura.web.app.analyze_paddle", return_value={"no_invoice_content": True, "reader": "paddle"}):
            self.app.process(doc["id"])
        self.assertIsNone(self.app.store.get(doc["id"]))
        self.assertFalse((self.app.store.root / "files" / doc["id"]).exists())
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
