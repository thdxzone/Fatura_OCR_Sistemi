"""Miktar okumasında boş hücre, çok basamak ve motor anlaşmazlığını sınar."""
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from fatura.ocr.backends import prepare_quantity, TesseractBackend, VisionBackend

ROOT = Path(__file__).resolve().parents[1]


class BackendTests(unittest.TestCase):
    def test_blank_quantity(self):
        self.assertIsNone(prepare_quantity(Image.new('RGB', (120, 80), 'white')))

    def test_vision_rejects_other_platforms(self):
        with patch.object(sys, 'platform', 'win32'):
            with self.assertRaisesRegex(ValueError, 'macOS'):
                VisionBackend()

    def test_quantity_disagreement_is_not_accepted(self):
        backend = TesseractBackend.__new__(TesseractBackend)
        image = Image.new('RGB', (40, 40), 'white')
        ImageDraw.Draw(image).rectangle((10, 10, 15, 30), fill='black')
        attempts = [[{'text': '1', 'confidence': .9}], [{'text': '7', 'confidence': .9}]]
        with patch.object(backend, 'recognize', side_effect=attempts * 3):
            accepted, raw = backend.quantity(image)
        self.assertEqual(accepted, [])
        self.assertEqual(raw, attempts * 3)

    @unittest.skipUnless(shutil.which('tesseract') and (ROOT / 'models/tessdata/eng.traineddata').exists(),
                         'Yerel Tesseract ve İngilizce ağırlık gerekli')
    def test_real_engine_single_multi_and_decimal_quantities(self):
        backend = TesseractBackend(languages='eng', tessdata=ROOT / 'models/tessdata')
        # Pillow varsayılan fontu platforma özel dosya yolu gerektirmez.
        font = ImageFont.load_default(size=44)
        for value in ['1', '12', '1,5']:
            with self.subTest(value=value):
                image = Image.new('RGB', (200, 100), 'white')
                ImageDraw.Draw(image).text((25, 25), value, font=font, fill='black')
                accepted, attempts = backend.quantity(image)
                self.assertTrue(accepted, attempts)
                self.assertEqual(accepted[0]['text'], value)

    def test_identifier_keeps_letters_and_leading_zeros(self):
        backend = TesseractBackend.__new__(TesseractBackend)
        raw = [{'text': 'ABC00000000123', 'confidence': .95, 'bbox': [0, 0, 20, 10]}]
        with patch.object(backend, 'recognize', return_value=raw):
            accepted, _, review = backend.identifier(Image.new('RGB', (80, 20), 'white'))
        self.assertEqual(accepted[0]['text'], 'ABC00000000123')
        self.assertFalse(review)

    def test_identifier_alternative_remains_a_review_candidate(self):
        backend = TesseractBackend.__new__(TesseractBackend)
        primary = [{'text': '$I000123', 'confidence': .5, 'bbox': [0, 0, 20, 10]}]
        candidate = [{'text': 'SI000123', 'confidence': .8, 'bbox': [0, 0, 20, 10]}]
        with patch.object(backend, 'recognize', side_effect=[primary, candidate, candidate]):
            accepted, raw, review = backend.identifier(Image.new('RGB', (80, 20), 'white'))
        self.assertEqual(accepted[0]['text'], 'SI000123')
        self.assertEqual(raw[0][0]['text'], '$I000123')
        self.assertTrue(review)
