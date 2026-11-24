"""OCR sonuçlarında eksik veri, ham kanıt ve satır bulma davranışını sınar."""

import unittest
from copy import deepcopy
from fatura.invoice import run, validate
from fatura.extraction.scan import build_regions, parse_regions


class ScanTests(unittest.TestCase):
    def test_grouping_comma_repair_applies_to_money_not_quantity(self):
        keys = [
            "item.0." + field
            for field in ("name", "quantity", "unit_price", "vat_rate", "line_total")
        ]
        regions = {key: [0.1, 0.1, 0.9, 0.9] for key in keys}
        values = {
            "item.0.name": "Hizmet",
            "item.0.quantity": "1,778,10",
            "item.0.unit_price": "1,778,10 TL",
            "item.0.vat_rate": "20",
            "item.0.line_total": "1.778,10 TL",
        }
        crops = {
            "regions": {
                key: [{"text": value, "bbox": [0, 0, 10, 10]}]
                for key, value in values.items()
            }
        }
        result = parse_regions(
            {"width": 100, "height": 100, "lines": []}, regions, crops, None
        )
        self.assertEqual(result["items"][0]["unit_price"], "1778.10")
        self.assertIsNone(result["items"][0]["quantity"])
        self.assertEqual(
            result["evidence"]["item.0.unit_price"][0]["text"], "1,778,10 TL"
        )
        self.assertTrue(
            any(i["code"] == "ocr_numeric_separator" for i in result["issues"])
        )

    def test_rows_are_not_limited_to_two(self):
        profile = {
            "fields": {},
            "table": {
                "top": 0.2,
                "bottom": 0.9,
                "row_number": [0.01, 0.1],
                "columns": {"name": [0.2, 0.5]},
            },
        }
        ocr = {
            "width": 100,
            "height": 100,
            "lines": [
                {"text": str(i + 1), "bbox": [2, 25 + i * 10, 6, 30 + i * 10]}
                for i in range(5)
            ],
        }
        regions, anchors = build_regions(ocr, profile)
        self.assertEqual(len(anchors), 5)
        self.assertEqual(len(regions), 5)

    def test_empty_cell_is_null_and_separator_repair_is_visible(self):
        keys = [
            "item.0." + f
            for f in (
                "quantity",
                "unit_price",
                "vat_rate",
                "line_total",
                "name",
                "description",
            )
        ]
        regions = {k: [0.1, 0.2, 0.3, 0.4] for k in keys}
        crops = {"regions": {k: [] for k in keys}}
        for key, text in {
            "item.0.unit_price": "1.600.00 TL",
            "item.0.vat_rate": "%20,00",
            "item.0.line_total": "1.600,00 TL",
            "item.0.name": "Bakım",
        }.items():
            crops["regions"][key] = [{"text": text, "bbox": [0, 0, 10, 10]}]
        result = parse_regions(
            {"width": 100, "height": 100, "languages": ["en-US"], "lines": []},
            regions,
            crops,
            "test.pdf",
        )
        self.assertIsNone(result["items"][0]["quantity"])
        self.assertEqual(result["items"][0]["unit_price"], "1600.00")
        self.assertEqual(
            result["evidence"]["item.0.unit_price"][0]["text"], "1.600.00 TL"
        )
        self.assertIn("ocr_numeric_separator", [i["code"] for i in result["issues"]])
        self.assertTrue(result["needs_review"])

    def test_validator_accepts_missing_ocr_quantity(self):
        from pathlib import Path

        result = run(Path(__file__).resolve().parents[1] / "examples/multi_page.pdf")
        result = deepcopy(result)
        result["items"][0]["quantity"] = None
        result["checks"], result["issues"] = [], []
        result = validate(result)
        self.assertIn("missing_item_field", [i["code"] for i in result["issues"]])
        self.assertFalse(any(c["field"] == "totals.net" for c in result["checks"]))

    def test_amount_column_can_anchor_rows_without_inventing_quantities(self):
        profile = {
            "fields": {},
            "table": {
                "top": 0.2,
                "bottom": 0.8,
                "row_number": [0.01, 0.1],
                "columns": {"line_total": [0.7, 0.95], "quantity": [0.4, 0.5]},
            },
        }
        ocr = {
            "width": 100,
            "height": 100,
            "lines": [
                {"text": "100,00", "bbox": [75, 30, 90, 34]},
                {"text": "200,00", "bbox": [75, 50, 90, 54]},
            ],
        }
        regions, anchors = build_regions(ocr, profile)
        self.assertEqual([n for _, n in anchors], [None, None])
        self.assertIn("item.1.quantity", regions)

    def test_identifier_page_and_cell_disagreement_is_visible(self):
        regions = {"invoice_number": [0.1, 0.1, 0.8, 0.2]}
        crops = {
            "regions": {
                "invoice_number": [{"text": "L4S202600001", "bbox": [0, 0, 20, 10]}]
            }
        }
        ocr = {
            "width": 100,
            "height": 100,
            "engine": "tesseract",
            "lines": [{"text": "LAS202600001", "bbox": [15, 12, 70, 18]}],
        }
        result = parse_regions(ocr, regions, crops, "test.pdf")
        self.assertEqual(result["invoice_number"], "LAS202600001")
        self.assertIn("identifier_review", [i["code"] for i in result["issues"]])
        self.assertEqual(
            result["evidence"]["invoice_number"][0]["text"], "L4S202600001"
        )
