"""Hizmet ve zam analizi verisinin Postgres sürümü.

Eşleştirme, analiz ve doğrulama mantığı fatura.analysis.services.Services'ten gelir;
bu sınıf yalnızca kalıcılık metotlarını (rows, reference, save_service ...) değiştirir.
Satırlar JSON sürümüyle aynı biçimde üretilir; hizmet kimliği eski_kod
(excel-row-N / service-<hex>) varsa odur, yoksa uuid metnidir.
"""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import uuid

from psycopg.rows import dict_row

from fatura.analysis.services import Services, ratio
from fatura.supabase.columns import fmt
from fatura.supabase.store import iso


class PgServices(Services):
    def __init__(self, pool):
        # Services.__init__ JSON dosyası oluşturur; burada çağrılmaz.
        self.pool = pool
        with self.cursor() as cur:
            # Takip başlangıcı ilk açılışta kaydedilir (service-tracking.json started_at karşılığı).
            cur.execute(
                "insert into fatura.referans_aktarimi (id, takip_baslangici) values (1, now()) "
                "on conflict (id) do nothing"
            )

    @contextmanager
    def cursor(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    yield cur

    # ------------------------------------------------------------------
    # Okuma
    # ------------------------------------------------------------------

    def started_at(self):
        with self.cursor() as cur:
            cur.execute("select takip_baslangici from fatura.referans_aktarimi where id = 1")
            return iso(cur.fetchone()["takip_baslangici"])

    def reference(self):
        with self.cursor() as cur:
            cur.execute("select * from fatura.referans_aktarimi where id = 1")
            info = cur.fetchone() or {}
            cur.execute(
                "select fatura_no from fatura.referans_fatura_numaralari where aktarim_id = 1 order by id"
            )
            numbers = [row["fatura_no"] for row in cur.fetchall()]
            cur.execute("select distinct yil from fatura.hizmet_yillik_bedeller order by yil")
            years = [row["yil"] for row in cur.fetchall()]
        reference = {"years": years, "baseline_invoice_numbers": numbers, "rows": []}
        for key, column in (
            ("source_name", "kaynak_dosya"),
            ("sheet", "sayfa"),
            ("source_sha256", "kaynak_sha256"),
            ("basis", "tutar_esasi"),
            ("rates_basis", "oran_esasi"),
            ("institution_source", "kurum_kaynagi"),
        ):
            if info.get(column) is not None:
                reference[key] = info[column]
        for key, column in (("imported_date", "aktarim_tarihi"), ("baseline_as_of", "baz_tarihi")):
            if info.get(column) is not None:
                reference[key] = info[column].isoformat()
        return reference

    def rows(self):
        with self.cursor() as cur:
            cur.execute(
                "select * from fatura.hizmetler "
                "order by (kaynak = 'excel') desc, excel_satir, created_at, id"
            )
            services = cur.fetchall()
            children = {}
            for table, order in (
                ("hizmet_yillik_bedeller", "yil"),
                ("hizmet_yillik_odemeler", "yil"),
                ("hizmet_takma_adlari", "id"),
            ):
                cur.execute(f"select * from fatura.{table} order by hizmet_id, {order}")
                for row in cur.fetchall():
                    children.setdefault((table, row["hizmet_id"]), []).append(row)
        return [self._row(service, children) for service in services]

    @staticmethod
    def _row(service, children):
        key = service["id"]
        amounts = {
            str(row["yil"]): fmt(row["bedel"])
            for row in children.get(("hizmet_yillik_bedeller", key), [])
        }
        series = [value for _, value in sorted(amounts.items()) if value is not None]
        row = {
            "id": service["eski_kod"] or str(key),
            "description": service["aciklama"],
            "institution": service["kurum"],
            "currency": service["para_birimi"],
            "amounts": amounts,
            "rates": {year: ratio(amounts.get(str(int(year) - 1)), value) for year, value in amounts.items()},
            "total_increase": ratio(series[0], series[-1]) if len(series) > 1 else None,
            "invoice_count": service["baslangic_fatura_sayisi"],
            "unit_price": fmt(service["birim_fiyat"]),
            "service_type": service["hizmet_turu"],
            "single_date": service["tek_adet_tarihi"].isoformat() if service["tek_adet_tarihi"] else None,
            "note": service["notu"],
            "aliases": [a["takma_ad"] for a in children.get(("hizmet_takma_adlari", key), [])],
        }
        if service["excel_satir"] is not None:
            row["source_row"] = service["excel_satir"]
        # Tarih anahtarının varlığı anlam taşır: yoksa dönem nottan çıkarılır.
        for flag, column, name in (
            ("baslangic_elle", "baslangic_tarihi", "start_date"),
            ("bitis_elle", "bitis_tarihi", "end_date"),
        ):
            if service[flag]:
                row[name] = service[column].isoformat() if service[column] else None
        payments = children.get(("hizmet_yillik_odemeler", key), [])
        if payments:
            row["yearly_payments"] = {
                str(p["yil"]): {
                    "invoice_count": p["fatura_sayisi"],
                    "total": fmt(p["toplam"]),
                    "through_date": p["kapsanan_son_tarih"].isoformat(),
                }
                for p in payments
            }
        return row

    # ------------------------------------------------------------------
    # Yazma (Services.update / create / add_alias tarafından çağrılır)
    # ------------------------------------------------------------------

    def new_service_id(self):
        return str(uuid.uuid4())

    def insert_service(self, row):
        with self.cursor() as cur:
            cur.execute(
                "insert into fatura.hizmetler (id, kaynak, kurum, aciklama, para_birimi, hizmet_turu) "
                "values (%s, 'elle', %s, %s, %s, %s)",
                (uuid.UUID(row["id"]), row["institution"], row["description"], row["currency"], row["service_type"]),
            )

    def save_service(self, identifier, updated, aliases):
        with self.cursor() as cur:
            self.write_service(cur, self._key(cur, identifier), updated, aliases)

    @classmethod
    def write_service(cls, cur, key, updated, aliases):
        """Hizmet satırını ve alt tablolarını yazar; veri taşıması da bunu kullanır."""
        cur.execute(
            "update fatura.hizmetler set kurum = %s, aciklama = %s, hizmet_turu = %s, "
            "tek_adet_tarihi = %s, baslangic_tarihi = %s, baslangic_elle = %s, "
            "bitis_tarihi = %s, bitis_elle = %s, notu = %s, baslangic_fatura_sayisi = %s, "
            "birim_fiyat = %s where id = %s",
            (
                updated["institution"],
                updated["description"],
                updated.get("service_type"),
                _date(updated.get("single_date")),
                _date(updated.get("start_date")),
                "start_date" in updated,
                _date(updated.get("end_date")),
                "end_date" in updated,
                updated.get("note"),
                updated.get("invoice_count") or 0,
                _money(updated.get("unit_price"), 4, "Birim fiyat"),
                key,
            ),
        )
        cur.execute("delete from fatura.hizmet_yillik_bedeller where hizmet_id = %s", (key,))
        cur.executemany(
            "insert into fatura.hizmet_yillik_bedeller (hizmet_id, yil, bedel) values (%s, %s, %s)",
            [
                (key, int(year), _money(value, 2, f"{year} hizmet bedeli"))
                for year, value in sorted(updated.get("amounts", {}).items())
            ],
        )
        cur.execute("delete from fatura.hizmet_yillik_odemeler where hizmet_id = %s", (key,))
        cur.executemany(
            "insert into fatura.hizmet_yillik_odemeler "
            "(hizmet_id, yil, fatura_sayisi, toplam, kapsanan_son_tarih) values (%s, %s, %s, %s, %s)",
            [
                (
                    key,
                    int(year),
                    payment["invoice_count"],
                    _money(payment["total"], 2, f"{year} ödeme toplamı"),
                    _date(payment["through_date"]),
                )
                for year, payment in sorted(updated.get("yearly_payments", {}).items())
            ],
        )
        cls._replace_aliases(cur, key, aliases)

    def save_aliases(self, identifier, aliases):
        with self.cursor() as cur:
            self._replace_aliases(cur, self._key(cur, identifier), aliases)

    @staticmethod
    def _key(cur, identifier):
        cur.execute(
            "select id from fatura.hizmetler where eski_kod = %s or id::text = %s",
            (identifier, identifier),
        )
        row = cur.fetchone()
        if row is None:
            raise FileNotFoundError("Hizmet bulunamadı.")
        return row["id"]

    @staticmethod
    def _replace_aliases(cur, key, aliases):
        cur.execute("delete from fatura.hizmet_takma_adlari where hizmet_id = %s", (key,))
        cur.executemany(
            "insert into fatura.hizmet_takma_adlari (hizmet_id, takma_ad) values (%s, %s)",
            [(key, alias) for alias in aliases],
        )


def _date(value):
    return date.fromisoformat(value) if value else None


def _money(value, scale, label):
    """Hizmet ekranı 6 ondalığa izin verir; kolon hassasiyeti aşılırsa yuvarlamadan reddedilir."""
    if value is None:
        return None
    number = Decimal(value)
    if number.as_tuple().exponent < -scale:
        raise ValueError(f"{label} en fazla {scale} ondalık basamak içerebilir.")
    return number
