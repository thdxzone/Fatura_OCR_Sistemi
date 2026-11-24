"""Belgesiz, elle girilen fatura: uygulamanın HTTP uç noktaları üzerinden (SQLite modu).

Örnek veriler sentetiktir.
"""

from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from fatura.web.app import Application, Handler


class ManualInvoiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Application(temp.name, reader="prototype")
        self.addCleanup(self.app.executor.shutdown)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.application = self.app
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, path, body=None):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def record(self, number="EL-2026-001"):
        return {
            "seller": {"name": "Örnek Tedarikçi A.Ş.", "tax_id": "1234567890", "address": None},
            "invoice_number": number,
            "invoice_date": "2026-11-20",
            "currency": "TRY",
            "invoice_note": None,
            "totals": {"net": "1000", "vat": "200", "gross": "1200", "discount": None,
                       "withholding": None, "payable": "1200"},
            "items": [{"name": "Bakım hizmeti", "description": "Kasım bakımı", "unit": "Adet",
                       "quantity": "1", "unit_price": "1000", "vat_rate": "20",
                       "line_total": "1000", "discount": None,
                       "price_basis": "net", "total_basis": "net"}],
        }

    def create(self):
        status, doc = self.call("/api/documents/manual", {})
        self.assertEqual(status, 201)
        return doc

    def test_manual_invoice_is_saved_and_approved_like_any_invoice(self):
        doc = self.create()
        self.assertEqual((doc["status"], doc["mime"]), ("review", "manual"))
        self.assertIsNotNone(doc["edited"])  # form boş ama hazır açılır

        status, saved = self.call(
            f"/api/documents/{doc['id']}/save",
            {"record": self.record(), "revision": doc["revision"], "approve": True, "reviewed": True},
        )
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["status"], "approved")
        _, listing = self.call("/api/documents")
        self.assertEqual([d["invoice_number"] for d in listing["documents"]], ["EL-2026-001"])
        self.assertEqual(listing["summaries"]["TRY"]["all"]["count"], 1)

    def test_manual_invoice_has_no_document_or_rereading(self):
        doc = self.create()
        for part in ("source", "preview"):
            status, body = self.call(f"/api/documents/{doc['id']}/{part}")
            self.assertEqual(status, 404)
            self.assertIn("elle girildi", body["error"])
        status, body = self.call(f"/api/documents/{doc['id']}/retry", {})
        self.assertEqual(status, 400)
        self.assertIn("yeniden okunamaz", body["error"])
        with self.assertRaises(ValueError):
            self.app.store.retry(doc["id"])  # depo da engeller

    def test_same_rules_apply_duplicates_and_required_fields(self):
        first = self.create()
        self.call(f"/api/documents/{first['id']}/save",
                  {"record": self.record(), "revision": first["revision"], "approve": True, "reviewed": True})
        second = self.create()
        status, body = self.call(f"/api/documents/{second['id']}/save",
                                 {"record": self.record(" el-2026-001 "), "revision": second["revision"]})
        self.assertEqual((status, body["duplicate_of"]), (409, first["id"]))

        incomplete = self.record("EL-2026-002") | {"invoice_date": None}
        status, body = self.call(f"/api/documents/{second['id']}/save",
                                 {"record": incomplete, "revision": second["revision"], "approve": True})
        self.assertEqual(status, 422)  # onay için zorunlu alanlar aynı


if __name__ == "__main__":
    unittest.main()
