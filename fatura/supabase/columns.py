"""Fatura kaydı (JSON) ile fatura.belgeler / fatura.kalemler kolonları arasındaki dönüşüm.

Para ve miktar kolonlarının hassasiyeti sabittir (numeric(18,2) vb.). Postgres fazla
ondalığı sessizce yuvarlayacağı için değerler yazılmadan önce denetlenir:
  * Kullanıcı kaydı (strict=True): anlaşılır bir ValueError ile reddedilir.
  * OCR çıktısı (strict=False): değer boş bırakılır ve kayda kontrol uyarısı eklenir.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import re

from fatura.storage import invoice_key

# (kayıt anahtarı, kolon, toplam basamak, ondalık basamak, etiket)
TOTALS = (
    ("net", "toplam_net", 18, 2, "KDV hariç toplam"),
    ("vat", "toplam_kdv", 18, 2, "KDV toplamı"),
    ("gross", "toplam_brut", 18, 2, "KDV dahil toplam"),
    ("discount", "toplam_iskonto", 18, 2, "Toplam iskonto"),
    ("withholding", "toplam_tevkifat", 18, 2, "Tevkifat"),
    ("payable", "odenecek_tutar", 18, 2, "Ödenecek tutar"),
)
ITEM_NUMBERS = (
    ("quantity", "miktar", 18, 4, "miktar"),
    ("unit_price", "birim_fiyat", 18, 4, "birim fiyat"),
    ("vat_rate", "kdv_orani", 5, 2, "KDV oranı"),
    ("line_total", "satir_toplami", 18, 2, "satır toplamı"),
    ("discount", "iskonto", 18, 2, "iskonto"),
)
ITEM_TEXTS = (("name", "ad"), ("description", "aciklama"), ("unit", "birim"))
BASES = ("net", "gross", "unknown")


def fmt(value):
    """Decimal → kayıttaki metin biçimi ('8320.00' → '8320'); üstel gösterim üretmez."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def vkn(tax_id):
    """Boşlukları atılmış 10–11 haneli VKN/TCKN; değilse None (firma kartı açılmaz)."""
    digits = re.sub(r"\s", "", tax_id or "")
    return digits if re.fullmatch(r"[0-9]{10,11}", digits) else None


class Columns:
    """Bir kaydın kolon değerleri ve dönüşüm sırasında oluşan uyarılar."""

    def __init__(self, record, strict):
        self.strict = strict
        self.issues = []
        seller = record.get("seller") or {}
        totals = record.get("totals") or {}
        number = record.get("invoice_number")
        self.header = {
            "satici_unvan": seller.get("name"),
            "satici_vkn": seller.get("tax_id"),
            "satici_adres": seller.get("address"),
            "fatura_no": number,
            "fatura_no_anahtar": invoice_key(number) or None if number else None,
            "fatura_tarihi": self.date(record.get("invoice_date")),
            "para_birimi": self.currency(record.get("currency")),
            "fatura_notu": record.get("invoice_note"),
            "inceleme_gerekli": bool(record.get("needs_review", True)),
            "uyarilar": list(record.get("issues") or []),
            "kontroller": list(record.get("checks") or []),
        }
        if self.header["fatura_no_anahtar"] is None:
            self.header["fatura_no"] = None
        for key, column, digits, scale, label in TOTALS:
            self.header[column] = self.number(totals.get(key), digits, scale, label)
        self.items = [self.item(i, item) for i, item in enumerate(record.get("items") or [], 1)]
        self.vkn = vkn(seller.get("tax_id"))
        if self.issues:
            self.header["uyarilar"] += self.issues
            self.header["inceleme_gerekli"] = True

    def reject(self, message, field):
        if self.strict:
            raise ValueError(message)
        self.issues.append({"code": "storage_precision", "detail": f"{message} Alan boş bırakıldı: {field}."})

    def number(self, value, digits, scale, label, minimum=None, maximum=None):
        if value in (None, ""):
            return None
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            self.reject(f"{label} sayı olmalı.", label)
            return None
        if not number.is_finite():
            self.reject(f"{label} sayı olmalı.", label)
            return None
        if number.as_tuple().exponent < -scale:
            self.reject(f"{label} en fazla {scale} ondalık basamak içerebilir.", label)
            return None
        if abs(number) >= Decimal(10) ** (digits - scale):
            self.reject(f"{label} çok büyük.", label)
            return None
        if (minimum is not None and number <= minimum) or (maximum is not None and number > maximum):
            self.reject(f"{label} geçerli aralıkta değil.", label)
            return None
        return number

    def date(self, value):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            self.reject("Fatura tarihi YYYY-AA-GG biçiminde olmalı.", "fatura tarihi")
            return None

    def currency(self, value):
        if not value:
            return None
        if not re.fullmatch(r"[A-Z]{3}", value):
            self.reject("Para birimi TRY, USD gibi üç harfli kod olmalı.", "para birimi")
            return None
        return value

    def item(self, index, item):
        row = {"sira": index}
        for key, column in ITEM_TEXTS:
            row[column] = item.get(key)
        for key, column, digits, scale, label in ITEM_NUMBERS:
            limits = {"minimum": Decimal(0)} if key == "quantity" else {}
            if key == "vat_rate":
                limits = {"maximum": Decimal(100)}
            value = self.number(item.get(key), digits, scale, f"{index}. kalem {label}", **limits)
            if key == "vat_rate" and value is not None and value < 0:
                self.reject(f"{index}. kalem KDV oranı geçerli aralıkta değil.", f"{index}. kalem KDV oranı")
                value = None
            row[column] = value
        row["fiyat_esasi"] = item.get("price_basis") if item.get("price_basis") in BASES else "unknown"
        row["toplam_esasi"] = item.get("total_basis") if item.get("total_basis") in BASES else "unknown"
        row["kanit"] = item.get("evidence")
        return row


def item_record(row):
    """fatura.kalemler satırı → kayıttaki kalem sözlüğü."""
    item = {key: row[column] for key, column in ITEM_TEXTS}
    for key, column, *_ in ITEM_NUMBERS:
        item[key] = fmt(row[column])
    item["price_basis"] = row["fiyat_esasi"]
    item["total_basis"] = row["toplam_esasi"]
    item["evidence"] = row["kanit"]
    return item
