"""SQLite + JSON verisini Supabase'e taşır (plan: docs/veritabani-tasima-plani.md).

Uygulama durdurulmuşken, proje kökünden:
  python -m fatura.supabase.migrate --data-dir data             yalnızca ön kontrol; bağlantı yok
  python -m fatura.supabase.migrate --data-dir data --prova     yazar, doğrular, GERİ ALIR
  python -m fatura.supabase.migrate --data-dir data --uygula    yazar, doğrular, PDF'leri yükler, commit eder
  --cakismalar mukerrer   aynı fatura numaralı aktif kayıtlardan onaylı olmayanları
                          'duplicate' işaretler (varsayılan 'dur': taşıma durur)

Ortam: FATURA_DATABASE_URL (fatura_backend rolü), --uygula için ayrıca SUPABASE_URL ve
SUPABASE_SERVICE_ROLE_KEY. Kaynak veri salt okunur açılır. Çıktıda yalnızca sayılar ve
kayıt kimlikleri yer alır; fatura içeriği, satıcı adı ve VKN yazılmaz.

Yazma kuralları uygulamanınkiyle aynıdır (PgStore / PgServices yardımcıları kullanılır);
taşınan her belge sonra uygulamanın okuma koduyla geri okunup kaynakla karşılaştırılır.
"""

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid

from fatura.analysis.services import Services, words
from fatura.storage import DuplicateInvoice
from fatura.supabase.columns import Columns, ITEM_NUMBERS, TOTALS

ACTIVE = ("processing", "review", "approved")
MAX_PDF_BYTES = 25 * 1024 * 1024  # bucket sınırıyla aynı (file_size_limit)
TIMESTAMPS = ("created_at", "updated_at", "approved_at")
# Hedefte dolu olmaması gereken tablolar. core.firmalar diğer projelerle ortaktır;
# referans_aktarimi satırını uygulama ilk açılışta oluşturabilir, taşıma üzerine yazar.
MUST_BE_EMPTY = (
    "fatura.belgeler", "fatura.hizmetler", "fatura.ocr_calismalari", "fatura.onaylar",
    "fatura.referans_fatura_numaralari",
)


class MigrationError(Exception):
    def __init__(self, problems):
        super().__init__(f"{len(problems)} sorun bulundu; hiçbir şey yazılmadı.")
        self.problems = problems


# ----------------------------------------------------------------------
# Kaynak
# ----------------------------------------------------------------------


@dataclass
class Source:
    root: Path
    documents: list
    analyses: dict  # belge kimliği → [{"created_at", "data"}] (eskiden yeniye)
    approvals: dict  # belge kimliği → [{"approved_at", "data", "checks"}]
    reference: dict
    tracking: dict
    services: list  # Services.rows() biçimi (Excel + elle + düzeltmeler + takma adlar)

    def pdf(self, identifier):
        return self.root / "files" / identifier / "source"


def read_source(root, exclude=()):
    """Veriyi değiştirmeden okur (SQLite salt okunur; Services yapıcısı çağrılmaz).

    exclude: taşınmayacak belge kimlikleri (kullanıcı kararıyla, ör. laboratuvar artığı).
    """
    root = Path(root)
    database = root / "invoices.sqlite3"
    if not database.exists():
        raise MigrationError([f"Veritabanı bulunamadı: {database}"])
    # sqlite3 bağlantısının with bloğu bağlantıyı kapatmaz; closing ile dosya kilidi bırakılır.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        documents = []
        for row in db.execute("SELECT * FROM documents ORDER BY created_at, id"):
            value = dict(row)
            for key in ("original_json", "edited_json"):
                raw = value.pop(key)
                value[key.removesuffix("_json")] = json.loads(raw) if raw else None
            if value["id"] not in exclude:
                documents.append(value)
        unknown = set(exclude) - {row["id"] for row in db.execute("SELECT id FROM documents")}
        if unknown:
            raise MigrationError([f"{identifier}: hariç tutulacak belge bulunamadı." for identifier in sorted(unknown)])
        analyses, approvals = {}, {}
        included = {doc["id"] for doc in documents}
        for row in db.execute("SELECT document_id, created_at, data_json FROM analyses ORDER BY created_at, id"):
            if row["document_id"] in included:
                analyses.setdefault(row["document_id"], []).append(
                    {"created_at": row["created_at"], "data": json.loads(row["data_json"])}
                )
        for doc in documents:
            # Eski sürümler analyses satırı yazmadan original_json'u doldurmuş olabilir;
            # ilk okuma kaybolmasın diye OCR çalıştırması olarak taşınır.
            if doc["original"] is not None and not analyses.get(doc["id"]):
                analyses[doc["id"]] = [{"created_at": doc["created_at"], "data": doc["original"], "synthetic": True}]
        for row in db.execute(
            "SELECT document_id, approved_at, data_json, checks_json FROM approvals ORDER BY approved_at, id"
        ):
            if row["document_id"] not in included:
                continue
            approvals.setdefault(row["document_id"], []).append(
                {
                    "approved_at": row["approved_at"],
                    "data": json.loads(row["data_json"]),
                    "checks": json.loads(row["checks_json"]),
                }
            )
    settings = root / "reference" / "service-tracking.json"
    if not settings.exists():
        raise MigrationError(["service-tracking.json yok; uygulamayı yeni sürümle bir kez başlatın."])
    # Services.__init__ dosya yazar; burada yalnızca okuma metotları kullanılır.
    reader = Services.__new__(Services)
    reader.root = root / "reference"
    reader.settings = settings
    return Source(
        root=root,
        documents=documents,
        analyses=analyses,
        approvals=approvals,
        reference=reader.reference(),
        tracking=reader.config(),
        services=reader.rows(),
    )


# ----------------------------------------------------------------------
# Ön kontrol ve plan (bağlantı gerektirmez)
# ----------------------------------------------------------------------


@dataclass
class Plan:
    documents: list = field(default_factory=list)  # yazılacak sırayla, çakışma kararı uygulanmış
    columns: dict = field(default_factory=dict)  # belge kimliği → Columns
    firms: dict = field(default_factory=dict)  # VKN → (unvan, adres)
    baseline: list = field(default_factory=list)  # (fatura_no, anahtar)
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def plan(source, conflicts="dur"):
    result = Plan()
    problems = result.problems
    if "aliases" not in source.tracking:
        problems.append("service-tracking.json'da 'aliases' yok; uygulamayı yeni sürümle bir kez başlatın.")

    documents = {doc["id"]: deepcopy(doc) for doc in source.documents}
    for doc in documents.values():
        identifier = doc["id"]
        try:
            uuid.UUID(hex=identifier)
        except ValueError:
            problems.append(f"{identifier}: kimlik 32 haneli hex değil.")
            continue
        if doc["status"] == "processing":
            problems.append(f"{identifier}: okuma sürüyor görünüyor; uygulama durdurulmamış olabilir.")
        runs = source.analyses.get(identifier, [])
        if any(run.get("synthetic") for run in runs):
            result.notes.append(f"{identifier}: OCR çalıştırması yoktu; ilk okuma çalıştırma olarak taşınacak.")
        if doc["edited"] is not None and not runs:
            problems.append(f"{identifier}: okunmuş kayıt var ama OCR çalıştırması yok.")
        if runs and doc["original"] != runs[0]["data"]:
            problems.append(f"{identifier}: ilk okuma (original) en eski OCR çalıştırmasıyla aynı değil.")
        if doc["status"] == "approved" and not source.approvals.get(identifier):
            problems.append(f"{identifier}: onaylı ama onay kaydı yok.")
        pdf = source.pdf(identifier)
        if not pdf.is_file():
            problems.append(f"{identifier}: PDF dosyası yok.")
        else:
            with pdf.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    # Bucket yalnızca PDF kabul eder; görseller eski laboratuvar aracından kalmadır.
                    problems.append(f"{identifier}: kaynak dosya PDF değil ({doc['mime']}).")
            if pdf.stat().st_size > MAX_PDF_BYTES:
                problems.append(f"{identifier}: PDF 25 MB sınırını aşıyor.")
        if doc["edited"] is not None:
            try:
                result.columns[identifier] = Columns(doc["edited"], strict=True)
            except ValueError as error:
                # Mesajlar yalnızca alan adını içerir (ör. "2. kalem miktar ..."), değeri değil.
                problems.append(f"{identifier}: {error}")

    _check_duplicates(documents, problems)
    _resolve_conflicts(documents, result, conflicts)

    # Mükerrer kayıt, işaret ettiği kayıttan sonra yazılır (kod zincir oluşturmaz).
    ordered = sorted(documents.values(), key=lambda d: (d["status"] == "duplicate", d["created_at"], d["id"]))
    result.documents = ordered

    # Firma kartı: VKN başına en son onaylanan kayıt, yoksa en son kayıt.
    ranked = {}
    for doc in ordered:
        columns = result.columns.get(doc["id"])
        if not columns or not columns.vkn or not columns.header["satici_unvan"]:
            continue
        rank = (doc["status"] == "approved", doc["approved_at"] or "", doc["updated_at"])
        if columns.vkn not in ranked or rank > ranked[columns.vkn][0]:
            ranked[columns.vkn] = (rank, (columns.header["satici_unvan"], columns.header["satici_adres"]))
    result.firms = {vkn: value for vkn, (_, value) in ranked.items()}

    seen = set()
    for number in source.reference.get("baseline_invoice_numbers", []):
        key = words(number)
        if not key:
            problems.append("Başlangıç fatura numaralarından biri boş.")
        elif key in seen:
            result.notes.append("Yazım farkı dışında aynı olan bir başlangıç fatura numarası tek kayda indirildi.")
        else:
            seen.add(key)
            result.baseline.append((number, key))
    return result


def _check_duplicates(documents, problems):
    for doc in documents.values():
        if doc["status"] != "duplicate":
            continue
        target = documents.get(doc["duplicate_of"] or "")
        if target is None:
            problems.append(f"{doc['id']}: mükerrer kaydın işaret ettiği kayıt yok.")
        elif target["status"] == "duplicate":
            problems.append(f"{doc['id']}: mükerrer kayıt başka bir mükerrer kayda işaret ediyor.")


def _resolve_conflicts(documents, result, conflicts):
    """Aynı numaralı aktif kayıtlar belgeler_fatura_no_tekil index'ini ihlal eder."""
    groups = {}
    for identifier, columns in result.columns.items():
        doc = documents[identifier]
        number = columns.header["fatura_no_anahtar"]
        if number and doc["status"] in ACTIVE:
            groups.setdefault(number, []).append(doc)
    for group in groups.values():
        if len(group) < 2:
            continue
        ids = ", ".join(doc["id"] for doc in group)
        approved = [doc for doc in group if doc["status"] == "approved"]
        if conflicts != "mukerrer":
            result.problems.append(f"Aynı fatura numaralı aktif kayıtlar: {ids}")
            continue
        if len(approved) > 1:
            result.problems.append(f"Aynı fatura numarası birden fazla onaylı kayıtta: {ids}")
            continue
        # Onaylı kayıt korunur; yoksa uygulamadaki kural: en eski kayıt asıldır.
        keeper = approved[0] if approved else min(group, key=lambda d: (d["created_at"], d["id"]))
        for doc in group:
            if doc is keeper:
                continue
            doc["status"] = "duplicate"
            doc["duplicate_of"] = keeper["id"]
            doc["error"] = str(DuplicateInvoice(keeper["id"], doc["edited"].get("invoice_number")))
            doc["revision"] += 1
            result.notes.append(f"{doc['id']}: mükerrer işaretlendi (asıl kayıt {keeper['id']}).")


# ----------------------------------------------------------------------
# Yazma (tek transaction)
# ----------------------------------------------------------------------


class SingleConnection:
    """PgStore / PgServices'in havuz arayüzü; taşıma transaction'ı içinde savepoint açarlar."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        yield self.conn


def write(conn, store, source, plan_):
    """Plandaki her şeyi yazar. Satır hataları toplanır; sorun varsa MigrationError."""
    from psycopg import Error
    from psycopg.rows import dict_row

    problems = []
    with conn.cursor(row_factory=dict_row) as cur:
        for table in MUST_BE_EMPTY:
            cur.execute(f"select exists (select 1 from {table}) as dolu")
            if cur.fetchone()["dolu"]:
                raise MigrationError([f"Hedef tablo boş değil: {table}"])
        cur.execute("set constraints fatura.ocr_calismalari_belge_id_fkey deferred")
        _write_reference(cur, source, plan_)
        for row in source.services:
            try:
                with conn.transaction():
                    _write_service(cur, row)
            except (Error, ValueError) as error:
                problems.append(f"Hizmet {row['id']}: {_describe(error)}")
        for vkn, (name, address) in plan_.firms.items():
            # Ortak kart başka bir projeden gelmiş olabilir; varsa değiştirilmez.
            cur.execute(
                "insert into core.firmalar (vkn, unvan, adres) values (%s, %s, %s) "
                "on conflict (vkn) where vkn is not null do nothing",
                (vkn, name, address),
            )
        for doc in plan_.documents:
            try:
                with conn.transaction():
                    _write_document(cur, store, source, plan_, doc)
            except (Error, ValueError) as error:
                problems.append(f"{doc['id']}: {_describe(error)}")
    if problems:
        raise MigrationError(problems)


def _write_reference(cur, source, plan_):
    reference = source.reference
    cur.execute(
        "insert into fatura.referans_aktarimi (id, kaynak_dosya, sayfa, kaynak_sha256, aktarim_tarihi, "
        "baz_tarihi, tutar_esasi, oran_esasi, kurum_kaynagi, takip_baslangici) "
        "values (1, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (id) do update set "
        "kaynak_dosya = excluded.kaynak_dosya, sayfa = excluded.sayfa, kaynak_sha256 = excluded.kaynak_sha256, "
        "aktarim_tarihi = excluded.aktarim_tarihi, baz_tarihi = excluded.baz_tarihi, "
        "tutar_esasi = excluded.tutar_esasi, oran_esasi = excluded.oran_esasi, "
        "kurum_kaynagi = excluded.kurum_kaynagi, takip_baslangici = excluded.takip_baslangici",
        (
            reference.get("source_name"), reference.get("sheet"), reference.get("source_sha256"),
            reference.get("imported_date"), reference.get("baseline_as_of"), reference.get("basis"),
            reference.get("rates_basis"), reference.get("institution_source"),
            _timestamp(source.tracking["started_at"]),
        ),
    )
    cur.executemany(
        "insert into fatura.referans_fatura_numaralari (fatura_no, fatura_no_anahtar) values (%s, %s)",
        plan_.baseline,
    )


def _write_service(cur, row):
    from fatura.supabase.services import PgServices

    excel = "source_row" in row
    cur.execute(
        "insert into fatura.hizmetler (eski_kod, kaynak, excel_satir, kurum, aciklama, para_birimi) "
        "values (%s, %s, %s, %s, %s, %s) returning id",
        (
            row["id"], "excel" if excel else "elle", row.get("source_row") if excel else None,
            row["institution"], row["description"], row.get("currency") or "TRY",
        ),
    )
    PgServices.write_service(cur, cur.fetchone()["id"], row, row.get("aliases", []))


def open_store(conn, cache):
    """Uygulamanın deposu, taşıma transaction'ı üzerinde (PDF önbelleği kullanılmaz)."""
    from fatura.supabase.store import PgStore

    return PgStore(cache, SingleConnection(conn), files=None, recover_processing=False)


def object_path(doc):
    """Bucket yolu belgenin oluşturulduğu ay: {yyyy}/{mm}/{uuid}.pdf (PgStore._upload ile aynı)."""
    return f"{_timestamp(doc['created_at']):%Y/%m}/{uuid.UUID(hex=doc['id'])}.pdf"


def _write_document(cur, store, source, plan_, doc):
    from psycopg.types.json import Jsonb

    key = uuid.UUID(hex=doc["id"])
    runs = source.analyses.get(doc["id"], [])
    run_ids = []
    for run in runs:
        metrics = run["data"].get("metrics") or {}
        stamp = _timestamp(run["created_at"])
        cur.execute(
            "insert into fatura.ocr_calismalari (belge_id, motor, sure_saniye, ham_cikti, created_at, updated_at) "
            "values (%s, %s, %s, %s, %s, %s) returning id",
            (key, run["data"].get("reader"), metrics.get("elapsed_seconds"), Jsonb(run["data"]), stamp, stamp),
        )
        run_ids.append(cur.fetchone()["id"])

    values = {}
    columns = plan_.columns.get(doc["id"])
    source_run = None
    if doc["edited"] is not None:
        # Kaydetme, ilk okumanın kanıtını taşır (PgStore.save); okuma sonrası kayıt son çalıştırmadır.
        source_run = run_ids[-1] if doc["edited"] == runs[-1]["data"] else run_ids[0]
        values = store._header(cur, columns, doc["edited"].get("service_id"), approve=False)
    values |= {
        "id": key,
        "dosya_adi": doc["filename"],
        "mime_turu": doc["mime"],
        "belge_yolu": object_path(doc),
        "durum": doc["status"],
        "revizyon": doc["revision"],
        "hata_mesaji": doc["error"],
        "mukerrer_belge_id": uuid.UUID(hex=doc["duplicate_of"]) if doc["duplicate_of"] else None,
        "grup_id": uuid.UUID(hex=doc["batch_id"]) if doc["batch_id"] else None,
        "grup_sayfa": doc["batch_page"],
        "grup_sayfa_sayisi": doc["batch_count"],
        "kaynak_ocr_id": source_run,
        "onaylanma_zamani": _timestamp(doc["approved_at"]),
        "created_at": _timestamp(doc["created_at"]),
        "updated_at": _timestamp(doc["updated_at"]),
    }
    names = list(values)
    cur.execute(
        f"insert into fatura.belgeler ({', '.join(names)}) values ({', '.join(f'%({n})s' for n in names)})",
        values,
    )
    if columns:
        store._replace_items(cur, key, columns.items)
    for approval in source.approvals.get(doc["id"], []):
        stamp = _timestamp(approval["approved_at"])
        cur.execute(
            "insert into fatura.onaylar (belge_id, onaylanma_zamani, kayit, kontroller, created_at, updated_at) "
            "values (%s, %s, %s, %s, %s, %s)",
            (key, stamp, Jsonb(approval["data"]), Jsonb(approval["checks"]), stamp, stamp),
        )


def _describe(error):
    """Veritabanı hatasının yalnızca kodu ve kısıt adı; DETAIL satırı değer içerebilir."""
    diag = getattr(error, "diag", None)
    if diag is not None:
        parts = [error.sqlstate or "", diag.constraint_name or diag.column_name or ""]
        return "veritabanı hatası " + " ".join(p for p in parts if p)
    return str(error)


def _timestamp(value):
    return datetime.fromisoformat(value) if value else None


# ----------------------------------------------------------------------
# Doğrulama: uygulamanın okuma koduyla geri oku, kaynakla karşılaştır
# ----------------------------------------------------------------------


def normalized(document):
    """Karşılaştırma biçimi: zamanlar datetime, sayılar Decimal ('10.50' = '10.5')."""
    document = deepcopy(document)
    for key in TIMESTAMPS:
        if document.get(key):
            document[key] = _timestamp(document[key])
    for name in ("edited", "original"):
        record = document.get(name)
        if not record:
            continue
        if record.get("service_id") is None:
            record.pop("service_id", None)
        # Eksik anahtar ile None aynı anlamdadır (uygulama .get ile okur); kolonlar hep döner.
        seller = record.get("seller") or {}
        record["seller"] = {key: seller.get(key) for key in ("name", "tax_id", "address")} | seller
        totals = record.setdefault("totals", {})
        for key, *_ in TOTALS:
            totals[key] = _decimal(totals.get(key))
        for item in record.get("items") or []:
            for key, *_ in ITEM_NUMBERS:
                item[key] = _decimal(item.get(key))
    return document


def _decimal(value):
    return Decimal(str(value)).normalize() if value not in (None, "") else None


def differences(expected, actual, prefix=""):
    """Farklı alanların adları (değerleri değil), en fazla iki düzey."""
    if isinstance(expected, dict) and isinstance(actual, dict) and prefix.count(".") < 2:
        names = []
        for key in sorted(set(expected) | set(actual)):
            if expected.get(key) != actual.get(key):
                names += differences(expected.get(key), actual.get(key), f"{prefix}{key}.")
        return names
    return [prefix.rstrip(".")] if expected != actual else []


def _service_view(row):
    """Oranlar Excel'den gelir, Postgres'te yeniden hesaplanır; ayrıca raporlanır."""
    row = {k: v for k, v in row.items() if k not in ("rates", "total_increase")}
    row.setdefault("currency", "TRY")
    row.setdefault("aliases", [])
    for key in ("single_date", "note", "service_type", "unit_price", "invoice_count"):
        row.setdefault(key, None)
    row["invoice_count"] = row["invoice_count"] or 0
    row["unit_price"] = _decimal(row["unit_price"])
    row["amounts"] = {year: _decimal(value) for year, value in row.get("amounts", {}).items()}
    # Boş ödeme listesi ile anahtarın olmaması aynıdır (services.py, app.js: || {}).
    row["yearly_payments"] = {
        year: payment | {"total": _decimal(payment["total"])}
        for year, payment in row.get("yearly_payments", {}).items()
    }
    return row


def verify(conn, store, source, plan_):
    """Taşınan veriyi uygulamanın okuma koduyla doğrular; (sorunlar, notlar, sayılar) döner."""
    from psycopg.rows import dict_row

    from fatura.supabase.services import PgServices

    problems, notes = [], []
    for doc in plan_.documents:
        stored = store.get(doc["id"], include_deleted=True)
        if stored is None:
            problems.append(f"{doc['id']}: taşınan kayıt okunamadı.")
            continue
        changed = differences(normalized(doc), normalized(stored))
        if changed:
            problems.append(f"{doc['id']}: farklı alanlar: {', '.join(changed)}")

    services = PgServices(SingleConnection(conn))
    stored_rows = {row["id"]: row for row in services.rows()}
    rate_changes = 0
    for row in source.services:
        stored = stored_rows.get(row["id"])
        if stored is None:
            problems.append(f"Hizmet {row['id']}: taşınan kayıt okunamadı.")
            continue
        changed = differences(_service_view(row), _service_view(stored))
        if changed:
            problems.append(f"Hizmet {row['id']}: farklı alanlar: {', '.join(changed)}")
        if "rates" in row and (
            {y: _decimal(v) for y, v in row["rates"].items()}
            != {y: _decimal(v) for y, v in stored["rates"].items()}
        ):
            rate_changes += 1
    if rate_changes:
        notes.append(f"{rate_changes} hizmetin Excel'deki zam oranı yeniden hesaplanan orandan farklı (bedeller aynı).")
    reference = services.reference()
    for key in ("baseline_invoice_numbers", "years"):
        expected = source.reference.get(key, [])
        if key == "baseline_invoice_numbers":
            expected = [number for number, _ in plan_.baseline]
        if [str(v) for v in reference.get(key, [])] != [str(v) for v in expected]:
            problems.append(f"Referans bilgisi farklı: {key}")
    if _timestamp(services.started_at()) != _timestamp(source.tracking["started_at"]):
        problems.append("Hizmet takibinin başlangıç zamanı farklı.")

    expected_counts = {
        "fatura.belgeler": len(plan_.documents),
        "fatura.kalemler": sum(len(c.items) for c in plan_.columns.values()),
        "fatura.ocr_calismalari": sum(len(source.analyses.get(d["id"], [])) for d in plan_.documents),
        "fatura.onaylar": sum(len(source.approvals.get(d["id"], [])) for d in plan_.documents),
        "fatura.hizmetler": len(source.services),
        "fatura.hizmet_takma_adlari": sum(len(r.get("aliases", [])) for r in source.services),
    }
    totals = {}
    for doc in plan_.documents:
        columns = plan_.columns.get(doc["id"])
        if doc["status"] == "approved" and columns and columns.header["toplam_brut"] is not None:
            currency = columns.header["para_birimi"]
            totals[currency] = totals.get(currency, Decimal(0)) + columns.header["toplam_brut"]
    with conn.cursor(row_factory=dict_row) as cur:
        for table, count in expected_counts.items():
            cur.execute(f"select count(*) as n from {table}")
            if cur.fetchone()["n"] != count:
                problems.append(f"{table}: satır sayısı kaynakla tutmuyor.")
        cur.execute(
            "select para_birimi, sum(toplam_brut) as toplam from fatura.belgeler "
            "where durum = 'approved' and toplam_brut is not null group by para_birimi"
        )
        stored_totals = {row["para_birimi"]: row["toplam"] for row in cur.fetchall()}
    if stored_totals != totals:
        problems.append("Onaylı faturaların para birimi bazında brüt toplamı kaynakla tutmuyor.")
    return problems, notes, expected_counts


# ----------------------------------------------------------------------
# PDF yükleme
# ----------------------------------------------------------------------


def upload_files(files, source, documents):
    """Var olan nesnenin üzerine yazılmaz; her nesne indirilip SHA-256 ile karşılaştırılır.

    Commit'ten önce çalışır: yarıda kalırsa veritabanına hiçbir şey yazılmamış olur ve
    betik yeniden çalıştırıldığında önceden yüklenen nesneler doğrulanıp kullanılır.
    """
    from fatura.supabase.files import StorageError

    problems, reused = [], 0
    with tempfile.TemporaryDirectory(prefix="fatura-tasima-") as work:
        for doc in documents:
            path = object_path(doc)
            data = source.pdf(doc["id"]).read_bytes()
            try:
                files.upload(path, data)
            except StorageError as error:
                # Storage sürümüne göre "zaten var" 409 veya gövdesinde Duplicate olan 400 döner.
                if not (error.status == 409 or (error.status == 400 and "Duplicate" in str(error))):
                    problems.append(f"{doc['id']}: PDF yüklenemedi ({error.status or 'bağlantı'}).")
                    continue
                reused += 1
            copy = files.download(path, Path(work) / doc["id"])
            if hashlib.sha256(copy.read_bytes()).digest() != hashlib.sha256(data).digest():
                problems.append(f"{doc['id']}: bucket'taki PDF yerel dosyayla aynı değil.")
            copy.unlink()
    return problems, reused


# ----------------------------------------------------------------------
# Komut satırı
# ----------------------------------------------------------------------


def run(data_dir, mode, conflicts, database_url=None, files=None, exclude=(), out=print):
    """mode: 'kontrol' | 'prova' | 'uygula'. Başarıda 0, sorun varsa MigrationError."""
    source = read_source(data_dir, exclude)
    if exclude:
        out(f"Hariç tutulan: {len(exclude)} belge")
    out(
        f"Kaynak: {len(source.documents)} belge, "
        f"{sum(map(len, source.analyses.values()))} OCR çalıştırması, "
        f"{sum(map(len, source.approvals.values()))} onay, {len(source.services)} hizmet, "
        f"{len(source.reference.get('baseline_invoice_numbers', []))} başlangıç fatura no"
    )
    plan_ = plan(source, conflicts)
    for note in plan_.notes:
        out(f"  not: {note}")
    if plan_.problems:
        raise MigrationError(plan_.problems)
    out(f"Ön kontrol temiz. {len(plan_.firms)} firma kartı, {len(plan_.documents)} belge yazılacak.")
    if mode == "kontrol":
        return 0

    import psycopg

    conn = psycopg.connect(
        database_url, autocommit=True, prepare_threshold=None, application_name="fatura-tasima"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="fatura-tasima-") as cache, conn.transaction():
            store = open_store(conn, cache)
            write(conn, store, source, plan_)
            problems, notes, counts = verify(conn, store, source, plan_)
            for note in notes:
                out(f"  not: {note}")
            if problems:
                raise MigrationError(problems)
            out("Yazıldı ve doğrulandı: " + ", ".join(f"{t.split('.')[1]} {n}" for t, n in counts.items()))
            if mode != "uygula":
                out("Prova: transaction geri alındı, veritabanına hiçbir şey yazılmadı.")
                raise psycopg.Rollback()
            problems, reused = upload_files(files, source, plan_.documents)
            if problems:
                raise MigrationError(problems)
            out(f"PDF'ler bucket'ta ve SHA-256 doğrulandı ({len(plan_.documents)} dosya, {reused} tanesi önceden yüklenmişti).")
        if mode == "uygula":
            out("Commit edildi. Taşıma tamamlandı.")
    finally:
        conn.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="SQLite + JSON verisini Supabase'e taşır.")
    parser.add_argument("--data-dir", required=True, type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prova", action="store_true", help="yaz, doğrula, geri al")
    group.add_argument("--uygula", action="store_true", help="yaz, doğrula, PDF'leri yükle, commit et")
    parser.add_argument("--cakismalar", choices=("dur", "mukerrer"), default="dur")
    parser.add_argument(
        "--haric", default="", help="taşınmayacak belge kimlikleri, virgülle (32 haneli hex)"
    )
    args = parser.parse_args(argv)
    exclude = tuple(sorted({item.strip() for item in args.haric.split(",") if item.strip()}))
    mode = "uygula" if args.uygula else "prova" if args.prova else "kontrol"

    database_url, files = None, None
    if mode != "kontrol":
        database_url = os.environ.get("FATURA_DATABASE_URL")
        if not database_url:
            parser.error("FATURA_DATABASE_URL tanımlı değil.")
    if mode == "uygula":
        from fatura.supabase.files import SupabaseFiles

        files = SupabaseFiles(
            os.environ.get("SUPABASE_URL"),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
            os.environ.get("FATURA_STORAGE_BUCKET", "fatura-belgeler"),
        )
    try:
        return run(args.data_dir, mode, args.cakismalar, database_url, files, exclude)
    except MigrationError as error:
        print(error, file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
