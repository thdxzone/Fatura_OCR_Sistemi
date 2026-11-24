"""Mevcut kayıtları boş bir kalıcı diske taşır; dolu hedefe asla yazmaz."""

import argparse
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile


def seed(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("Kaynak ve hedef ayrı klasörler olmalı.")
    database = source / "invoices.sqlite3"
    if not database.is_file():
        raise ValueError("Kaynakta invoices.sqlite3 bulunamadı.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Hedef dolu. Mevcut kayıtların üzerine yazılmadı.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".fatura-import-", dir=target.parent
    ) as work:
        staged = Path(work) / "data"
        shutil.copytree(
            source,
            staged,
            ignore=shutil.ignore_patterns(
                "*.sqlite3-journal", "*.sqlite3-wal", "*.sqlite3-shm", ".instance.lock"
            ),
        )
        # Dosyayı doğrudan kopyalamak yerine SQLite'ın tutarlı yedek API'si kullanılır.
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as current:
            if current.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Kaynak veritabanının bütünlük kontrolü başarısız.")
            (staged / "invoices.sqlite3").unlink()
            with closing(sqlite3.connect(staged / "invoices.sqlite3")) as backup:
                current.backup(backup)
        # Dolu hedef, ikinci kontrolde de korunur; boş volume dizini değiştirilebilir.
        if target.exists() and any(target.iterdir()):
            raise ValueError("Hedef işlem sırasında doldu; aktarım iptal edildi.")
        staged.replace(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    try:
        print("Aktarım tamamlandı:", seed(args.source, args.target))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error) + "\n")
