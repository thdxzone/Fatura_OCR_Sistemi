"""PaddleOCR'ı ayrı süreçte çalıştırır; iş bitince model belleği bırakılır."""

import argparse
import json
import os
from pathlib import Path
import time

from fatura.paths import MODELS_DIR

os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(MODELS_DIR / "paddlex"))
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def run(reader, source, mime):
    from fatura.extraction.page_filter import is_blank_image
    from fatura.extraction.service import (
        NoInvoiceContent, image_record, merge_records, no_invoice_result, render_page,
    )
    from fatura.invoice import extract
    from fatura.metrics import peak_rss_bytes

    started = time.perf_counter()
    if mime == "application/pdf":
        pages = extract(source)
        if len(pages) > 30:
            raise ValueError("En fazla 30 sayfa yüklenebilir.")
        images = [render_page(source, source.parent, i + 1) for i in range(len(pages))]
    else:
        images = [source.parent / "page-1.png"]
    if reader != "paddle":
        raise ValueError("Bilinmeyen okuyucu.")
    from fatura.ocr.paddle_reader import PaddleBackend

    # Boş sayfalar model yüklenmeden elenir; tamamen boş belgede model hiç yüklenmez.
    blank = [is_blank_image(image) for image in images]
    if all(blank):
        return no_invoice_result(reader)
    backend = PaddleBackend()
    records = [None if empty else image_record(image, backend) for image, empty in zip(images, blank)]
    try:
        result = merge_records(records)
    except NoInvoiceContent:
        return no_invoice_result(reader)
    result["metrics"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "process_peak_rss_bytes": peak_rss_bytes(),
    }
    result["reader"] = reader
    result["needs_review"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader", choices=["paddle"], default="paddle")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--mime", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.reader, args.source, args.mime)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
