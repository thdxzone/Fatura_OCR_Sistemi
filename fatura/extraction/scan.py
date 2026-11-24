"""Taranmış PDF'leri seçilebilir yerel OCR motoru ve bölge profiliyle okur."""

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
from fatura.metrics import peak_rss_bytes
import shutil
import subprocess
import tempfile
import time

from fatura.invoice import extract, number, parse, validate


def build_regions(ocr, profile):
    """Satır numaralarını çapa alır; sabit ürün sayısı kullanmaz."""
    table = profile["table"]
    anchors = []
    for line in ocr["lines"]:
        x0, y0, x1, y1 = line["bbox"]
        x, y = (x0 + x1) / 2 / ocr["width"], (y0 + y1) / 2 / ocr["height"]
        if (
            table["row_number"][0] <= x <= table["row_number"][1]
            and table["top"] <= y <= table["bottom"]
        ):
            if re.fullmatch(r"\d+", line["text"].strip()):
                anchors.append((y, int(line["text"])))
    anchors.sort()
    # Seyrek metin OCR sıra numarasını kaçırırsa tutar sütunu satır çapası olabilir.
    # Bu yalnızca satır konumunu bulur; miktar veya sıra numarası üretmez.
    total_bounds = table["columns"].get("line_total")
    total_anchors = []
    if total_bounds:
        for line in ocr["lines"]:
            x0, y0, x1, y1 = line["bbox"]
            x, y = (x0 + x1) / 2 / ocr["width"], (y0 + y1) / 2 / ocr["height"]
            if (
                total_bounds[0] <= x <= total_bounds[1]
                and table["top"] <= y <= table["bottom"]
            ):
                if re.fullmatch(r"\d[\d.,]*(?:\s*(?:TL|TRY))?", line["text"].strip()):
                    if not any(
                        abs(y - existing[0]) < 0.004 for existing in total_anchors
                    ):
                        total_anchors.append((y, None))
        total_anchors.sort()
    if len(total_anchors) > len(anchors):
        anchors = total_anchors
    regions = dict(profile["fields"])
    for i, (y, _) in enumerate(anchors):
        top = table["top"] if i == 0 else (anchors[i - 1][0] + y) / 2
        bottom = (
            table["bottom"] if i == len(anchors) - 1 else (anchors[i + 1][0] + y) / 2
        )
        for field, (left, right) in table["columns"].items():
            regions[f"item.{i}.{field}"] = [left, top, right, bottom]
    return regions, anchors


def parse_regions(ocr, regions, crops, source):
    """Ham OCR metnini korur; biçim onarımlarını ayrıca kontrol nedeni yapar."""
    page = {
        "page": 1,
        "width": ocr["width"],
        "height": ocr["height"],
        "method": ocr.get("engine", "apple_vision"),
        "lines": [
            {"text": l["text"], "page": 1, "bbox": l["bbox"]} for l in ocr["lines"]
        ],
    }
    result = parse([page], source)
    result["issues"], result["checks"] = [], []

    def issue(code, detail):
        result["issues"].append({"code": code, "detail": detail})

    def read(key):
        if key not in regions:
            return None
        # Aynı satırdaki sözcüklerin üst kenarı farklı olabilir; dikey merkezle grupla.
        rows = []
        for observation in sorted(
            crops["regions"].get(key, []),
            key=lambda l: (l["bbox"][1] + l["bbox"][3]) / 2,
        ):
            b = observation["bbox"]
            center = (b[1] + b[3]) / 2
            if not rows or abs(rows[-1][0] - center) > max(3, (b[3] - b[1]) * 0.5):
                rows.append((center, [observation]))
            else:
                rows[-1][1].append(observation)
        observations = [
            word for _, row in rows for word in sorted(row, key=lambda l: l["bbox"][0])
        ]
        text = " ".join(l["text"] for l in observations).strip()
        b = regions[key]
        evidence = {
            "text": text,
            "page": 1,
            "bbox": [
                b[0] * ocr["width"],
                b[1] * ocr["height"],
                b[2] * ocr["width"],
                b[3] * ocr["height"],
            ],
        }
        result["evidence"][key] = [evidence]
        return text or None

    def numeric(key):
        text = read(key)
        if (
            key.endswith(".unit_price") or key.startswith("totals.")
        ) and key in regions:
            b = regions[key]
            values = [
                w["text"]
                for w in ocr["lines"]
                if re.fullmatch(r"\d[\d.]*,\d+", w["text"])
                and b[0] <= (w["bbox"][0] + w["bbox"][2]) / 2 / ocr["width"] <= b[2]
                and b[1] <= (w["bbox"][1] + w["bbox"][3]) / 2 / ocr["height"] <= b[3]
            ]
            if len(values) == 1:
                text = values[0]
        if text is None:
            return None
        raw = (
            re.sub(r"\s*(TL|TRY|USD|EUR|GBP|₺)\s*$", "", text)
            .strip()
            .lstrip("%")
            .strip()
        )
        if raw.startswith(("—", "–")):
            issue(
                "ocr_numeric_separator",
                f"{key}: tutarın başındaki çizgi artefaktı kontrol edilmeli",
            )
            raw = raw.lstrip("—–").strip()
        # Binlik ayıracı da virgül okunmuşsa yalnızca açık para biçimi onarılır.
        # Örnek: 1,778,10 → 1.778,10. Miktar/oran bu kurala dahil değildir.
        if (
            key.startswith("totals.") or key.endswith((".unit_price", ".line_total"))
        ) and re.fullmatch(r"\d{1,3}(?:,\d{3})+,\d{2,}", raw):
            grouped, decimal = raw.rsplit(",", 1)
            corrected = grouped.replace(",", ".") + "," + decimal
            issue("ocr_numeric_separator", f"{key}: {raw} → {corrected}")
            raw = corrected
        # OCR virgülü nokta okuyabilir. Dönüşüm kayıt altına alınır ve onay istenir.
        if "," not in raw and re.fullmatch(r"\d+(?:\.\d{3})*\.\d{2}", raw):
            raw = raw[: raw.rfind(".")] + "," + raw[raw.rfind(".") + 1 :]
            issue("ocr_numeric_separator", f"{key}: {text} → {raw}")
        try:
            return str(number(raw))
        except ValueError:
            issue("invalid_field", f"{key}: {text}")
            return None

    for key in regions:
        if key.startswith(("item.", "vat.")):
            continue
        if key.startswith("totals."):
            value = numeric(key)
        else:
            value = read(key)
        if key == "seller.tax_id" and value:
            match = re.search(r"\b\d{10}\b", value)
            value = match[0] if match else None
        if key == "invoice_number" and key in regions:
            b = regions[key]
            candidates = [
                w["text"].strip("|[]()")
                for w in ocr["lines"]
                if b[0] <= (w["bbox"][0] + w["bbox"][2]) / 2 / ocr["width"] <= b[2]
                and b[1] <= (w["bbox"][1] + w["bbox"][3]) / 2 / ocr["height"] <= b[3]
                and re.fullmatch(r"[A-Za-z0-9]{8,}", w["text"].strip("|[]()"))
            ]
            if len(candidates) == 1 and value != candidates[0]:
                issue(
                    "identifier_review",
                    "Tam sayfa ve hücre okumaları farklı; numara adayı belgeyle doğrulanmalı.",
                )
                value = candidates[0]
        if key == "invoice_number" and crops.get("identifier_unresolved"):
            issue(
                "identifier_review",
                "Numara başındaki harf okunamadı; numarayı belgeden tamamlayın.",
            )
            value = None
        if (
            key == "invoice_number"
            and value
            and not re.fullmatch(r"[A-Za-z0-9]+", value)
        ):
            issue("invalid_identifier", f"{key}: {value}")
            value = None
        if key == "invoice_date" and key in regions:
            b = regions[key]
            source_dates = [
                w["text"]
                for w in ocr["lines"]
                if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", w["text"])
                and b[0] <= (w["bbox"][0] + w["bbox"][2]) / 2 / ocr["width"] <= b[2]
                and b[1] <= (w["bbox"][1] + w["bbox"][3]) / 2 / ocr["height"] <= b[3]
            ]
            if len(source_dates) == 1 and (
                value is None or source_dates[0] not in value
            ):
                issue(
                    "date_review",
                    "Tam sayfa ve hücre tarih okumaları farklı; tarihi belgeyle doğrulayın.",
                )
                value = source_dates[0]
        if key == "invoice_date" and value:
            match = re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", value)
            try:
                value = (
                    datetime.strptime(re.sub(r"[-/]", ".", match[0]), "%d.%m.%Y")
                    .date()
                    .isoformat()
                    if match
                    else None
                )
            except ValueError:
                value = None
        group, _, field = key.partition(".")
        if field:
            result[group][field] = value
        else:
            result[key] = value
    if crops.get("identifier_review"):
        issue(
            "identifier_review",
            "Fatura numarası alternatif okumayla bulundu; harf ve rakamları belgeyle doğrulayın.",
        )
        box = result["evidence"].get("invoice_number", [{}])[0].get("bbox", [])
        for attempt in crops.get("identifier_attempts", {}).get("invoice_number", []):
            raw_text = " ".join(o["text"] for o in attempt)
            result["evidence"].setdefault("invoice_number", []).append(
                {"text": raw_text, "page": 1, "bbox": box}
            )
    vat_keys = [k for k in regions if k.startswith("vat.")]
    amounts = [numeric(k) for k in vat_keys]
    if amounts and all(a is not None for a in amounts):
        result["totals"]["vat"] = str(sum((Decimal(a) for a in amounts), Decimal(0)))
        result["evidence"]["totals.vat"] = [
            e for k in vat_keys for e in result["evidence"][k]
        ]
    indices = sorted({int(k.split(".")[1]) for k in regions if k.startswith("item.")})
    for i in indices:
        item = {
            field: numeric(f"item.{i}.{field}")
            for field in ("quantity", "unit_price", "vat_rate", "line_total")
        }
        item.update(
            {
                "name": read(f"item.{i}.name"),
                "description": read(f"item.{i}.description"),
                "unit": read(f"item.{i}.unit"),
                "discount": (
                    numeric(f"item.{i}.discount")
                    if f"item.{i}.discount" in regions
                    else None
                ),
                "price_basis": "unknown",
                "total_basis": "unknown",
                "evidence": result["evidence"][f"item.{i}.name"][0],
            }
        )
        result["items"].append(item)
        issue(
            "ambiguous_tax_basis", f"items.{i}: vergi esası açık bir ifadeden okunamadı"
        )
    # Döviz kodu banka hesaplarından alınmaz; yalnızca parasal hücredeki TL kullanılır.
    currency_keys = [
        k
        for k in regions
        if k.startswith("totals.") or k.endswith((".unit_price", ".line_total"))
    ]
    currencies = set()
    currency_evidence = []
    for key in currency_keys:
        for entry in result["evidence"].get(key, []):
            matches = re.findall(r"\b(TL|TRY|EUR|USD|GBP)\b", entry["text"])
            if matches:
                currencies.update(
                    "TRY" if value == "TL" else value for value in matches
                )
                currency_evidence.append(entry)
    if len(currencies) == 1:
        result["currency"] = currencies.pop()
        result["evidence"]["currency"] = currency_evidence
    elif len(currencies) > 1:
        issue(
            "currency_conflict",
            "Parasal hücrelerde farklı para birimleri okundu; doğrulayın.",
        )
    if ocr.get("engine") == "pdf_text":
        issue(
            "document_review",
            "Okunan alanların belgedeki doğru alanlarla eşleştiğini doğrulayın.",
        )
    else:
        issue(
            "ocr_review",
            "OCR çıktısındaki ad/unvan, numara ve sayılar görselle doğrulanmalı",
        )
    return validate(result)


def run_scan(pdf, profile_path, output, renderer="pdftoppm", backend=None):
    if backend is None:
        from fatura.ocr.backends import TesseractBackend

        backend = TesseractBackend()
    started = time.perf_counter()
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    pages = extract(pdf)
    if len(pages) != 1:
        raise ValueError(
            "Bu bölge profili yalnızca tek sayfalı belgeler için doğrulandı"
        )
    if pages[0]["method"] != "ocr_required":
        raise ValueError("Metin katmanlı PDF için invoice.py kullanın")
    with tempfile.TemporaryDirectory(prefix="fatura-scan-") as directory:
        prefix = Path(directory) / "page"
        subprocess.run(
            [
                renderer,
                "-scale-to",
                "2200",
                "-png",
                "-singlefile",
                str(pdf),
                str(prefix),
            ],
            check=True,
        )
        ocr = backend(prefix.with_suffix(".png"))
        regions, anchors = build_regions(ocr, profile)
        crops = backend(prefix.with_suffix(".png"), regions)
    result = parse_regions(ocr, regions, crops, str(pdf))
    result["issues"].append(
        {
            "code": "profile_review",
            "detail": "Bölge profili bu belge düzenine özeldir; farklı taramalarda hizalama doğrulanmalı",
        }
    )
    if [n for _, n in anchors] != list(range(1, len(anchors) + 1)):
        result["issues"].append(
            {"code": "row_sequence", "detail": "Kalem sıra numaraları kesintili"}
        )
    result["needs_review"] = True
    result["metrics"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "process_peak_rss_bytes": peak_rss_bytes(),
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    output.with_suffix(".ocr.json").write_text(
        json.dumps(
            {"page": ocr, "regions": regions, "crops": crops},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        f"{len(result['items'])} kalem; {len(result['issues'])} kontrol nedeni; çıktı: {output}"
    )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdf", type=Path)
    cli.add_argument("--profile", type=Path, required=True)
    cli.add_argument("-o", "--output", required=True)
    cli.add_argument("--renderer", default=shutil.which("pdftoppm") or "pdftoppm")
    cli.add_argument(
        "--ocr-engine", choices=["tesseract", "vision"], default="tesseract"
    )
    cli.add_argument(
        "--tesseract", default="tesseract", help="Tesseract yürütülebilir dosya yolu"
    )
    cli.add_argument("--languages", default="tur+eng")
    cli.add_argument("--tessdata", type=Path, help="Yerel dil ağırlıklarının klasörü")
    args = cli.parse_args()
    try:
        from fatura.ocr.backends import TesseractBackend, VisionBackend

        backend = (
            VisionBackend()
            if args.ocr_engine == "vision"
            else TesseractBackend(args.tesseract, args.languages, args.tessdata)
        )
        run_scan(args.pdf, args.profile, args.output, args.renderer, backend)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        cli.exit(1, f"İşlem başarısız: {exc}\n")
