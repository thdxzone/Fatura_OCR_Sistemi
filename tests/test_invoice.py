"""Çıkarım, eksik alan ve tutar farkı davranışlarının regresyon testleri."""
import unittest
from pathlib import Path
from decimal import Decimal
from fatura.invoice import number, parse, run
ROOT = Path(__file__).resolve().parents[1]

class InvoiceTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(number('1.234,56'), Decimal('1234.56'))
        self.assertEqual(number('0,125'), Decimal('.125'))
        with self.assertRaises(ValueError):
            number('1,234.56')

    def test_multi_page(self):
        r = run(ROOT / 'examples/multi_page.pdf')
        self.assertEqual(len(r['items']), 3)
        self.assertEqual(r['invoice_number'], 'ABC0000000000123')
        self.assertEqual(r['seller']['name'], 'Örnek Bilişim Ltd. Şti.')
        self.assertEqual(r['invoice_date'], '2026-11-14')
        self.assertEqual(r['items'][1]['description'], None)
        self.assertEqual(r['items'][2]['evidence']['page'], 2)
        self.assertFalse(r['needs_review'], r['issues'])

    def test_mismatch_preserves_read_value(self):
        r = run(ROOT / 'examples/mismatch.pdf')
        self.assertTrue(r['needs_review'])
        self.assertEqual(r['totals']['gross'], '3900.00')
        self.assertIn('amount_mismatch', [i['code'] for i in r['issues']])

    def test_scan_and_mixed(self):
        r = run(ROOT / 'examples/multi_page.pdf')
        r = parse(r['pages'] + [{'page': 3, 'method': 'ocr_required', 'lines': []}])
        self.assertEqual(len(r['items']), 3)
        self.assertIn('ocr_required', [i['code'] for i in r['issues']])

    def test_unknown_and_missing(self):
        r = parse([{'page': 1, 'method': 'ocr_required', 'lines': []}])
        self.assertIsNone(r['totals']['gross'])
        self.assertTrue(r['needs_review'])

    def test_ambiguous_basis(self):
        r = run(ROOT / 'examples/multi_page.pdf')
        for p in r['pages']:
            for line in p['lines']:
                line['text'] = line['text'].replace('| net', '| belirsiz')
        self.assertIn('ambiguous_tax_basis', [i['code'] for i in parse(r['pages'])['issues']])

if __name__ == '__main__':
    unittest.main()
