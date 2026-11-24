"""PDF ve fotoğrafları aynı inceleme kaydına dönüştüren uygulama hizmeti."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import time
import tempfile

from fatura.extraction.page_filter import is_blank_image, is_invoice_text, ocr_text, page_text
from fatura.invoice import extract, parse, validate
from fatura.metrics import peak_rss_bytes


class NoInvoiceContent(ValueError):
    """Belgenin hiçbir sayfası fatura içeriği taşımıyor (boş / yalnızca imza)."""


def no_invoice_result(reader=None):
    """Kayıt açılmayacağını bildiren sonuç; uygulama bu belgeyi tamamen kaldırır."""
    return {'no_invoice_content': True, 'reader': reader}


def empty_record():
    record = parse([])
    record['metrics'] = {'elapsed_seconds': 0, 'process_peak_rss_bytes': None}
    return record


def renderer():
    executable = shutil.which('pdftoppm')
    if executable:
        return executable
    # Geliştirme bilgisayarının paketli aracı yalnızca yedek yoldur.
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/pdftoppm'
    if bundled.exists():
        return str(bundled)
    raise ValueError('PDF önizlemesi için Poppler kurulmalı (pdftoppm bulunamadı).')


def render_page(source, directory, page=1):
    destination = Path(directory) / f'page-{page}.png'
    if not destination.exists():
        # Önizleme ve OCR eşzamanlı isterse yarım PNG okunmasını engeller.
        with tempfile.TemporaryDirectory(prefix='render-', dir=directory) as work:
            prefix = Path(work) / 'page'
            subprocess.run([renderer(), '-f', str(page), '-l', str(page), '-scale-to', '2200',
                            '-png', '-singlefile', str(source), str(prefix)],
                           check=True, capture_output=True, timeout=90)
            prefix.with_suffix('.png').replace(destination)
    return destination


def image_record(image, backend):
    """Sayfa kaydı; sayfa fatura içeriği taşımıyorsa (boş, imza/tarih) None döner."""
    from fatura.extraction.scan import parse_regions
    ocr = backend(image)
    if not is_invoice_text(ocr_text(ocr)):
        return None
    # Tedarikçi profili yoksa başlık ve etiketlere göre bölgeler türetilir.
    from fatura.extraction.flexible_layout import dynamic_regions
    regions, warnings = dynamic_regions(ocr)
    if not any(key.startswith('item.') for key in regions) and hasattr(backend, 'cleaned'):
        alternative = backend.cleaned()
        alternative_ocr = alternative(image)
        alternative_regions, alternative_warnings = dynamic_regions(alternative_ocr)
        if not any(key.startswith('item.') for key in alternative_regions):
            from PIL import Image
            from fatura.ocr.preprocessing import remove_table_lines, enhance_header_words
            with Image.open(image) as source_image:
                enhanced_ocr = enhance_header_words(remove_table_lines(source_image), alternative_ocr, alternative)
            enhanced_regions, enhanced_warnings = dynamic_regions(enhanced_ocr)
            if len(enhanced_regions) > len(alternative_regions):
                alternative_ocr, alternative_regions, alternative_warnings = enhanced_ocr, enhanced_regions, enhanced_warnings
        if len(alternative_regions) > len(regions):
            backend, ocr, regions = alternative, alternative_ocr, {**regions, **alternative_regions}
            warnings = alternative_warnings + [{'code': 'preprocessing_review', 'detail': 'Tablo çizgileri okuma kopyasında temizlendi; alanları kaynak belgeyle doğrulayın.'}]
    if regions:
        if getattr(backend, 'table_cleanup', False):
            from fatura.ocr.preprocessing import align_regions_to_grid, recover_grid_rows
            from PIL import Image
            with Image.open(image) as source_image:
                regions = align_regions_to_grid(source_image, regions)
                regions = recover_grid_rows(source_image, regions)
        crops = backend(image, regions)
        from fatura.extraction.identifier_check import check_identifier
        check_identifier(image, regions, crops, backend)
        result = parse_regions(ocr, regions, crops, None)
        result['issues'].extend(warnings)
        result['issues'].append({'code': 'layout_review', 'detail': 'Alanlar belge başlıklarından eşleştirildi; konumları ve kalemleri doğrulayın.'})
        if hasattr(backend, 'review_headers'):
            result = backend.review_headers(result, ocr)
        return result
    lines = [{'text': l['text'], 'page': 1, 'bbox': l['bbox']} for l in ocr['lines']]
    page = {'page': 1, 'width': ocr['width'], 'height': ocr['height'], 'method': backend.name, 'lines': lines}
    result = parse([page])
    result['issues'].append({'code': 'unknown_layout', 'detail': 'Belgenin düzeni henüz tanınmıyor. Alanları belgeden kontrol ederek tamamlayın.'})
    if hasattr(backend, 'review_headers'):
        result = backend.review_headers(result, ocr)
    return result


def text_record(page):
    """Metinli PDF'de başlık tabanlı alanları OCR çalıştırmadan eşleştirir."""
    from fatura.extraction.flexible_layout import dynamic_regions
    from fatura.extraction.scan import parse_regions
    words = [word for line in page['lines'] for word in line.get('words', [])]
    if not words:
        return parse([page])
    ocr = {'width': page['width'], 'height': page['height'], 'engine': 'pdf_text',
           'lines': [{'text': w['text'], 'bbox': [w['x0'], w['top'], w['x1'], w['bottom']]} for w in words]}
    regions, warnings = dynamic_regions(ocr)
    if not regions:
        return parse([page])
    crops = {'regions': {}}
    for key, b in regions.items():
        crops['regions'][key] = [word for word in ocr['lines']
            if b[0] <= (word['bbox'][0]+word['bbox'][2])/2/page['width'] <= b[2]
            and b[1] <= (word['bbox'][1]+word['bbox'][3])/2/page['height'] <= b[3]]
    result = parse_regions(ocr, regions, crops, None)
    result['issues'].extend(warnings)
    return result


def merge_records(records):
    """Sayfalardan gelen kalemleri birleştirir; çelişen değerleri görünür kılar.

    None olan sayfa fatura içermediği için atlanmıştır; sayfa numaraları korunur.
    """
    if not any(record is not None for record in records):
        raise NoInvoiceContent('Belgede fatura içeren sayfa bulunamadı.')
    merged = empty_record()
    merged['issues'], merged['checks'] = [], []
    skipped = [n for n, record in enumerate(records, 1) if record is None]
    if skipped:
        merged['issues'].append({'code': 'skipped_pages', 'detail': f'{", ".join(map(str, skipped))}. sayfa fatura içeriği taşımadığı için (boş/imza) okunmadı.'})
    for page_number, record in enumerate(records, 1):
        if record is None:
            continue
        record = deepcopy(record)
        def fix_page(value):
            if isinstance(value, dict):
                if 'page' in value:
                    value['page'] = page_number
                for child in value.values():
                    fix_page(child)
            elif isinstance(value, list):
                for child in value:
                    fix_page(child)
        fix_page(record)
        if record.get('invoice_note'):
            notes = [merged.get('invoice_note'), record['invoice_note']]
            merged['invoice_note'] = '\n'.join(dict.fromkeys(n for n in notes if n))
        merged['items'].extend(record['items'])
        merged['pages'].extend(record['pages'])
        for key in ['invoice_number', 'invoice_date', 'currency']:
            value = record.get(key)
            if value is not None:
                if merged[key] not in (None, value):
                    merged['issues'].append({'code': 'conflicting_values', 'detail': f'{key}: sayfalar arasında farklı değerler var.'})
                else:
                    merged[key] = value
        for group in ('seller', 'totals'):
            for key, value in record[group].items():
                if value is not None:
                    if merged[group][key] not in (None, value):
                        merged['issues'].append({'code': 'conflicting_values', 'detail': f'{group}.{key}: sayfalar arasında farklı değerler var.'})
                    else:
                        merged[group][key] = value
        for key, evidence in record.get('evidence', {}).items():
            merged['evidence'].setdefault(key, []).extend(evidence)
        # Son birleşik kayıt için yeniden hesaplanacak eksik alan uyarıları taşınmaz.
        merged['issues'].extend(i for i in record['issues'] if i['code'] not in ('missing_field', 'missing_items', 'missing_item_field', 'amount_mismatch'))
    return validate(merged)


def analyze(source, mime):
    """Metinli sayfayı doğrudan, görsel sayfayı OCR ile okur; dış servise göndermez."""
    start = time.perf_counter()
    source = Path(source)
    records = []
    backend = None
    if mime == 'application/pdf':
        pages = extract(source)
        if len(pages) > 30:
            raise ValueError('Bir seferde en fazla 30 sayfalık PDF yükleyebilirsiniz.')
        try:
            # Tanımlı metin şablonlarında sayfa devamını mevcut ayrıştırıcı korur.
            if all(p['method'] == 'pdf_text' for p in pages):
                kept = [p for p in pages if is_invoice_text(page_text(p))]
                if not kept:
                    raise NoInvoiceContent('Belgede fatura içeren sayfa bulunamadı.')
                result = parse(kept)
                if not result['items']:
                    result = merge_records([text_record(p) if p in kept else None for p in pages])
            else:
                for i, page in enumerate(pages, 1):
                    if page['method'] == 'pdf_text':
                        records.append(text_record(page) if is_invoice_text(page_text(page)) else None)
                        continue
                    image = render_page(source, source.parent, i)
                    if is_blank_image(image):
                        records.append(None)
                        continue
                    if backend is None:
                        from fatura.ocr.backends import TesseractBackend
                        backend = TesseractBackend()
                    records.append(image_record(image, backend))
                result = merge_records(records)
        except NoInvoiceContent:
            return no_invoice_result()
    else:
        from fatura.ocr.backends import TesseractBackend
        backend = TesseractBackend()
        result = image_record(source.parent / 'page-1.png', backend)
        if result is None:
            return no_invoice_result()
    result['source'] = None
    result['metrics'] = {'elapsed_seconds': round(time.perf_counter()-start, 3), 'process_peak_rss_bytes': peak_rss_bytes()}
    return result
