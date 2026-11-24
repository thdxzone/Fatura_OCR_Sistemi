"""Başlıklara göre alan bulmanın firma adına ve sabit konuma bağlı olmadığını sınar."""

import unittest
from fatura.extraction.service import text_record
from fatura.extraction.flexible_layout import dynamic_regions


def sample(dx=0, dy=0):
    values = []

    def word(text, x, y, width=45):
        values.append(
            {"text": text, "bbox": [x + dx, y + dy, x + dx + width, y + dy + 14]}
        )

    word("Örnek Teknoloji A.Ş.", 50, 100, 210)
    word("SAYIN", 50, 200)
    word("Alıcı Hizmet A.Ş.", 50, 225, 200)
    word("Fatura No:", 600, 280, 115)
    word("ABC2026000000012", 740, 280, 170)
    word("Fatura Tarihi:", 600, 310, 125)
    word("19-10-2026", 740, 310, 120)
    for text, x, width in [
        ("Sıra", 40, 35),
        ("Mal", 150, 25),
        ("Hizmet", 178, 55),
        ("Miktar", 350, 55),
        ("Birim", 450, 35),
        ("Fiyat", 490, 40),
        ("KDV", 600, 30),
        ("Oranı", 635, 40),
        ("KDV", 700, 30),
        ("Tutarı", 735, 40),
        ("Mal", 850, 25),
        ("Hizmet", 880, 43),
        ("Tutarı", 928, 40),
    ]:
        word(text, x, 500, width)
    for text, x, width in [
        ("1", 40, 10),
        ("Danışmanlık", 80, 160),
        ("2", 365, 10),
        ("Adet", 390, 40),
        ("500,00", 470, 65),
        ("%20,00", 610, 65),
        ("200,00", 720, 60),
        ("1.000,00", 900, 65),
    ]:
        word(text, x, 550, width)
    for label, amount, y in [
        ("Mal Hizmet Toplam Tutarı", "1.000,00", 620),
        ("Hesaplanan KDV", "200,00", 650),
        ("Vergiler Dahil Toplam Tutar", "1.200,00", 680),
    ]:
        word(label, 550, y, 290)
        word(amount, 900, y, 65)
        word("TL", 970, y, 20)
    word("Proje kapsamındaki hizmet açıklaması.", 50, 780, 450)
    return {"width": 1050, "height": 1400, "engine": "pdf_text", "lines": values}


class FlexibleLayoutTests(unittest.TestCase):
    def test_headers_survive_page_translation(self):
        original, _ = dynamic_regions(sample())
        shifted, _ = dynamic_regions(sample(20, 35))
        for key in [
            "seller.name",
            "invoice_number",
            "invoice_date",
            "totals.net",
            "item.0.quantity",
            "item.0.line_total",
        ]:
            self.assertIn(key, original)
            self.assertAlmostEqual(shifted[key][0] - original[key][0], 20 / 1050)
            self.assertAlmostEqual(shifted[key][1] - original[key][1], 35 / 1400)

    def test_text_pdf_uses_words_without_ocr(self):
        ocr = sample()
        page = {
            "page": 1,
            "width": 1050,
            "height": 1400,
            "method": "pdf_text",
            "lines": [
                {
                    "text": w["text"],
                    "bbox": w["bbox"],
                    "page": 1,
                    "words": [
                        {
                            "text": w["text"],
                            "x0": w["bbox"][0],
                            "top": w["bbox"][1],
                            "x1": w["bbox"][2],
                            "bottom": w["bbox"][3],
                        }
                    ],
                }
                for w in ocr["lines"]
            ],
        }
        result = text_record(page)
        self.assertEqual(result["seller"]["name"], "Örnek Teknoloji A.Ş.")
        self.assertEqual(result["invoice_number"], "ABC2026000000012")
        self.assertEqual(result["invoice_date"], "2026-10-19")
        self.assertEqual(result["items"][0]["quantity"], "2")
        self.assertEqual(result["items"][0]["unit"], "Adet")
        self.assertEqual(result["items"][0]["vat_rate"], "20.00")
        self.assertIsNone(result["items"][0]["description"])
        self.assertEqual(result["totals"]["gross"], "1200.00")
        self.assertEqual(result["currency"], "TRY")

    def test_unknown_headers_do_not_invent_products(self):
        regions, _ = dynamic_regions({"width": 1000, "height": 1400, "lines": []})
        self.assertFalse(any(key.startswith("item.") for key in regions))

    def test_stacked_headers_and_label_value_on_different_baselines(self):
        ocr = sample()
        for word in ocr["lines"]:
            if word["text"] == "Oranı":
                word["bbox"] = [597, 508, 640, 522]
            elif word["text"] == "Tutarı" and word["bbox"][0] == 928:
                word["bbox"] = [865, 508, 920, 522]
            elif word["text"] == "ABC2026000000012":
                word["bbox"][1] -= 9
                word["bbox"][3] -= 9
        regions, _ = dynamic_regions(ocr)
        self.assertIn("item.0.vat_rate", regions)
        self.assertIn("item.0.line_total", regions)
        self.assertIn("invoice_number", regions)
        self.assertLess(regions["item.0.vat_rate"][2], regions["item.0.line_total"][0])

    def test_delivery_number_cannot_replace_invoice_number(self):
        ocr = sample()
        for word in ocr["lines"]:
            if word["text"] == "Fatura No:":
                word["text"] = "Etiket silinmiş"
        ocr["lines"].extend(
            [
                {"text": "İrsaliye No:", "bbox": [600, 335, 720, 349]},
                {"text": "IRS2026000000099", "bbox": [740, 335, 920, 349]},
            ]
        )
        regions, _ = dynamic_regions(ocr)
        number = regions["invoice_number"]
        self.assertLess(number[3] * ocr["height"], 310)
        self.assertAlmostEqual(number[1] * ocr["height"], 277)
        # Gerçek fatura numarası yoksa irsaliye yerine boş bırakılmalı.
        ocr["lines"] = [w for w in ocr["lines"] if w["text"] != "ABC2026000000012"]
        regions, _ = dynamic_regions(ocr)
        self.assertNotIn("invoice_number", regions)

    def test_foreign_invoice_totals_are_not_overwritten_by_local_equivalents(self):
        ocr = sample()
        for word in ocr["lines"]:
            if word["text"] == "TL":
                word["text"] = "USD"
        for label, value, top in [
            ("Mal Hizmet Toplam Tutarı(TL)", "50.000,00", 900),
            ("Vergiler Dahil Toplam Tutar(TL)", "60.000,00", 930),
        ]:
            ocr["lines"].extend(
                [
                    {"text": label, "bbox": [550, top, 840, top + 14]},
                    {"text": value, "bbox": [900, top, 965, top + 14]},
                    {"text": "TL", "bbox": [970, top, 990, top + 14]},
                ]
            )
        regions, _ = dynamic_regions(ocr)
        self.assertLess(regions["totals.net"][1] * 1400, 900)
        self.assertLess(regions["totals.gross"][1] * 1400, 900)

    def test_fused_column_headings_are_recognized(self):
        ocr = sample()
        ocr["lines"] = [word for word in ocr["lines"] if word["text"] != "Fiyat"]
        for word in ocr["lines"]:
            if word["text"] == "Birim":
                word["text"] = "BirimFiyat"
                word["bbox"][2] = 530
        regions, _ = dynamic_regions(ocr)
        self.assertIn("item.0.unit_price", regions)
        self.assertIn("item.0.line_total", regions)


class SummaryBoundaryTests(unittest.TestCase):
    def test_long_quantity_left_of_header_is_not_cut_or_added_to_product_name(self):
        for quantity in ("200.000", "12.345.678", "0"):
            with self.subTest(quantity=quantity):
                ocr = sample()
                for word in ocr["lines"]:
                    if word["text"] == "2" and word["bbox"][0] == 365:
                        word["text"] = quantity
                        word["bbox"] = [325, 550, 385, 564]
                regions, _ = dynamic_regions(ocr)
                bounds = regions["item.0.quantity"]
                self.assertLessEqual(bounds[0] * ocr["width"], 325)
                self.assertGreaterEqual(bounds[2] * ocr["width"], 385)
                self.assertLessEqual(regions["item.0.name"][2], bounds[0])

    def test_withholding_is_read_from_label_not_calculated_from_balance(self):
        ocr = sample()
        ocr["lines"].extend(
            [
                {"text": "KDV Tevkifat Tutarı (603)", "bbox": [550, 710, 840, 724]},
                {"text": "140,00", "bbox": [900, 710, 965, 724]},
            ]
        )
        regions, _ = dynamic_regions(ocr)
        self.assertIn("totals.withholding", regions)
        ocr["lines"] = [w for w in ocr["lines"] if w["bbox"][1] != 710]
        regions, _ = dynamic_regions(ocr)
        self.assertNotIn("totals.withholding", regions)

    def test_first_row_overlapping_stacked_header_is_not_merged_with_second(self):
        ocr = sample()
        for word in ocr["lines"]:
            if word["bbox"][1] >= 620:
                word["bbox"][1] += 100
                word["bbox"][3] += 100
            elif word["bbox"][1] == 550:
                word["bbox"][1] = 525
                word["bbox"][3] = 539
                if word["text"] == "1.000,00":
                    word["bbox"][1] = 523
                    word["bbox"][3] = 537
        # İki satırlı başlığın altı 524; ilk tutarın üstü 523, merkezi 530.
        ocr["lines"].append({"text": "No", "bbox": [40, 510, 65, 524]})
        for index, y in enumerate((565, 605, 645), 2):
            ocr["lines"].extend(
                [
                    {"text": f"Hizmet {index}", "bbox": [80, y, 240, y + 14]},
                    {"text": str(index), "bbox": [365, y, 375, y + 14]},
                    {"text": "Adet", "bbox": [390, y, 430, y + 14]},
                    {"text": "500,00", "bbox": [470, y, 535, y + 14]},
                    {"text": "%20,00", "bbox": [610, y, 675, y + 14]},
                    {"text": "1.000,00", "bbox": [900, y, 965, y + 14]},
                ]
            )
        regions, _ = dynamic_regions(ocr)
        self.assertEqual(
            {k.split(".")[1] for k in regions if k.startswith("item.")},
            {"0", "1", "2", "3"},
        )
        for i, center in enumerate((532, 572, 612, 652)):
            bounds = regions[f"item.{i}.quantity"]
            self.assertLess(bounds[1] * ocr["height"], center)
            self.assertGreater(bounds[3] * ocr["height"], center)
        self.assertLess(regions["item.0.name"][3] * ocr["height"], 565)

    def test_summary_aliases_do_not_create_service_rows(self):
        for label in ("Mal ve Hizmet Toplam Tutarı", "Vergi Hariç Tutar"):
            with self.subTest(label=label):
                ocr = sample()
                ocr["lines"] = [w for w in ocr["lines"] if w["bbox"][1] < 620]
                ocr["lines"].extend(
                    [
                        {"text": label, "bbox": [550, 620, 840, 634]},
                        {"text": "1.000,00", "bbox": [900, 620, 965, 634]},
                        {"text": "KDV Matrahı", "bbox": [550, 650, 840, 664]},
                        {"text": "1.000,00", "bbox": [900, 650, 965, 664]},
                        {"text": "Hesaplanan KDV", "bbox": [550, 680, 840, 694]},
                        {"text": "200,00", "bbox": [900, 680, 965, 694]},
                    ]
                )
                regions, _ = dynamic_regions(ocr)
                self.assertIn("totals.net", regions)
                self.assertEqual(
                    {key.split(".")[1] for key in regions if key.startswith("item.")},
                    {"0"},
                )
                self.assertLess(regions["item.0.line_total"][3] * ocr["height"], 620)

    def test_joined_total_label_and_amount_still_end_table(self):
        ocr = sample()
        ocr["lines"] = [w for w in ocr["lines"] if w["bbox"][1] < 620]
        ocr["lines"].extend(
            [
                {"text": "Mal ve Hizmet Toplam", "bbox": [550, 620, 830, 634]},
                {"text": "Tutarı|1.000,00", "bbox": [835, 620, 965, 634]},
                {"text": "KDV Matrahı", "bbox": [550, 650, 840, 664]},
                {"text": "1.000,00", "bbox": [900, 650, 965, 664]},
            ]
        )
        regions, _ = dynamic_regions(ocr)
        self.assertIn("totals.net", regions)
        self.assertNotIn("item.1.name", regions)
