"""Onay, veri kalıcılığı ve eksik miktar davranışlarının uygulama testleri."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fatura.web.app import approval_errors, normalized_record
from fatura.invoice import run
from fatura.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def setUp(self):
        self.record = run(ROOT / 'examples/multi_page.pdf')

    def test_missing_quantity_blocks_approval(self):
        value = deepcopy(self.record)
        value['items'][1]['quantity'] = None
        normalized = normalized_record(value, self.record)
        self.assertIn('2. kalem miktar', approval_errors(normalized))

    def test_turkish_decimal_and_original_preserved(self):
        value = deepcopy(self.record)
        value['items'][0]['unit_price'] = '1.234,56'
        value['seller']['name'] = 'Düzeltilmiş satıcı'
        normalized = normalized_record(value, self.record)
        self.assertEqual(normalized['items'][0]['unit_price'], '1234.56')
        self.assertEqual(self.record['seller']['name'], 'Örnek Bilişim Ltd. Şti.')
        self.assertEqual(approval_errors(normalized), [])

    def test_invalid_quantity_and_nonfinite_values_rejected(self):
        for quantity in ('0', '-1', 'NaN', 'abc'):
            value = deepcopy(self.record)
            value['items'][0]['quantity'] = quantity
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                normalized_record(value, self.record)

    def test_approval_is_durable_and_revision_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            identifier = 'a'*32
            store.create(identifier, 'fatura.pdf', 'application/pdf')
            store.analyzed(identifier, self.record)
            doc = store.get(identifier)
            updated = deepcopy(self.record)
            updated['seller']['name'] = 'Onaylı satıcı'
            store.save(identifier, updated, doc['revision'], approved=True)
            with self.assertRaises(ValueError):
                store.save(identifier, self.record, doc['revision'])
            reopened = Store(directory).get(identifier)
            self.assertEqual(reopened['status'], 'approved')
            self.assertEqual(reopened['original']['seller']['name'], 'Örnek Bilişim Ltd. Şti.')
            self.assertEqual(reopened['edited']['seller']['name'], 'Onaylı satıcı')
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM approvals').fetchone()[0], 1)

    def test_invoice_note_is_editable_and_optional(self):
        value = deepcopy(self.record)
        value['invoice_note'] = 'Kullanıcının düzelttiği fatura notu'
        record = normalized_record(value, self.record)
        self.assertEqual(record['invoice_note'], value['invoice_note'])
        value['invoice_note'] = ''
        self.assertIsNone(normalized_record(value, self.record)['invoice_note'])

    def test_reanalysis_preserves_first_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            identifier = 'b' * 32
            store.create(identifier, 'belge.pdf', 'application/pdf')
            store.analyzed(identifier, self.record)
            second = deepcopy(self.record)
            second['invoice_note'] = 'Yeni okunan not'
            store.analyzed(identifier, second)
            record = store.get(identifier)
            self.assertIsNone(record['original']['invoice_note'])
            self.assertEqual(record['edited']['invoice_note'], 'Yeni okunan not')

    def test_deleted_invoice_is_removed_from_visible_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            identifier='c'*32
            store.create(identifier,'fatura.pdf','application/pdf')
            store.analyzed(identifier,self.record)
            revision=store.get(identifier)['revision']
            with self.assertRaises(ValueError):
                store.delete(identifier,revision-1)
            store.delete(identifier,revision)
            self.assertIsNone(store.get(identifier))
            self.assertEqual(store.list(),[])
            with store.connect() as db:
                row=db.execute('SELECT status, original_json FROM documents WHERE id=?',(identifier,)).fetchone()
                self.assertEqual(row['status'],'deleted')
                self.assertIsNotNone(row['original_json'])

    def test_processing_invoice_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            identifier='d'*32
            doc=store.create(identifier,'fatura.pdf','application/pdf')
            with self.assertRaises(ValueError):
                store.delete(identifier,doc['revision'])

    def test_company_name_does_not_force_a_fixed_coordinate_profile(self):
        from unittest.mock import patch
        from fatura.extraction.service import image_record
        class Backend:
            name='test'
            def __call__(self,path,regions=None):
                if regions is None:
                    return {'width':100,'height':100,'engine':'test','lines':[
                        {'text':'SINERJI DANISMANLIK FATURA','bbox':[0,0,90,10]}]}
                self.regions=regions
                return {'regions':{'seller.name':[{'text':'Dinamik firma adı','bbox':[0,0,20,10]}]}}
        backend=Backend()
        regions={'seller.name':[.5,.5,.9,.6]}
        with patch('fatura.extraction.flexible_layout.dynamic_regions',return_value=(regions,[])):
            result=image_record('unused.png',backend)
        self.assertEqual(backend.regions,regions)
        self.assertEqual(result['seller']['name'],'Dinamik firma adı')
