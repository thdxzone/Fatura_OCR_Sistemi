"""İlk veri aktarımı ve yedekleme dolu hedefe veya mevcut yedeğe dokunmamalı."""

from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from scripts.backup_data import backup
from scripts.seed_data import seed
from fatura.storage import Store
from fatura.extraction.service import empty_record


class DeploymentDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.store = Store(self.source)
        self.store.create("a" * 32, "ornek.pdf", "application/pdf")
        self.store.analyzed("a" * 32, empty_record())
        file = self.store.file("a" * 32)
        file.parent.mkdir(parents=True)
        file.write_bytes(b"%PDF-test")
        (self.source / "reference").mkdir()
        (self.source / "reference" / "increase-analysis.json").write_text(
            '{"rows":[],"years":[]}'
        )

    def test_seed_copies_documents_and_valid_database_without_overwriting(self):
        target = self.root / "target"
        target.mkdir()
        seed(self.source, target)
        with sqlite3.connect(target / "invoices.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                db.execute("select count(*) from documents").fetchone()[0], 1
            )
        self.assertEqual(
            (target / "files" / ("a" * 32) / "source").read_bytes(), b"%PDF-test"
        )
        before = (target / "invoices.sqlite3").read_bytes()
        with self.assertRaises(ValueError):
            seed(self.source, target)
        self.assertEqual((target / "invoices.sqlite3").read_bytes(), before)
        with self.assertRaises(ValueError):
            seed(self.source, self.source / "nested")

    def test_backup_preserves_source_and_never_replaces_an_existing_archive(self):
        output = self.root / "backups" / "snapshot.zip"
        before = (self.source / "invoices.sqlite3").read_bytes()
        backup(self.source, output)
        self.assertEqual((self.source / "invoices.sqlite3").read_bytes(), before)
        with zipfile.ZipFile(output) as archive:
            self.assertIn("invoices.sqlite3", archive.namelist())
            self.assertIn("reference/increase-analysis.json", archive.namelist())
            self.assertIn("files/" + ("a" * 32) + "/source", archive.namelist())
        with self.assertRaises(ValueError):
            backup(self.source, output)
        with self.assertRaises(ValueError):
            backup(self.source, self.source / "backup.zip")
