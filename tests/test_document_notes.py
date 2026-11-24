"""Not başlığı ve metin bloğunun kaynak kanıtıyla çıkarılmasını doğrular."""
import unittest
from fatura.invoice import parse
from fatura.document_notes import extract_note
from fatura.extraction.service import merge_records


def page(lines):
    return {'page': 1, 'width': 600, 'height': 800, 'method': 'pdf_text',
            'lines': [{'text': text, 'page': 1, 'bbox': [40, top, 450, top+10]} for text, top in lines]}


class NoteTests(unittest.TestCase):
    def test_multiline_note_stops_at_footer(self):
        p = page([('Fatura Notu', 500), ('Vergi istisna açıklaması', 515),
                  ('İkinci açıklama satırı', 530), ('https://ornek.test', 700)])
        record = parse([p])
        self.assertEqual(record['invoice_note'], 'Vergi istisna açıklaması\nİkinci açıklama satırı')
        self.assertEqual(len(record['evidence']['invoice_note']), 2)

    def test_missing_note_is_null(self):
        value, evidence = extract_note([page([('Başka bir metin', 500)])])
        self.assertIsNone(value)
        self.assertEqual(evidence, [])

    def test_inline_note_and_multi_page_merge(self):
        first = parse([page([('Fatura Notu: İlk açıklama', 500)])])
        second = parse([page([('Notlar: İkinci açıklama', 500)])])
        self.assertEqual(merge_records([first, second])['invoice_note'], 'İlk açıklama\nİkinci açıklama')
