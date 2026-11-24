"""Sabit tedarikçi koordinatları yerine belge başlıklarından okuma bölgeleri bulur."""

import re
from statistics import median

from fatura.document_notes import normalized, text_rows

AMOUNT = re.compile(r"^\d[\d.,]*\d(?:\s*(?:TL|TRY|EUR|USD))?$")
DATE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}")


def page_from_ocr(ocr):
    return {
        "page": 1,
        "width": ocr["width"],
        "height": ocr["height"],
        "method": ocr.get("engine", "ocr"),
        "lines": [
            {"text": w["text"], "page": 1, "bbox": w["bbox"]} for w in ocr["lines"]
        ],
    }


def dynamic_regions(ocr):
    """Etiketleri ve tablo sütunlarını sayfanın gerçek konumlarından çıkarır.

    Başlık tanınmıyorsa o alan boş kalır. Fatura numarasındaki karakterler,
    miktarlar ve tutarlar bu aşamada değiştirilmez veya hesapla tamamlanmaz.
    """
    page = page_from_ocr(ocr)
    words = page["lines"]
    compounds = {
        "BIRIMFIYAT": ["Birim", "Fiyat"],
        "MALHIZMET": ["Mal", "Hizmet"],
        "MALHIZMETTUTARI": ["Mal", "Hizmet", "Tutarı"],
        "KDVTUTARI": ["KDV", "Tutarı"],
        "DIGERVERGILER": ["Diğer", "Vergiler"],
    }
    expanded = []
    for word in words:
        # Tarama çizgisi etiketle tutarı birleştirebilir: Tutarı|4.120,00.
        joined = re.fullmatch(r"([A-Za-zÇĞİÖŞÜçğıöşü]+)[|:](\d[\d.,]*)", word["text"])
        if joined:
            b = word["bbox"]
            split = b[0] + (b[2] - b[0]) * (len(joined[1]) + 1) / len(word["text"])
            expanded.extend(
                [
                    {"text": joined[1], "page": 1, "bbox": [b[0], b[1], split, b[3]]},
                    {"text": joined[2], "page": 1, "bbox": [split, b[1], b[2], b[3]]},
                ]
            )
            continue
        key = re.sub(r"[^A-Z]", "", normalized(word["text"]))
        parts = compounds.get(key)
        if parts:
            b = word["bbox"]
            length = sum(len(part) for part in parts)
            start = b[0]
            for part in parts:
                end = start + (b[2] - b[0]) * len(part) / length
                expanded.append(
                    {"text": part, "page": 1, "bbox": [start, b[1], end, b[3]]}
                )
                start = end
        else:
            expanded.append(word)
    words = expanded
    page["lines"] = words
    rows = text_rows(page)
    width, height = page["width"], page["height"]
    fields = {}
    typical_height = (
        median(max(1, w["bbox"][3] - w["bbox"][1]) for w in words) if words else 10
    )
    warnings = []

    def box(bounds, padding=3):
        x0, y0, x1, y1 = bounds
        return [
            max(0, (x0 - padding) / width),
            max(0, (y0 - padding) / height),
            min(1, (x1 + padding) / width),
            min(1, (y1 + padding) / height),
        ]

    def row_words(row):
        b = row["bbox"]
        return [
            w
            for w in words
            if b[0] - 1 <= (w["bbox"][0] + w["bbox"][2]) / 2 <= b[2] + 1
            and b[1] - 1 <= (w["bbox"][1] + w["bbox"][3]) / 2 <= b[3] + 1
        ]

    def value_box(row, pattern):
        candidates = [w for w in row_words(row) if pattern(w["text"])]
        if not candidates:
            return None
        value = max(candidates, key=lambda w: w["bbox"][0])
        bounds = list(value["bbox"])
        for word in row_words(row):
            if (
                word["text"] in ("TL", "TRY", "EUR", "USD")
                and 0 <= word["bbox"][0] - bounds[2] < width * 0.035
            ):
                bounds[2] = max(bounds[2], word["bbox"][2])
        return box(bounds)

    # Satıcı unvanını SAYIN/ALICI bloğundan önceki şirket satırında ararız.
    buyer_top = min(
        (
            r["bbox"][1]
            for r in rows
            if normalized(r["text"]).startswith(("SAYIN", "ALICI"))
        ),
        default=height * 0.5,
    )
    for row in rows:
        text = normalized(row["text"])
        if row["bbox"][1] < buyer_top and row["bbox"][0] < width * 0.6:
            if re.search(r"\b(A\.?\s*S\.?|LIMITED|LTD\.?|SIRKETI)\b", text):
                bounds = list(row["bbox"])
                previous = [
                    r
                    for r in rows
                    if r["bbox"][3] <= bounds[1]
                    and 0 <= bounds[1] - r["bbox"][3] < typical_height * 1.2
                    and abs(r["bbox"][0] - bounds[0]) < width * 0.025
                    and not re.search(r"\d", r["text"])
                    and not any(
                        t in normalized(r["text"])
                        for t in ("MAH", "SOK", "TEL", "FATURA")
                    )
                ]
                if previous:
                    bounds[1] = min(r["bbox"][1] for r in previous)
                    bounds[2] = max([bounds[2]] + [r["bbox"][2] for r in previous])
                fields["seller.name"] = box(bounds)
                break

    delivery_numbers = set()
    for row in rows:
        if "IRSALIYE" in normalized(row["text"]):
            b = row["bbox"]
            delivery_numbers.update(
                w["text"].strip("|[]()")
                for w in words
                if abs((w["bbox"][1] + w["bbox"][3]) / 2 - (b[1] + b[3]) / 2)
                <= typical_height
                and w["bbox"][0] >= b[0]
                and re.search(r"\d", w["text"])
            )

    totals_rows = []
    vat_index = 0
    primary_currency = None
    for row in rows:
        text = normalized(row["text"])
        if re.search(r"FATURA\s+(?:NO|NUMARA)", text) and "IRSALIYE" not in text:
            region = value_box(
                row,
                lambda t: bool(
                    re.fullmatch(r"[A-Za-z0-9$]{6,}", t.strip("|[]()"))
                    and re.search(r"\d", t)
                ),
            )
            if region is None:
                label_box = row["bbox"]
                next_labels = [
                    r["bbox"][1]
                    for r in rows
                    if r["bbox"][1] > label_box[3]
                    and r["bbox"][0] >= label_box[0] - width * 0.02
                    and re.search(
                        r"(FATURA|IRSALIYE|SIPARIS|DUZENLEME).*(TARIH|NO|SAAT)",
                        normalized(r["text"]),
                    )
                ]
                stop = min(next_labels, default=label_box[3] + typical_height * 3.5)
                nearby = [
                    w
                    for w in words
                    if label_box[1] - typical_height <= w["bbox"][1]
                    and w["bbox"][3] < stop
                    and label_box[0] <= w["bbox"][0] <= label_box[2] + width * 0.35
                    and re.fullmatch(r"[A-Za-z0-9$]{8,}", w["text"].strip("|[]()"))
                    and re.search(r"[A-Za-z$]", w["text"])
                    and re.search(r"\d", w["text"])
                    and w["text"].strip("|[]()") not in delivery_numbers
                ]
                if nearby:
                    candidate = min(
                        nearby,
                        key=lambda w: abs(
                            (w["bbox"][1] + w["bbox"][3]) / 2
                            - (label_box[1] + label_box[3]) / 2
                        ),
                    )
                    region = box(candidate["bbox"])
            if region:
                fields["invoice_number"] = region
        if "FATURA TARIH" in text:
            region = value_box(row, lambda t: bool(DATE.search(t)))
            if region is None:
                b = row["bbox"]
                dates = [
                    w
                    for w in words
                    if DATE.fullmatch(w["text"])
                    and b[0] <= w["bbox"][0] <= b[2] + width * 0.3
                    and 0 <= w["bbox"][1] - b[3] <= typical_height * 2
                ]
                if dates:
                    region = box(min(dates, key=lambda w: w["bbox"][1])["bbox"])
            if region:
                fields["invoice_date"] = region
        key = None
        if any(
            label in text
            for label in (
                "MAL HIZMET TOPLAM",
                "MAL VE HIZMET TOPLAM",
                "KDV HARIC TOPLAM",
                "VERGILER HARIC TOPLAM",
                "VERGI HARIC TUTAR",
            )
        ):
            key = "totals.net"
        elif (
            "HESAPLANAN" in text and ("KDV" in text or "KATMA" in text)
        ) or "KDV TOPLAM" in text:
            key = f"vat.{vat_index}"
            vat_index += 1
        elif "DAHIL TOPLAM" in text:
            key = "totals.gross"
        elif "ODENECEK" in text or "LENECEK TUTAR" in text:
            key = "totals.payable"
        elif "TOPLAM ISKONTO" in text or "TOPLAM INDIRIM" in text:
            key = "totals.discount"
        elif "KDV TEVKIFAT TUTARI" in text:
            key = "totals.withholding"
        if key:
            found_currency = re.findall(r"(TRY|USD|EUR|GBP|TL)\b", text)
            row_currency = (
                ("TRY" if found_currency[-1] == "TL" else found_currency[-1])
                if found_currency
                else None
            )
            if key == "totals.net" and primary_currency is None:
                primary_currency = row_currency
            if primary_currency and row_currency and primary_currency != row_currency:
                totals_rows.append(row)
                continue
            # Etiketlerdeki oran ve yıl gibi sayılar yerine en sağdaki tutarı seçeriz.
            region = value_box(
                row,
                lambda t: bool(
                    AMOUNT.fullmatch(t.strip("—–|")) and ("," in t or "." in t)
                ),
            )
            if region is None:
                same_line = [
                    w
                    for w in words
                    if AMOUNT.fullmatch(w["text"].strip())
                    and "," in w["text"]
                    and abs(
                        (w["bbox"][1] + w["bbox"][3]) / 2
                        - (row["bbox"][1] + row["bbox"][3]) / 2
                    )
                    < typical_height
                    and w["bbox"][0] > row["bbox"][2] - width * 0.08
                ]
                if same_line:
                    region = box(max(same_line, key=lambda w: w["bbox"][0])["bbox"])
            if region is None and key.startswith("vat."):
                nearby = [
                    w
                    for w in words
                    if AMOUNT.fullmatch(w["text"].strip())
                    and "," in w["text"]
                    and 0 <= w["bbox"][1] - row["bbox"][1] <= typical_height * 2
                    and w["bbox"][0] > row["bbox"][2] - width * 0.1
                ]
                if nearby:
                    candidate = min(nearby, key=lambda w: w["bbox"][1])
                    region = box(candidate["bbox"])
            if region:
                fields.setdefault(key, region)
                totals_rows.append(row)

    if not any(key.startswith("vat.") for key in fields) and all(
        k in fields for k in ("totals.net", "totals.gross")
    ):
        lower, upper = (
            fields["totals.net"][3] * height,
            fields["totals.gross"][1] * height,
        )
        candidates = [
            row
            for row in rows
            if lower < row["bbox"][1] < upper
            and any(
                AMOUNT.fullmatch(w["text"]) and "," in w["text"] for w in row_words(row)
            )
            and not any(t in normalized(row["text"]) for t in ("ISKONTO", "INDIRIM"))
        ]
        if len(candidates) == 1:
            fields["vat.0"] = value_box(
                candidates[0], lambda t: bool(AMOUNT.fullmatch(t) and "," in t)
            )
            warnings.append(
                {
                    "code": "vat_total_review",
                    "detail": "KDV toplamının etiketi okunamadı; toplamlar arasındaki tutar adayını doğrulayın.",
                }
            )

    if "invoice_number" not in fields and "invoice_date" in fields:
        date_box = fields["invoice_date"]
        # Etiket silinmişse yalnızca fatura tarihinin hemen üstündeki aynı blok taranır.
        # İrsaliye tarihinin çevresinden veya alt satırlardan numara seçilmez.
        candidates = [
            w
            for w in words
            if 0 < date_box[1] * height - w["bbox"][3] < typical_height * 3
            and abs(w["bbox"][0] - date_box[0] * width) < width * 0.07
            and re.fullmatch(r"[A-Za-z0-9]{10,}", w["text"].strip("|[]()"))
            and re.search(r"[A-Za-z]", w["text"])
            and re.search(r"\d", w["text"])
            and w["text"].strip("|[]()") not in delivery_numbers
        ]
        if candidates:
            candidate = max(candidates, key=lambda w: w["bbox"][3])
            fields["invoice_number"] = box(candidate["bbox"])
            warnings.append(
                {
                    "code": "identifier_review",
                    "detail": "Numara etiketi okunamadı; fatura tarihinin üstündeki numara adayı belgeyle doğrulanmalı.",
                }
            )

    # Sütunları Miktar başlığının çevresindeki başlık sözcüklerinden oluştururuz.
    quantity_headers = [
        w for w in words if normalized(w["text"]).strip("()[]|:.") == "MIKTAR"
    ]
    regions = dict(fields)
    if quantity_headers:
        q = quantity_headers[0]
        qy = (q["bbox"][1] + q["bbox"][3]) / 2
        char_height = median(max(1, w["bbox"][3] - w["bbox"][1]) for w in words)
        headers = sorted(
            [
                w
                for w in words
                if abs((w["bbox"][1] + w["bbox"][3]) / 2 - qy) <= char_height * 1.15
            ],
            key=lambda w: w["bbox"][0],
        )
        table_top = max(w["bbox"][3] for w in headers)
        below = [r for r in totals_rows if r["bbox"][1] > table_top]
        table_bottom = min((r["bbox"][1] for r in below), default=height * 0.9) - 3

        def label(word):
            value = normalized(word["text"]).strip("()[]|:.")
            return {"TUTAN": "TUTARI", "TUTAR": "TUTARI", "HIZM": "HIZMET"}.get(
                value, value
            )

        def find_sequence(sequence, prefer_left=False):
            # Aynı başlık iki satıra bölünebilir; yalnızca soldan sağa sıraya bağlı değiliz.
            matches = []
            for first in headers:
                if label(first) != sequence[0]:
                    continue
                selected = [first]
                for token in sequence[1:]:
                    candidates = [
                        w
                        for w in headers
                        if label(w) == token
                        and w not in selected
                        and abs(w["bbox"][0] - selected[-1]["bbox"][2]) < width * 0.08
                        and w["bbox"][0] >= first["bbox"][0] - width * 0.025
                    ]
                    if not candidates:
                        break
                    selected.append(
                        min(
                            candidates,
                            key=lambda w: abs(w["bbox"][0] - selected[-1]["bbox"][2]),
                        )
                    )
                if len(selected) == len(sequence):
                    matches.append(
                        (
                            min(w["bbox"][0] for w in selected),
                            max(w["bbox"][2] for w in selected),
                        )
                    )
            if not matches:
                return None
            return min(
                matches,
                key=(
                    (lambda span: span[0])
                    if prefer_left
                    else (lambda span: span[1] - span[0])
                ),
            )

        header_specs = [
            ("quantity", ["MIKTAR"]),
            ("unit_price", ["BIRIM", "FIYAT"]),
            ("vat_rate", ["KDV", "ORANI"]),
            ("name", ["MAL", "HIZMET"]),
            ("line_total", ["MAL", "HIZMET", "TUTARI"]),
            ("discount", ["ISKONTO", "TUTARI"]),
            ("description", ["ACIKLAMA"]),
            ("vat_amount", ["KDV", "TUTARI"]),
            ("other_tax", ["DIGER", "VERGILER"]),
            ("row_number", ["SIRA"]),
        ]
        spans = {
            key: match
            for key, sequence in header_specs
            if (match := find_sequence(sequence, prefer_left=(key == "name")))
        }
        if "name" not in spans:
            title_words = [
                w
                for w in headers
                if label(w) in ("HIZMET", "URUN")
                and w["bbox"][0] < spans.get("quantity", (width,))[0]
            ]
            if title_words:
                spans["name"] = (
                    min(w["bbox"][0] for w in title_words),
                    max(w["bbox"][2] for w in title_words),
                )
        if "line_total" not in spans:
            total_words = [w for w in headers if label(w) == "TOPLAM"]
            if total_words:
                word = max(total_words, key=lambda w: w["bbox"][0])
                spans["line_total"] = (word["bbox"][0], word["bbox"][2])
        if "line_total" not in spans and "quantity" in spans:
            # En sağdaki Mal/Hizmet başlığı okunup altındaki Tutar sözcüğü kaybolabilir.
            right_labels = [
                w
                for w in headers
                if label(w) == "MAL" and w["bbox"][0] > spans["quantity"][1]
            ]
            for first in sorted(right_labels, key=lambda w: w["bbox"][0], reverse=True):
                seconds = [
                    w
                    for w in headers
                    if label(w) == "HIZMET"
                    and 0 <= w["bbox"][0] - first["bbox"][2] < width * 0.05
                ]
                if seconds:
                    spans["line_total"] = (
                        first["bbox"][0],
                        max(w["bbox"][2] for w in seconds),
                    )
                    warnings.append(
                        {
                            "code": "partial_header",
                            "detail": "Son tutar sütununun başlığı kısmen okundu; satır toplamlarını doğrulayın.",
                        }
                    )
                    break
        if "vat_rate" not in spans:
            candidates = [
                w
                for w in headers
                if label(w) == "KDV"
                and (
                    "vat_amount" not in spans
                    or w["bbox"][0] < spans["vat_amount"][0] - 5
                )
            ]
            if candidates:
                w = min(candidates, key=lambda w: w["bbox"][0])
                spans["vat_rate"] = (w["bbox"][0], w["bbox"][2])
        if all(
            k in spans
            for k in ("name", "quantity", "unit_price", "vat_rate", "line_total")
        ):
            columns = {}
            for key, (left_edge, right_edge) in spans.items():
                if key in ("vat_amount", "other_tax", "row_number"):
                    continue
                left = left_edge - 5
                if key in ("line_total", "unit_price", "vat_rate"):
                    previous_edges = [
                        span[1] for other, span in spans.items() if span[1] < left_edge
                    ]
                    if previous_edges:
                        left = (max(previous_edges) + left_edge) / 2
                if key == "name":
                    previous = [
                        w["bbox"][2] for w in headers if w["bbox"][2] < left_edge
                    ]
                    if previous:
                        left = max(previous) + 2
                following = [
                    span[0] for other, span in spans.items() if span[0] > right_edge
                ]
                right = min(following) - 3 if following else width * 0.96
                if key in ("quantity", "unit_price", "vat_rate") and following:
                    right = (right_edge + min(following)) / 2
                columns[key] = (left, right)
            # Sağdaki mal/hizmet tutarı hücreleri satır çapasıdır; sayısal değer üretmez.
            left, right = columns["line_total"]
            anchors = sorted(
                {
                    (w["bbox"][1] + w["bbox"][3]) / 2
                    for w in words
                    # Eğik taramada ilk tutarın üst kenarı başlıkla örtüşebilir.
                    # Satırın gövdesini temsil eden merkez nokta esas alınır.
                    if table_top < (w["bbox"][1] + w["bbox"][3]) / 2 < table_bottom
                    and left <= w["bbox"][0] < right
                    and AMOUNT.fullmatch(w["text"].strip())
                }
            )
            for i, y in enumerate(anchors):
                top = table_top if i == 0 else (anchors[i - 1] + y) / 2
                bottom = (
                    table_bottom if i == len(anchors) - 1 else (anchors[i + 1] + y) / 2
                )
                if i == len(anchors) - 1:
                    extent = y + char_height
                    for word in sorted(words, key=lambda w: w["bbox"][1]):
                        b = word["bbox"]
                        if b[1] < y - char_height or b[1] >= table_bottom:
                            continue
                        if not columns["name"][0] <= b[0] <= columns["line_total"][1]:
                            continue
                        if b[1] > extent + char_height * 2.5:
                            break
                        extent = max(extent, b[3])
                    bottom = min(bottom, extent + 3)
                for key, (left, right) in columns.items():
                    if key == "name":
                        right = min(
                            columns["quantity"][0],
                            columns.get("description", columns["quantity"])[0],
                        )
                    cell_top, cell_bottom = top, bottom
                    if key in ("quantity", "unit_price", "vat_rate", "line_total"):
                        cell_top = max(top, y - char_height * 1.5)
                        cell_bottom = min(bottom, y + char_height * 1.5)
                    regions[f"item.{i}.{key}"] = box(
                        [left, cell_top, right, cell_bottom], padding=0
                    )
                # Miktar hücresindeki birim metni ayrı okunur; miktardan düşürülmez.
                qleft, qright = columns["quantity"]
                units = [
                    w
                    for w in words
                    if top <= (w["bbox"][1] + w["bbox"][3]) / 2 <= bottom
                    and qleft <= w["bbox"][0] <= qright
                    and normalized(w["text"])
                    in ("ADET", "SAAT", "KG", "LT", "M", "M2", "PAKET", "GUN", "AY")
                ]
                if units:
                    unit_left = min(w["bbox"][0] for w in units) - 2
                    regions[f"item.{i}.quantity"][2] = unit_left / width
                    regions[f"item.{i}.unit"] = box(
                        [unit_left, top, qright, bottom], padding=0
                    )
                # Sağa hizalı uzun miktar, kısa Miktar başlığının soluna taşabilir.
                # Hücre merkezine düşen tek sayısal OCR kutusu kesilmeden alınır;
                # komşu satırdan/sütundan miktar veya hesapla değer üretilmez.
                quantity_box = regions[f"item.{i}.quantity"]
                quantity_words = [
                    word
                    for word in words
                    if re.fullmatch(r"-?\d+(?:[.,]\d+)*", word["text"])
                    and quantity_box[0] * width
                    <= (word["bbox"][0] + word["bbox"][2]) / 2
                    <= quantity_box[2] * width
                    and quantity_box[1] * height
                    <= (word["bbox"][1] + word["bbox"][3]) / 2
                    <= quantity_box[3] * height
                ]
                if len(quantity_words) == 1:
                    start = max(0, (quantity_words[0]["bbox"][0] - 3) / width)
                    quantity_box[0] = min(quantity_box[0], start)
                    for text_column in ("name", "description"):
                        key = f"item.{i}.{text_column}"
                        if key in regions and regions[key] is not None:
                            regions[key][2] = min(regions[key][2], quantity_box[0])
                # Açıklama sütunu yoksa boş hücreye başka alandan veri taşınmaz.
                if "description" not in columns:
                    regions[f"item.{i}.description"] = None
        else:
            warnings.append(
                {
                    "code": "unknown_table",
                    "detail": "Tablo başlıklarının bir bölümü tanınamadı; kalemleri kontrol edin.",
                }
            )

    # Etiketsiz dipnot bölgesi ancak toplamların altındaki ayrı metin bloğunda adaydır.
    if totals_rows:
        bottom = max(r["bbox"][3] for r in totals_rows)
        note_rows = [
            r
            for r in rows
            if bottom + height * 0.01 < r["bbox"][1] < bottom + height * 0.13
            and r["bbox"][0] < width * 0.5
            and not normalized(r["text"]).startswith(
                ("*BU BELGE", "HTTP", "BANKA BILGILERI", "SCANNED")
            )
        ]
        if note_rows:
            # İlk açıklama bloğundan sonraki banka tablosu ve kaşeyi nota katmayız.
            selected = []
            line_height = median(max(1, w["bbox"][3] - w["bbox"][1]) for w in words)
            for row in sorted(note_rows, key=lambda r: r["bbox"][1]):
                if len(row["text"].strip()) < 10:
                    continue
                if (
                    selected
                    and row["bbox"][1] - selected[-1]["bbox"][3] > line_height * 2.2
                ):
                    break
                selected.append(row)
            if selected:
                regions["invoice_note"] = box(
                    [
                        min(r["bbox"][0] for r in selected),
                        min(r["bbox"][1] for r in selected),
                        max(r["bbox"][2] for r in selected),
                        max(r["bbox"][3] for r in selected),
                    ]
                )
                warnings.append(
                    {
                        "code": "invoice_note_review",
                        "detail": "Toplamların altındaki açıklama not adayı olarak okundu; belgeyle doğrulayın.",
                    }
                )
    return {k: v for k, v in regions.items() if v is not None}, warnings
