"""Sütun aidiyeti, tekrar eden başlık ve belirsizlik davranışlarını doğrular."""
import unittest
from pathlib import Path
from fatura.extraction.layout import adapt_pages, load_template
from fatura.invoice import parse

ROOT = Path(__file__).resolve().parents[1]


def line(text, y=10, words=None):
    return {'text': text, 'page': 1, 'bbox': [20, y, 590, y + 10], 'words': words or []}


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.template = load_template(ROOT / 'examples/layout_template.json')

    def pages(self, marker=True):
        values = [('Bakım', 20), ('Yıllık', 160), ('2', 270), ('Adet', 320),
                  ('100,00', 370), ('%20', 460), ('200,00', 510)]
        words = [{'text': text, 'x0': x, 'x1': x + 30} for text, x in values]
        lines = [line('Mal Hizmet Miktar Birim Fiyat KDV Tutar'),
                 line('Bakım Yıllık 2 Adet 100,00 %20 200,00', 30, words),
                 line('Mal Hizmet Miktar Birim Fiyat KDV Tutar', 50),
                 line('Bakım Yıllık 2 Adet 100,00 %20 200,00', 70, words),
                 line('KDV Hariç Toplam: 400,00', 90)]
        if marker:
            lines.insert(0, line('Birim fiyat ve tutarlar KDV hariçtir'))
        return [{'page': 1, 'method': 'pdf_text', 'lines': lines}]

    def test_columns_and_original_preserved(self):
        pages = self.pages()
        adapted, issues = adapt_pages(pages, self.template)
        result = parse(adapted)
        self.assertFalse(issues)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['items'][0]['name'], 'Bakım')
        self.assertEqual(result['items'][0]['quantity'], '2')
        self.assertEqual(result['items'][0]['price_basis'], 'net')
        self.assertNotIn('|', pages[0]['lines'][2]['text'])

    def test_no_basis_guess(self):
        adapted, _ = adapt_pages(self.pages(False), self.template)
        result = parse(adapted)
        self.assertEqual(result['items'][0]['price_basis'], 'unknown')
        self.assertIn('ambiguous_tax_basis', [x['code'] for x in result['issues']])

    def test_overflow_flagged(self):
        pages = self.pages()
        pages[0]['lines'][2]['words'][0]['x1'] = 200
        _, issues = adapt_pages(pages, self.template)
        self.assertIn('layout_overflow', [x['code'] for x in issues])

    def test_description_only_is_not_silently_merged(self):
        pages = self.pages()
        pages[0]['lines'].insert(3, line('Ek açıklama', 40, [{'text': 'Ek açıklama', 'x0': 160, 'x1': 240}]))
        adapted, _ = adapt_pages(pages, self.template)
        result = parse(adapted)
        self.assertEqual(len(result['items']), 2)
        self.assertIn('invalid_row', [x['code'] for x in result['issues']])
