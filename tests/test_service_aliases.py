"""Hizmet takma adlarının veriye taşınmasını, düzenlenmesini ve eşleştirmeyi sınar."""

import tempfile
import unittest

from fatura.analysis.services import Services, clean_aliases


def reference(*rows):
    return {"years": [2026], "imported_date": "2026-11-17", "rows": list(rows)}


def service(identifier, description, institution="Sinerji"):
    return {
        "id": identifier,
        "institution": institution,
        "description": description,
        "amounts": {"2026": "1000"},
        "rates": {},
        "total_increase": None,
        "invoice_count": 0,
        "service_type": None,
        "note": None,
    }


def invoice(description, vendor="Sinerji Bilgisayar"):
    return {
        "seller": {"name": vendor},
        "currency": "TRY",
        "items": [{"name": "Hizmet", "description": description}],
    }


class ServiceAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def services(self, *rows):
        """Excel başlangıcı mevcutken açılan kurulum (canlıdaki durum)."""
        root = Services(self.temp.name).root
        # İlk açılışta taşıma yapılmış sayılmasın diye ayar dosyası sıfırlanır.
        Services.write(root / "service-tracking.json", {"started_at": "2026-11-17T00:00:00+00:00", "custom_rows": []})
        Services.write(root / "increase-analysis.json", reference(*rows))
        return Services(self.temp.name)

    def test_legacy_aliases_are_migrated_once_and_still_match(self):
        services = self.services(
            service("excel-row-1", "PACS Hizmeti"), service("excel-row-2", "Protein TSS")
        )
        rows = {row["id"]: row for row in services.rows()}
        self.assertEqual(rows["excel-row-1"]["aliases"], ["Radyoloji Görüntüleme", "PACS"])
        self.assertEqual(rows["excel-row-2"]["aliases"], ["TSS Protein"])
        # Eskiden kodda sabit olan eşleştirme aynen çalışır.
        self.assertEqual(services.match(invoice("Radyoloji görüntüleme bedeli"))["id"], "excel-row-1")
        self.assertEqual(services.match(invoice("TSS Protein Kasım"))["id"], "excel-row-2")
        # Kullanıcı takma adı silerse ikinci açılışta geri gelmez.
        services.add_alias("excel-row-2", "Protein test")
        config = services.config()
        config["aliases"]["excel-row-1"] = []
        Services.write(services.settings, config)
        self.assertEqual(Services(self.temp.name).rows()[0]["aliases"], [])

    def test_alias_added_from_invoice_matches_next_invoice(self):
        services = self.services(service("excel-row-3", "Ekinoks"))
        self.assertIsNone(services.match(invoice("Hemşire çağrı sistemi bakımı"))["id"])
        services.add_alias("excel-row-3", "Hemşire çağrı sistemi")
        match = services.match(invoice("HEMŞİRE ÇAĞRI SİSTEMİ BAKIMI Aralık"))
        self.assertEqual(match["id"], "excel-row-3")
        self.assertEqual(match["method"], "automatic")
        # Yazım farkı olan tekrar ve hizmet adının kendisi eklenmez.
        services.add_alias("excel-row-3", "hemşire  çağrı sistemi")
        services.add_alias("excel-row-3", "EKİNOKS")
        self.assertEqual(services.rows()[0]["aliases"], ["Hemşire çağrı sistemi"])
        with self.assertRaises(ValueError):
            services.add_alias("excel-row-yok", "Bir ad")

    def test_service_edit_saves_aliases_outside_overrides(self):
        services = self.services(service("excel-row-4", "Ekinoks"))
        current = services.analysis([])["rows"][0]
        record = current["editable"] | {"aliases": [" Ek-1 ", "", "ek 1", "Ekinoks"]}
        saved = services.update(current["id"], {"record": record, "revision": current["edit_revision"]})
        self.assertEqual(saved["aliases"], ["Ek-1"])
        config = services.config()
        self.assertNotIn("aliases", config["overrides"]["excel-row-4"])
        self.assertEqual(config["aliases"]["excel-row-4"], ["Ek-1"])
        # Takma ad gönderilmeyen düzenleme mevcut takma adları korur.
        current = services.analysis([])["rows"][0]
        record = {k: v for k, v in current["editable"].items() if k != "aliases"}
        saved = services.update(current["id"], {"record": record, "revision": current["edit_revision"]})
        self.assertEqual(saved["aliases"], ["Ek-1"])

    def test_invalid_aliases_are_rejected(self):
        with self.assertRaises(ValueError):
            clean_aliases("PACS")
        with self.assertRaises(ValueError):
            clean_aliases(["x" * 301])
        with self.assertRaises(ValueError):
            clean_aliases(["---"])
        with self.assertRaises(ValueError):
            clean_aliases(["a"] * 101)


if __name__ == "__main__":
    unittest.main()
