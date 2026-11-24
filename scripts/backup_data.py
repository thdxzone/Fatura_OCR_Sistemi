"""Veritabanını SQLite yedek API'siyle, belgeleriyle birlikte ZIP'e alır."""

import argparse
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents:
        raise ValueError("Yedek dosyası veri klasörünün dışında olmalı.")
    if destination.exists():
        raise ValueError("Bu yedek adı mevcut; farklı bir ad kullanın.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Kaynak dosyaları kopyalamadan önce veritabanının tutarlı görüntüsü alınır.
    with tempfile.TemporaryDirectory(prefix="fatura-backup-") as work:
        snapshot = Path(work) / "invoices.sqlite3"
        database = source / "invoices.sqlite3"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as current:
            if current.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Kaynak veritabanının bütünlüğü doğrulanamadı.")
            with sqlite3.connect(snapshot) as copy:
                current.backup(copy)
        # Geçici çıktı, yarım kalan dosyanın tamamlanmış yedek sanılmasını engeller.
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, suffix=".partial", delete=False
        ) as stream:
            partial = Path(stream.name)
        try:
            with zipfile.ZipFile(
                partial, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.write(snapshot, "invoices.sqlite3")
                for path in sorted(source.rglob("*")):
                    relative = path.relative_to(source)
                    if (
                        not path.is_file()
                        or relative.parts[0] == "backups"
                        or path.name in ("invoices.sqlite3", ".instance.lock")
                        or path.name.endswith(("-journal", "-wal", "-shm"))
                    ):
                        continue
                    archive.write(path, relative.as_posix())
            # Aynı isimle ikinci bir iş başlarsa mevcut yedek ezilmez.
            os.link(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print("Yedek hazır:", backup(args.source, args.output))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error) + "\n")
