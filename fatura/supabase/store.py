"""Supabase (PostgreSQL + Storage) belge deposu; fatura.storage.Store ile aynı arayüz.

Uygulama katmanı (fatura.web.app) iki depoyu ayırt etmez:
  * Kimlikler dışarıya 32 haneli hex olarak verilir (uuid.hex), içeride uuid saklanır.
  * Zamanlar UTC ISO metni olarak döner (SQLite deposuyla aynı biçim).
  * "edited" kaydı kolonlardan ve kaynak OCR çalıştırmasından yeniden kurulur;
    "original" ilk OCR çalıştırmasının ham çıktısıdır.
PDF'ler Storage bucket'ında tutulur; OCR ve önizleme için yerel önbelleğe
(data/files/{id}/source) indirilir.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil
import uuid

from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fatura.storage import MANUAL, DuplicateInvoice
from fatura.supabase.columns import Columns, TOTALS, fmt, item_record

# Aynı fatura numarasının iki aktif kayda yazılmasını engelleyen işlem kilidi
# (SQLite'taki BEGIN IMMEDIATE karşılığı). Unique index ek güvencedir.
LOG = logging.getLogger("fatura")
DUPLICATE_LOCK ="select pg_advisory_xact_lock(hashtext('fatura.belgeler.fatura_no'))"

HEADER = (
    "satici_unvan", "satici_vkn", "satici_adres", "firma_id", "fatura_no",
    "fatura_no_anahtar", "fatura_tarihi", "para_birimi", "fatura_notu", "hizmet_id",
    "toplam_net", "toplam_kdv", "toplam_brut", "toplam_iskonto", "toplam_tevkifat",
    "odenecek_tutar", "inceleme_gerekli", "uyarilar", "kontroller",
)
ITEM_COLUMNS = (
    "sira", "ad", "aciklama", "birim", "miktar", "birim_fiyat", "kdv_orani",
    "satir_toplami", "iskonto", "fiyat_esasi", "toplam_esasi", "kanit",
)
SELECT_DOCUMENT = """
    select b.*, coalesce(h.eski_kod, h.id::text) as hizmet_kodu
    from fatura.belgeler b
    left join fatura.hizmetler h on h.id = b.hizmet_id
"""


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat() if value else None


def uid(identifier):
    """Dış kimlik (32 hex) → uuid; geçersiz kimlik kayıt bulunamadı demektir."""
    try:
        return uuid.UUID(hex=identifier)
    except (TypeError, ValueError):
        raise FileNotFoundError("Kayıt bulunamadı.") from None


class PgStore:
    def __init__(self, root, pool, files, recover_processing=True, prevent_duplicates=True):
        self.root = Path(root)
        self.pool = pool
        self.files = files
        self.prevent_duplicates = prevent_duplicates
        (self.root / "files").mkdir(parents=True, exist_ok=True)
        if recover_processing:
            # Yarım kalan işlemler sessizce kaybolmaz; inceleme için tekrar erişilebilir kalır.
            with self.cursor() as cur:
                cur.execute(
                    "update fatura.belgeler set durum = 'review', hata_mesaji = %s "
                    "where durum = 'processing'",
                    ("İşlem tamamlanmadan uygulama kapandı. Yeniden analiz edebilirsiniz.",),
                )

    @contextmanager
    def cursor(self):
        """Tek transaction: blok hatasız biterse commit, aksi hâlde rollback."""
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    yield cur

    # ------------------------------------------------------------------
    # Okuma
    # ------------------------------------------------------------------

    def get(self, identifier, include_deleted=False):
        """include_deleted yalnızca taşıma doğrulaması içindir; uygulama silineni görmez."""
        key = uid(identifier)
        hidden = "" if include_deleted else " and b.durum <> 'deleted'"
        with self.cursor() as cur:
            cur.execute(SELECT_DOCUMENT + " where b.id = %s" + hidden, (key,))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "select id, ham_cikti from fatura.ocr_calismalari where belge_id = %s "
                "order by created_at, id",
                (key,),
            )
            runs = cur.fetchall()
            items = self._items(cur, [key]).get(key, [])
        base = next((r["ham_cikti"] for r in runs if r["id"] == row["kaynak_ocr_id"]), None)
        if row["mime_turu"] == MANUAL:
            from fatura.extraction.service import empty_record

            base = empty_record()  # elle girilen kaydın OCR çıktısı yoktur
        document = self._document(row, items, base)
        document["original"] = runs[0]["ham_cikti"] if runs else None
        return document

    def list(self):
        """Liste ve analizler için hafif kayıtlar: kanıt ve OCR sayfa metni yüklenmez."""
        with self.cursor() as cur:
            cur.execute(
                SELECT_DOCUMENT
                + " where b.durum not in ('deleted', 'duplicate') order by b.created_at desc, b.id"
            )
            rows = cur.fetchall()
            items = self._items(cur, [row["id"] for row in rows])
        documents = []
        for row in rows:
            document = self._document(row, items.get(row["id"], []), {})
            document["original"] = None
            documents.append(document)
        return documents

    def batch(self, identifier):
        with self.cursor() as cur:
            cur.execute(
                "select id, dosya_adi, durum, grup_sayfa from fatura.belgeler "
                "where grup_id = %s and durum <> 'deleted' order by grup_sayfa",
                (uid(identifier),),
            )
            return [
                {"id": r["id"].hex, "filename": r["dosya_adi"], "status": r["durum"], "batch_page": r["grup_sayfa"]}
                for r in cur.fetchall()
            ]

    def file(self, identifier):
        """Yerel önbellekteki PDF; yoksa bucket'tan indirilir."""
        path = self.root / "files" / uid(identifier).hex / "source"
        if not path.exists():
            with self.cursor() as cur:
                cur.execute("select belge_yolu from fatura.belgeler where id = %s", (uid(identifier),))
                row = cur.fetchone()
            if row is None:
                raise FileNotFoundError("Kayıt bulunamadı.")
            if row["belge_yolu"] is None:
                raise FileNotFoundError("Bu fatura elle girildi; belgesi yok.")
            self.files.download(row["belge_yolu"], path)
        return path

    @staticmethod
    def _items(cur, keys):
        if not keys:
            return {}
        cur.execute(
            f"select belge_id, {', '.join(ITEM_COLUMNS)} from fatura.kalemler "
            "where belge_id = any(%s) order by belge_id, sira",
            (list(keys),),
        )
        grouped = {}
        for row in cur.fetchall():
            grouped.setdefault(row["belge_id"], []).append(item_record(row))
        return grouped

    @staticmethod
    def _document(row, items, base):
        document = {
            "id": row["id"].hex,
            "filename": row["dosya_adi"],
            "mime": row["mime_turu"],
            "status": row["durum"],
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
            "approved_at": iso(row["onaylanma_zamani"]),
            "revision": row["revizyon"],
            "error": row["hata_mesaji"],
            "duplicate_of": row["mukerrer_belge_id"].hex if row["mukerrer_belge_id"] else None,
            "batch_id": row["grup_id"].hex if row["grup_id"] else None,
            "batch_page": row["grup_sayfa"],
            "batch_count": row["grup_sayfa_sayisi"],
            "edited": None,
        }
        if row["kaynak_ocr_id"] is None and row["mime_turu"] != MANUAL:
            return document  # henüz okunmadı
        record = deepcopy(base) if base else {}
        record.update(
            {
                "seller": {
                    "name": row["satici_unvan"],
                    "tax_id": row["satici_vkn"],
                    "address": row["satici_adres"],
                },
                "invoice_number": row["fatura_no"],
                "invoice_date": row["fatura_tarihi"].isoformat() if row["fatura_tarihi"] else None,
                "currency": row["para_birimi"],
                "invoice_note": row["fatura_notu"],
                "service_id": row["hizmet_kodu"],
                "totals": {key: fmt(row[column]) for key, column, *_ in TOTALS},
                "items": items,
                "needs_review": row["inceleme_gerekli"],
                "issues": row["uyarilar"],
                "checks": row["kontroller"],
            }
        )
        document["edited"] = record
        return document

    # ------------------------------------------------------------------
    # Yazma
    # ------------------------------------------------------------------

    def create(self, identifier, filename, mime):
        """PDF önce bucket'a yüklenir; kayıt eklenemezse yüklenen nesne silinir."""
        key = uid(identifier)
        path = self._upload(key)
        try:
            with self.cursor() as cur:
                cur.execute(
                    "insert into fatura.belgeler (id, dosya_adi, mime_turu, belge_yolu, durum) "
                    "values (%s, %s, %s, %s, 'processing')",
                    (key, filename, mime, path),
                )
        except Exception:
            self._discard([path])
            raise
        return self.get(identifier)

    def create_manual(self, identifier, filename, record):
        """Belgesiz, elle girilen fatura: bucket'a dosya gitmez, doğrudan incelemede açılır."""
        columns = Columns(record, strict=True)
        key = uid(identifier)
        with self.cursor() as cur:
            values = self._header(cur, columns, record.get("service_id"), approve=False)
            values |= {"id": key, "dosya_adi": filename, "mime_turu": MANUAL, "durum": "review"}
            names = list(values)
            cur.execute(
                f"insert into fatura.belgeler ({', '.join(names)}) "
                f"values ({', '.join(f'%({n})s' for n in names)})",
                values,
            )
            self._replace_items(cur, key, columns.items)
        return self.get(identifier)

    def create_batch(self, documents, batch_id, replaced=None):
        """Sayfalar ve varsa birleşik kaydın kaldırılması tek işlemde tamamlanır."""
        uploaded = []
        try:
            for document in documents:
                uploaded.append(self._upload(uid(document["id"])))
            with self.cursor() as cur:
                if replaced:
                    identifier, revision = replaced
                    cur.execute(
                        "update fatura.belgeler set durum = 'deleted', revizyon = revizyon + 1 "
                        "where id = %s and revizyon = %s and durum not in ('processing', 'deleted')",
                        (uid(identifier), revision),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("Kayıt değişmiş veya halen okunuyor. Sayfayı yenileyin.")
                for index, (document, path) in enumerate(zip(documents, uploaded), start=1):
                    cur.execute(
                        "insert into fatura.belgeler (id, dosya_adi, mime_turu, belge_yolu, durum, "
                        "grup_id, grup_sayfa, grup_sayfa_sayisi) "
                        "values (%s, %s, 'application/pdf', %s, 'processing', %s, %s, %s)",
                        (uid(document["id"]), document["filename"], path, uid(batch_id), index, len(documents)),
                    )
        except Exception:
            self._discard(uploaded)
            raise
        return [self.get(document["id"]) for document in documents]

    def analyzed(self, identifier, record, error=None):
        columns = Columns(record, strict=False)
        key = uid(identifier)
        metrics = record.get("metrics") or {}
        with self.cursor() as cur:
            cur.execute(DUPLICATE_LOCK)
            cur.execute(
                "insert into fatura.ocr_calismalari (belge_id, motor, sure_saniye, hata_mesaji, ham_cikti) "
                "values (%s, %s, %s, %s, %s) returning id",
                (key, record.get("reader"), metrics.get("elapsed_seconds"), error, Jsonb(record)),
            )
            run = cur.fetchone()["id"]
            duplicate = self._duplicate(cur, key, columns.header["fatura_no_anahtar"])
            values = self._header(cur, columns, record.get("service_id"), approve=False)
            message = str(DuplicateInvoice(duplicate.hex, record.get("invoice_number"))) if duplicate else error
            cur.execute(
                self._update_sql(
                    "durum = %(durum)s, mukerrer_belge_id = %(mukerrer)s, kaynak_ocr_id = %(kaynak)s, "
                    "hata_mesaji = %(hata)s",
                    "id = %(id)s and durum <> 'deleted'",
                ),
                values
                | {
                    "durum": "duplicate" if duplicate else "review",
                    "mukerrer": duplicate,
                    "kaynak": run,
                    "hata": message,
                    "id": key,
                },
            )
            if cur.rowcount == 1:
                self._replace_items(cur, key, columns.items)

    def save(self, identifier, record, revision, approved=False):
        """Eski sekmenin yeni düzeltmeleri ezmesini revizyon numarasıyla engeller."""
        columns = Columns(record, strict=True)
        key = uid(identifier)
        stamp = now()
        with self.cursor() as cur:
            cur.execute(DUPLICATE_LOCK)
            duplicate = self._duplicate(cur, key, columns.header["fatura_no_anahtar"])
            if duplicate:
                raise DuplicateInvoice(duplicate.hex, record.get("invoice_number"))
            values = self._header(cur, columns, record.get("service_id"), approve=approved)
            # Düzenlenen kayıt ilk okumanın kanıt ve sayfa metnini taşır (app.normalized_record).
            cur.execute(
                self._update_sql(
                    "durum = %(durum)s, onaylanma_zamani = %(onay)s, mukerrer_belge_id = null, "
                    "hata_mesaji = case when durum = 'duplicate' then null else hata_mesaji end, "
                    "kaynak_ocr_id = (select id from fatura.ocr_calismalari where belge_id = %(id)s "
                    "order by created_at, id limit 1)",
                    "id = %(id)s and revizyon = %(revizyon)s and durum in ('review', 'approved', 'duplicate')",
                ),
                values
                | {
                    "durum": "approved" if approved else "review",
                    "onay": stamp if approved else None,
                    "id": key,
                    "revizyon": revision,
                },
            )
            if cur.rowcount != 1:
                raise ValueError("Bu kayıt başka bir işlemde değişti. Sayfayı yenileyip tekrar deneyin.")
            self._replace_items(cur, key, columns.items)
            if approved:
                cur.execute(
                    "insert into fatura.onaylar (belge_id, onaylanma_zamani, kayit, kontroller) "
                    "values (%s, %s, %s, %s)",
                    (key, stamp, Jsonb(record), Jsonb(record.get("checks", []))),
                )
        return self.get(identifier)

    def retry(self, identifier):
        with self.cursor() as cur:
            cur.execute(
                "update fatura.belgeler set durum = 'processing', hata_mesaji = null, revizyon = revizyon + 1 "
                "where id = %s and durum = 'review' and mime_turu <> %s",
                (uid(identifier), MANUAL),
            )
            if cur.rowcount != 1:
                raise ValueError("Yalnızca inceleme bekleyen kayıtlar yeniden analiz edilebilir.")

    def discard(self, identifier):
        """Fatura içermeyen, henüz okunan belgeyi iz bırakmadan kaldırır (kalemler,
        OCR çalıştırmaları cascade ile silinir; bucket nesnesi ve yerel önbellek de)."""
        key = uid(identifier)
        try:
            with self.cursor() as cur:
                cur.execute(
                    "delete from fatura.belgeler where id = %s and durum = 'processing' returning belge_yolu",
                    (key,),
                )
                row = cur.fetchone()
        except InsufficientPrivilege:
            # 20261124100000_bos_sayfa_silme çalıştırılmamış: kayıt en azından listeden çıkar.
            LOG.warning("DELETE yetkisi yok; fatura içermeyen belge yumuşak silindi: %s", identifier)
            with self.cursor() as cur:
                cur.execute(
                    "update fatura.belgeler set durum = 'deleted', revizyon = revizyon + 1 "
                    "where id = %s and durum = 'processing'",
                    (key,),
                )
            return
        if row is None:
            return
        self._discard([row["belge_yolu"]])
        shutil.rmtree(self.root / "files" / key.hex, ignore_errors=True)

    def delete(self, identifier, revision):
        """Kaydı listeden çıkarır; ilk okuma, onay geçmişi ve PDF denetim için korunur."""
        with self.cursor() as cur:
            cur.execute(
                "update fatura.belgeler set durum = 'deleted', revizyon = revizyon + 1 "
                "where id = %s and revizyon = %s and durum not in ('processing', 'deleted')",
                (uid(identifier), revision),
            )
            if cur.rowcount != 1:
                raise ValueError("Kayıt değişmiş veya halen okunuyor. Listeyi yenileyip tekrar deneyin.")

    # ------------------------------------------------------------------
    # Yardımcılar
    # ------------------------------------------------------------------

    def _upload(self, key):
        """Yerel PDF'i {yıl}/{ay}/{id}.pdf yoluna yükler ve nesne yolunu döner."""
        source = self.root / "files" / key.hex / "source"
        path = f"{now():%Y/%m}/{key}.pdf"
        self.files.upload(path, source.read_bytes())
        return path

    def _discard(self, paths):
        try:
            self.files.delete(paths)
        except Exception:
            pass  # Kayıt oluşmadı; sahipsiz nesne bucket temizliğinde silinebilir.

    def _duplicate(self, cur, key, number_key):
        if not self.prevent_duplicates or not number_key:
            return None
        cur.execute(
            "select id from fatura.belgeler where id <> %s and fatura_no_anahtar = %s "
            "and durum in ('processing', 'review', 'approved') "
            "and (kaynak_ocr_id is not null or mime_turu = %s) "
            "order by created_at, id limit 1",
            (key, number_key, MANUAL),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def _header(self, cur, columns, service_id, approve):
        values = dict(columns.header)
        values["uyarilar"] = Jsonb(values["uyarilar"])
        values["kontroller"] = Jsonb(values["kontroller"])
        values["hizmet_id"] = self._service(cur, service_id)
        values["firma_id"] = self._company(cur, columns, approve)
        return values

    @staticmethod
    def _service(cur, service_id):
        if not service_id:
            return None
        cur.execute(
            "select id from fatura.hizmetler where eski_kod = %s or id::text = %s",
            (service_id, service_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("Seçilen hizmet bulunamadı.")
        return row["id"]

    @staticmethod
    def _company(cur, columns, approve):
        """VKN okunduysa firma kartına bağlar; onayda kartın unvan ve adresi güncellenir."""
        if not columns.vkn:
            return None
        name, address = columns.header["satici_unvan"], columns.header["satici_adres"]
        if name:
            action = "do update set unvan = excluded.unvan, adres = excluded.adres" if approve else "do nothing"
            cur.execute(
                "insert into core.firmalar (vkn, unvan, adres) values (%s, %s, %s) "
                f"on conflict (vkn) where vkn is not null {action}",
                (columns.vkn, name, address),
            )
        cur.execute("select id from core.firmalar where vkn = %s", (columns.vkn,))
        row = cur.fetchone()
        return row["id"] if row else None

    @staticmethod
    def _update_sql(extra, where):
        assignments = ", ".join(f"{column} = %({column})s" for column in HEADER)
        return (
            f"update fatura.belgeler set {assignments}, {extra}, revizyon = revizyon + 1 "
            f"where {where}"
        )

    @staticmethod
    def _replace_items(cur, key, items):
        cur.execute("delete from fatura.kalemler where belge_id = %s", (key,))
        if items:
            cur.executemany(
                f"insert into fatura.kalemler (belge_id, {', '.join(ITEM_COLUMNS)}) "
                f"values (%s, {', '.join(['%s'] * len(ITEM_COLUMNS))})",
                [
                    (key, *(Jsonb(item[c]) if c == "kanit" and item[c] is not None else item[c] for c in ITEM_COLUMNS))
                    for item in items
                ],
            )
