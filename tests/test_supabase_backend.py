"""Supabase (Postgres) deposunun SQLite/JSON deposuyla aynı davrandığını sınar.

Bir PostgreSQL sunucusu gerekir; yoksa testler atlanır:
  FATURA_TEST_PG_ADMIN_URL=postgresql://postgres:<parola>@127.0.0.1:55432/postgres
Her çalıştırmada geçici bir veritabanı açılır, migration'lar uygulanır ve testler
fatura_backend rolüyle (uygulamanın yetkileriyle) çalışır. Örnek belgeler sentetiktir.
"""

from copy import deepcopy
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import unittest
from urllib.parse import urlsplit, urlunsplit
import uuid

from fatura.analysis.services import Services
from fatura.extraction.service import empty_record
from fatura.invoice import run
from fatura.storage import DuplicateInvoice, Store
from fatura.supabase.files import StorageError

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("FATURA_TEST_PG_ADMIN_URL")
SQL_FILES = [
    ROOT / "supabase/tests/supabase_stub.sql",
    *sorted((ROOT / "supabase/migrations").glob("*.sql")),
]


class FakeFiles:
    """Bellekte Storage taklidi; üzerine yazmayı reddeder (x-upsert: false)."""

    def __init__(self):
        self.objects = {}

    def upload(self, path, data):
        if path in self.objects:
            raise StorageError("Belge deposu POST 409: Duplicate", 409)
        self.objects[path] = data

    def download(self, path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[path])
        return destination

    def delete(self, paths):
        for path in paths:
            self.objects.pop(path, None)


def comparable(document):
    """Zaman damgaları ve sayıların yazım biçimi ('10.50' / '10.5') karşılaştırma dışıdır."""
    from fatura.supabase.migrate import TIMESTAMPS, normalized

    document = normalized(document)
    for key in TIMESTAMPS:
        document.pop(key, None)
    return document


def listed(document):
    """Liste ekranı ve analizlerin kullandığı alanlar (app /api/documents, Services.analysis).

    Postgres listesi bilerek hafiftir: kanıt kutuları ve OCR sayfa metni yüklenmez.
    """
    document = comparable(document)
    document.pop("original")
    record = document["edited"]
    if record:
        document["edited"] = {
            key: record.get(key)
            for key in ("seller", "invoice_number", "invoice_date", "currency", "totals", "issues", "invoice_note")
        } | {"items": [{k: v for k, v in item.items() if k != "evidence"} for item in record["items"]]}
    return document


@unittest.skipUnless(ADMIN_URL, "FATURA_TEST_PG_ADMIN_URL tanımlı değil; Postgres testleri atlandı.")
class SupabaseBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool

        cls.psycopg = psycopg
        cls.database = "fatura_test_" + secrets.token_hex(4)
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(cls.database)))
        parts = urlsplit(ADMIN_URL)
        cls.admin_url = urlunsplit(parts._replace(path="/" + cls.database))
        password = secrets.token_hex(16)
        with psycopg.connect(cls.admin_url, autocommit=True) as admin:
            for path in SQL_FILES:
                admin.execute(path.read_text(encoding="utf-8"))
            admin.execute(
                sql.SQL("alter role fatura_backend with login password {}").format(sql.Literal(password))
            )
        netloc = f"fatura_backend:{password}@{parts.hostname}:{parts.port}"
        cls.backend_url = backend_url = urlunsplit(parts._replace(netloc=netloc, path="/" + cls.database))
        cls.pool = ConnectionPool(backend_url,min_size=1, max_size=2, kwargs={"prepare_threshold": None}, open=True)

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql

        cls.pool.close()
        with cls.psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(cls.database)))

    def setUp(self):
        from fatura.supabase.services import PgServices
        from fatura.supabase.store import PgStore

        with self.psycopg.connect(self.admin_url, autocommit=True) as admin:
            admin.execute(
                "truncate fatura.belgeler, fatura.kalemler, fatura.ocr_calismalari, fatura.onaylar, "
                "fatura.hizmetler, fatura.hizmet_yillik_bedeller, fatura.hizmet_yillik_odemeler, "
                "fatura.hizmet_takma_adlari, fatura.referans_fatura_numaralari, fatura.referans_aktarimi, "
                "core.firmalar cascade"
            )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.files = FakeFiles()
        self.pg = PgStore(self.temp / "pg", self.pool, self.files)
        self.sqlite = Store(self.temp / "sqlite")
        self.pg_services = PgServices(self.pool)
        self.record = run(ROOT / "examples/multi_page.pdf")

    def upload(self, store, identifier, data=b"%PDF-1.4 ornek"):
        directory = store.root / "files" / identifier
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source").write_bytes(data)

    def both(self, action):
        """Aynı işlemi iki depoda çalıştırır; sonuçları döner."""
        return action(self.sqlite), action(self.pg)

    def create(self, identifier):
        def act(store):
            self.upload(store, identifier)
            return store.create(identifier, "ornek.pdf", "application/pdf")

        return self.both(act)

    def assertSameDocument(self, identifier):
        sqlite, pg = self.both(lambda store: store.get(identifier))
        self.assertEqual(comparable(sqlite), comparable(pg))

    # --- Belge akışı ------------------------------------------------------

    def test_document_lifecycle_matches_sqlite(self):
        identifier = secrets.token_hex(16)
        self.create(identifier)
        self.assertSameDocument(identifier)
        self.both(lambda store: store.analyzed(identifier, self.record))
        self.assertSameDocument(identifier)

        edited = deepcopy(self.record)
        edited["seller"]["name"] = "Örnek Tedarikçi A.Ş."
        edited["items"][0]["evidence"] = None
        self.both(lambda store: store.save(identifier, edited, 2, approved=False))
        self.assertSameDocument(identifier)
        self.both(lambda store: store.save(identifier, edited, 3, approved=True))
        self.assertSameDocument(identifier)

        sqlite, pg = self.both(lambda store: store.list())
        self.assertEqual([listed(d) for d in sqlite], [listed(d) for d in pg])
        with self.pool.connection() as conn:
            approvals = conn.execute("select count(*) from fatura.onaylar").fetchone()[0]
            runs = conn.execute("select count(*) from fatura.ocr_calismalari").fetchone()[0]
        self.assertEqual((approvals, runs), (1, 1))

        for store in (self.sqlite, self.pg):
            with self.assertRaises(ValueError):
                store.retry(identifier)  # onaylı kayıt yeniden okunamaz
            with self.assertRaises(ValueError):
                store.save(identifier, edited, 2)  # eski revizyon
        self.both(lambda store: store.delete(identifier, 4))
        self.assertEqual(self.both(lambda store: store.get(identifier)), (None, None))
        self.assertEqual(len(self.files.objects), 1)  # silinen belgenin PDF'i denetim için kalır

    def test_retry_keeps_original_and_uses_latest_reading(self):
        identifier = secrets.token_hex(16)
        self.create(identifier)
        self.both(lambda store: store.analyzed(identifier, self.record))
        self.both(lambda store: store.retry(identifier))
        second = deepcopy(self.record)
        second["metrics"] = {"elapsed_seconds": 2.5, "process_peak_rss_bytes": None}
        self.both(lambda store: store.analyzed(identifier, second, "Okuma kısmen tamamlandı."))
        self.assertSameDocument(identifier)
        document = self.pg.get(identifier)
        self.assertEqual(document["original"]["metrics"], self.record["metrics"])
        self.assertEqual(document["edited"]["metrics"]["elapsed_seconds"], 2.5)
        self.assertEqual(document["error"], "Okuma kısmen tamamlandı.")

    def test_duplicates_match_sqlite(self):
        first, second = secrets.token_hex(16), secrets.token_hex(16)
        for identifier in (first, second):
            self.create(identifier)
            self.both(lambda store: store.analyzed(identifier, self.record))
        for store in (self.sqlite, self.pg):
            document = store.get(second)
            self.assertEqual((document["status"], document["duplicate_of"]), ("duplicate", first))
            changed = deepcopy(document["edited"])
            changed["invoice_number"] = " " + changed["invoice_number"].lower() + " "
            with self.assertRaises(DuplicateInvoice):
                store.save(second, changed, document["revision"])
        self.assertSameDocument(second)
        self.both(lambda store: store.delete(first, 2))
        for store in (self.sqlite, self.pg):
            document = store.get(second)
            saved = store.save(second, document["edited"], document["revision"])
            self.assertEqual((saved["status"], saved["duplicate_of"], saved["error"]), ("review", None, None))

    def test_deleted_duplicate_keeps_reference(self):
        first, second = secrets.token_hex(16), secrets.token_hex(16)
        for identifier in (first, second):
            self.create(identifier)
            self.both(lambda store: store.analyzed(identifier, self.record))
        self.both(lambda store: store.delete(second, 2))
        with self.pool.connection() as conn:
            row = conn.execute(
                "select durum, mukerrer_belge_id from fatura.belgeler where id = %s", (uuid.UUID(hex=second),)
            ).fetchone()
        self.assertEqual((row[0], row[1].hex), ("deleted", first))

    def test_batch_replaces_combined_document(self):
        combined = secrets.token_hex(16)
        pages = [{"id": secrets.token_hex(16), "filename": f"ornek · sayfa {i}.pdf"} for i in (1, 2)]
        batch = secrets.token_hex(16)
        self.create(combined)
        self.both(lambda store: store.analyzed(combined, empty_record()))

        def act(store):
            for page in pages:
                self.upload(store, page["id"])
            return store.create_batch(pages, batch, replaced=(combined, 2))

        self.both(act)
        sqlite, pg = self.both(lambda store: store.batch(batch))
        self.assertEqual(sqlite, pg)
        self.assertEqual([p["batch_page"] for p in pg], [1, 2])
        self.assertIsNone(self.pg.get(combined))
        for page in pages:
            self.assertSameDocument(page["id"])

        # Revizyonu değişmiş kayıt ayrılamaz; yüklenen PDF'ler geri silinir.
        before = set(self.files.objects)
        other = [{"id": secrets.token_hex(16), "filename": "x.pdf"}]
        self.upload(self.pg, other[0]["id"])
        with self.assertRaises(ValueError):
            self.pg.create_batch(other, secrets.token_hex(16), replaced=(pages[0]["id"], 99))
        self.assertEqual(set(self.files.objects), before)

    def test_pdf_is_downloaded_when_local_cache_is_missing(self):
        identifier = secrets.token_hex(16)
        self.upload(self.pg, identifier, b"%PDF-1.4 icerik")
        self.pg.create(identifier, "ornek.pdf", "application/pdf")
        shutil.rmtree(self.pg.root / "files" / identifier)
        self.assertEqual(self.pg.file(identifier).read_bytes(), b"%PDF-1.4 icerik")
        path = next(iter(self.files.objects))
        self.assertRegex(path, r"^\d{4}/\d{2}/[0-9a-f-]{36}\.pdf$")

    def test_precision_is_never_rounded_silently(self):
        identifier = secrets.token_hex(16)
        self.upload(self.pg, identifier)
        self.pg.create(identifier, "ornek.pdf", "application/pdf")
        record = deepcopy(self.record)
        record["totals"]["gross"] = "10.005"
        self.pg.analyzed(identifier, record)
        document = self.pg.get(identifier)
        self.assertIsNone(document["edited"]["totals"]["gross"])
        self.assertIn("storage_precision", [i["code"] for i in document["edited"]["issues"]])
        self.assertTrue(document["edited"]["needs_review"])
        with self.assertRaisesRegex(ValueError, "en fazla 2 ondalık"):
            self.pg.save(identifier, record, document["revision"])

    def test_company_card_is_linked_by_tax_id(self):
        identifier = secrets.token_hex(16)
        self.upload(self.pg, identifier)
        self.pg.create(identifier, "ornek.pdf", "application/pdf")
        record = deepcopy(self.record)
        record["seller"] = {"name": "Okunan Unvan", "tax_id": "123 456 7890", "address": None}
        self.pg.analyzed(identifier, record)
        record["seller"]["name"] = "Onaylanan Unvan"
        for item in record["items"]:
            item["evidence"] = None
        self.pg.save(identifier, record, 2, approved=True)
        with self.pool.connection() as conn:
            companies = conn.execute("select vkn, unvan from core.firmalar").fetchall()
            linked = conn.execute("select count(*) from fatura.belgeler where firma_id is not null").fetchone()[0]
        self.assertEqual(companies, [("1234567890", "Onaylanan Unvan")])
        self.assertEqual(linked, 1)

    def test_interrupted_processing_is_recovered(self):
        from fatura.supabase.store import PgStore

        identifier = secrets.token_hex(16)
        self.upload(self.pg, identifier)
        self.pg.create(identifier, "ornek.pdf", "application/pdf")
        PgStore(self.temp / "pg", self.pool, self.files)
        document = self.pg.get(identifier)
        self.assertEqual(document["status"], "review")
        self.assertIn("tamamlanmadan", document["error"])

    # --- Hizmetler ----------------------------------------------------------

    def services(self):
        reference = {
            "years": [2025, 2026],
            "imported_date": "2026-11-17",
            "baseline_invoice_numbers": ["OLD001"],
            "rows": [
                {
                    "id": "excel-row-5", "source_row": 5, "institution": "Sinerji", "description": "Ekinoks",
                    "amounts": {"2025": "8320", "2026": "8320"}, "rates": {}, "total_increase": None,
                    "invoice_count": 10, "service_type": "Bitti", "unit_price": None, "single_date": None,
                    "note": "2025 Kasım ayında başladı. 2026 Ekim ayında bitti",
                }
            ],
        }
        Store(self.temp / "json")  # taşıma betiği boş da olsa belge veritabanı bekler
        json_services = Services(self.temp / "json")
        Services.write(json_services.root / "increase-analysis.json", reference)
        tracking = json_services.config() | {"aliases": {"excel-row-5": ["EKN"]}}
        Services.write(json_services.settings, tracking)
        self.migrate(self.temp / "json")  # hizmetler de taşıma betiğinin yoluyla yazılır
        return json_services, tracking["started_at"]

    def invoice(self, started, number="NEW001", amount="9000", when="2026-11-18", status="approved"):
        return {
            "id": number, "status": status, "created_at": started, "updated_at": started,
            "edited": {
                "seller": {"name": "Sinerji Bilgisayar"}, "currency": "TRY", "invoice_number": number,
                "invoice_date": when, "items": [{"name": "Hizmet", "description": "EKN hizmet bedeli"}],
                "totals": {"gross": amount},
            },
        }

    def test_service_analysis_matches_json(self):
        json_services, started = self.services()
        documents = [self.invoice(started), self.invoice(started, "OLD001", "8000", "2026-01-10")]
        expected = json_services.analysis(documents)
        actual = self.pg_services.analysis(documents)
        volatile = ("rates", "total_increase", "editable", "edit_revision")
        for row in (expected["rows"][0], actual["rows"][0]):
            for key in volatile:
                row.pop(key, None)
        self.assertEqual(expected["rows"], actual["rows"])
        self.assertEqual(
            {k: expected[k] for k in ("years", "tracking_started_at", "unmatched_document_ids")},
            {k: actual[k] for k in ("years", "tracking_started_at", "unmatched_document_ids")},
        )

    def test_service_edit_create_and_aliases(self):
        self.services()
        current = self.pg_services.analysis([])["rows"][0]
        record = current["editable"] | {
            "amounts": {"2025": "9000", "2026": "10000.50"},
            "start_date": "2025-11-01",
            "aliases": ["EKN", "Ekinoks lisans"],
            "yearly_payments": {"2025": {"invoice_count": 2, "total": "18000", "through_date": "2025-12-31"}},
        }
        self.pg_services.update(current["id"], {"record": record, "revision": current["edit_revision"]})
        row = self.pg_services.rows()[0]
        self.assertEqual(row["amounts"], {"2025": "9000", "2026": "10000.5"})
        self.assertEqual(row["rates"]["2026"], "0.1112")
        self.assertEqual((row["start_date"], "end_date" in row), ("2025-11-01", False))
        self.assertEqual(row["aliases"], ["EKN", "Ekinoks lisans"])
        self.assertEqual(row["yearly_payments"]["2025"]["total"], "18000")
        with self.assertRaises(ValueError):  # eski revizyon
            self.pg_services.update(current["id"], {"record": record, "revision": current["edit_revision"]})
        fresh = self.pg_services.analysis([])["rows"][0]
        bad = fresh["editable"] | {"amounts": {"2025": "1.005", "2026": "1"}}
        with self.assertRaisesRegex(ValueError, "en fazla 2 ondalık"):
            self.pg_services.update(fresh["id"], {"record": bad, "revision": fresh["edit_revision"]})

        created = self.pg_services.create({"description": "Yazılım desteği", "institution": "ABC Bilişim"})
        self.assertRegex(created["id"], r"^[0-9a-f-]{36}$")
        self.pg_services.add_alias(created["id"], "Yazılım bakım")
        record = self.invoice("2026-11-18T00:00:00+00:00")["edited"]
        record["seller"]["name"] = "ABC Bilişim"
        record["items"][0]["description"] = "Yazılım bakım Kasım"
        self.assertEqual(self.pg_services.match(record)["id"], created["id"])

        # Faturadaki hizmet bağlantısı eski koduyla (excel-row-5) da saklanır.
        identifier = secrets.token_hex(16)
        self.upload(self.pg, identifier)
        self.pg.create(identifier, "ornek.pdf", "application/pdf")
        self.pg.analyzed(identifier, self.record)
        linked = deepcopy(self.record) | {"service_id": "excel-row-5"}
        saved = self.pg.save(identifier, linked, 2)
        self.assertEqual(saved["edited"]["service_id"], "excel-row-5")

    # --- Veri taşıma (fatura.supabase.migrate) -----------------------------

    def migrate(self, root, mode="uygula", conflicts="dur", exclude=()):
        from fatura.supabase import migrate

        lines = []
        migrate.run(root, mode, conflicts, self.backend_url, self.files, exclude, out=lines.append)
        return lines

    def source(self, prevent_duplicates=True):
        """Taşınacak SQLite + JSON verisi, uygulamanın kendi API'siyle (sentetik)."""
        root = self.temp / "kaynak"
        store = Store(root, prevent_duplicates=prevent_duplicates)
        Services(root)
        return root, store

    def add(self, store, number, data=b"%PDF-1.4 ornek", mime="application/pdf"):
        identifier = secrets.token_hex(16)
        self.upload(store, identifier, data)
        store.create(identifier, "ornek.pdf", mime)
        store.analyzed(identifier, self.numbered(number))
        return identifier

    def numbered(self, number):
        record = deepcopy(self.record)
        record["invoice_number"] = number
        return record

    def count(self, table):
        with self.pool.connection() as conn:
            return conn.execute(f"select count(*) from {table}").fetchone()[0]

    def test_migration_copies_every_document_state(self):
        from fatura.supabase.migrate import MigrationError, normalized

        root, store = self.source()
        approved = self.add(store, "A-1")
        edited = store.get(approved)["edited"] | {"seller": {"name": "Örnek Tedarikçi", "tax_id": "1234567890"}}
        store.save(approved, edited, 2, approved=True)
        review = self.add(store, "B-1")
        retried = self.add(store, "C-1")
        store.retry(retried)
        store.analyzed(retried, self.numbered("C-1"), "Okuma kısmen tamamlandı.")
        duplicate = self.add(store, "A-1")
        deleted_duplicate = self.add(store, "A-1")
        store.delete(deleted_duplicate, 2)
        deleted = self.add(store, "D-1")
        store.delete(deleted, 2)
        identifiers = (approved, review, retried, duplicate, deleted_duplicate, deleted)

        lines = self.migrate(root, mode="prova")
        self.assertIn("geri alındı", lines[-1])
        self.assertEqual((self.count("fatura.belgeler"), self.files.objects), (0, {}))

        self.migrate(root)
        for identifier in identifiers:  # zaman damgaları dahil birebir
            self.assertEqual(
                normalized(self.pg.get(identifier, include_deleted=True)),
                normalized(store.get(identifier) or Store.decode(self.raw(store, identifier))),
            )
        self.assertEqual([listed(d) for d in store.list()], [listed(d) for d in self.pg.list()])
        self.assertEqual(self.pg.get(duplicate)["duplicate_of"], approved)
        self.assertEqual(self.count("fatura.onaylar"), 1)
        with self.pool.connection() as conn:
            card = conn.execute("select unvan from core.firmalar where vkn = '1234567890'").fetchone()
        self.assertEqual(card, ("Örnek Tedarikçi",))  # onaylanan kayıttaki unvan
        self.assertEqual(len(self.files.objects), len(identifiers))
        with self.assertRaisesRegex(MigrationError, "sorun"):
            self.migrate(root)  # hedef artık boş değil

    def raw(self, store, identifier):
        with store.connect() as db:
            return db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()

    def test_migration_conflicts_and_exclusions(self):
        from fatura.supabase.migrate import MigrationError

        root, store = self.source(prevent_duplicates=False)  # eski sürümde oluşmuş çakışma
        approved = self.add(store, "A-1")
        store.save(approved, store.get(approved)["edited"], 2, approved=True)
        conflict = self.add(store, " a-1 ")
        image = self.add(store, "E-1", b"\x89PNG\r\n\x1a\n", "image/png")

        with self.assertRaises(MigrationError) as raised:
            self.migrate(root, mode="kontrol")
        problems = "\n".join(raised.exception.problems)
        self.assertIn(conflict, problems)
        self.assertIn(f"{image}: kaynak dosya PDF değil (image/png)", problems)

        self.migrate(root, conflicts="mukerrer", exclude=(image,))
        document = self.pg.get(conflict)
        self.assertEqual((document["status"], document["duplicate_of"]), ("duplicate", approved))
        self.assertIn("zaten mevcut", document["error"])
        self.assertIsNone(self.pg.get(image, include_deleted=True))
        self.assertEqual(len(self.files.objects), 2)

    def test_migration_reuses_uploaded_pdf_only_if_identical(self):
        from fatura.supabase.migrate import MigrationError, object_path

        root, store = self.source()
        identifier = self.add(store, "A-1")
        path = object_path(store.get(identifier))
        self.files.objects[path] = b"%PDF-1.4 baska"
        with self.assertRaises(MigrationError) as raised:
            self.migrate(root)
        self.assertIn("aynı değil", raised.exception.problems[0])
        self.assertEqual(self.count("fatura.belgeler"), 0)  # commit edilmedi

        self.files.objects[path] = b"%PDF-1.4 ornek"  # önceki yarım kalmış yükleme
        lines = self.migrate(root)
        self.assertTrue(any("1 tanesi önceden yüklenmişti" in line for line in lines))
        self.assertEqual(self.count("fatura.belgeler"), 1)


if __name__ == "__main__":
    unittest.main()
