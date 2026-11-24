"""Yerel Türkçe fatura prototipi; hassas sayılar JSON içinde metin olarak saklanır."""
import argparse
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import re
from fatura.metrics import peak_rss_bytes
import time


def number(raw):
    """Türkçe sayı biçimini hassasiyet kaybetmeden Decimal değerine dönüştürür."""
    s = raw.strip().replace(' ', '').replace('₺', '')
    if not re.fullmatch(r'-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?', s):
        raise ValueError(f'Geçersiz Türkçe sayı: {raw}')
    return Decimal(s.replace('.', '').replace(',', '.'))


def money(n):
    """Kontrol hesaplarını kuruşa yuvarlar; belgeden okunan tutara dokunmaz."""
    return n.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def extract(path):
    """Her sayfanın metnini ve sol üst köşeye göre nokta cinsinden konumlarını okur."""
    import pdfplumber
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages, 1):
            words = p.extract_words(x_tolerance=2, y_tolerance=3)
            lines = []
            for w in words:
                if not lines or abs(lines[-1]['bbox'][1] - w['top']) > 3:
                    lines.append({'text': w['text'], 'page': i, 'bbox': [w['x0'], w['top'], w['x1'], w['bottom']], 'words': [w]})
                else:
                    line = lines[-1]
                    line['text'] += ' ' + w['text']
                    line['words'].append(w)
                    line['bbox'][2] = max(line['bbox'][2], w['x1'])
                    line['bbox'][3] = max(line['bbox'][3], w['bottom'])
            pages.append({'page': i, 'width': p.width, 'height': p.height,
                          'method': 'pdf_text' if words else 'ocr_required', 'lines': lines})
    return pages


def parse(pages, source=None):
    """Desteklenen şablonu ortak şemaya aktarır ve tutarlılık kontrollerini üretir."""
    result = {'schema_version': '1.0', 'source': source, 'seller': {'name': None, 'tax_id': None, 'address': None},
              'invoice_date': None, 'invoice_number': None, 'currency': None, 'invoice_note': None, 'items': [],
              'totals': dict.fromkeys(['net', 'vat', 'gross', 'discount', 'withholding', 'payable']),
              'evidence': {}, 'checks': [], 'issues': [], 'pages': pages}
    def issue(code, detail):
        result['issues'].append({'code': code, 'detail': detail})
    def assign(obj, key, value, evidence_key, line):
        if obj[key] is not None and obj[key] != value:
            issue('conflicting_values', evidence_key)
            return
        obj[key] = value
        result['evidence'].setdefault(evidence_key, []).append(line)
    labels = {'Satıcı': ('seller', 'name'), 'VKN': ('seller', 'tax_id'), 'Adres': ('seller', 'address'),
              'Fatura No': (None, 'invoice_number'), 'Fatura Tarihi': (None, 'invoice_date'),
              'Para Birimi': (None, 'currency'), 'KDV Hariç Toplam': ('totals', 'net'),
              'KDV Toplam': ('totals', 'vat'), 'KDV Dahil Toplam': ('totals', 'gross'),
              'İndirim Toplam': ('totals', 'discount'), 'Tevkifat': ('totals', 'withholding'),
              'Ödenecek Tutar': ('totals', 'payable')}
    # Tekrarlanan başlık yeni kalem değildir; sonraki sayfada tablo devam edebilir.
    table = False
    for page in pages:
        if page['method'] == 'ocr_required':
            issue('ocr_required', f"Sayfa {page['page']}: metin katmanı yok; OCR henüz bağlı değil")
        for line in page['lines']:
            text = line['text']
            if text.startswith('Ürün | Açıklama | Miktar | Birim | Birim Fiyat | KDV | Satır Toplam | Esas'):
                table = True
                continue
            if ':' in text and text.split(':', 1)[0] in labels:
                label, raw = text.split(':', 1)
                group, key = labels[label]
                value = raw.strip() or None
                try:
                    if group == 'totals' and value is not None:
                        value = str(number(value))
                    elif key == 'invoice_date' and value:
                        value = datetime.strptime(value, '%d.%m.%Y').date().isoformat()
                    assign(result[group] if group else result, key, value, f'{group + "." if group else ""}{key}', line)
                except ValueError:
                    issue('invalid_field', text)
                table = False
                continue
            if table and '|' in text:
                cells = [x.strip() for x in text.split('|')]
                if len(cells) != 8:
                    issue('unparsed_row', text)
                    continue
                try:
                    name, description, qty, unit, price, vat, total, basis = cells
                    item = {'name': name or None, 'description': description or None, 'quantity': str(number(qty)),
                            'unit': unit or None, 'unit_price': str(number(price)), 'vat_rate': str(number(vat.strip('%'))),
                            'line_total': str(number(total)), 'discount': None,
                            'price_basis': basis if basis in ('net', 'gross') else 'unknown',
                            'total_basis': basis if basis in ('net', 'gross') else 'unknown', 'evidence': line}
                    result['items'].append(item)
                    if item['price_basis'] == 'unknown':
                        issue('ambiguous_tax_basis', text)
                except ValueError:
                    issue('invalid_row', text)
            elif table and text.strip():
                issue('unparsed_table_text', text)
    from fatura.document_notes import extract_note
    result['invoice_note'], note_evidence = extract_note(pages)
    if note_evidence:
        result['evidence']['invoice_note'] = note_evidence
    return validate(result)


def validate(result):
    """Ortak kayıt üzerinde kontrolleri çalıştırır; eksik OCR değerlerini hesaplamaz."""
    def issue(code, detail):
        result['issues'].append({'code': code, 'detail': detail})
    for key in ['invoice_date', 'invoice_number', 'currency']:
        if result[key] is None:
            issue('missing_field', key)
    if not result['seller']['name']:
        issue('missing_field', 'seller.name')
    if not result['items']:
        issue('missing_items', 'Kalem çıkarılamadı; farklı bir şablon gerekebilir')
    for key in ['net', 'vat', 'gross']:
        if result['totals'][key] is None:
            issue('missing_field', 'totals.' + key)
    def check(field, observed, calculated):
        ok = abs(Decimal(observed) - calculated) <= Decimal('.01')
        result['checks'].append({'field': field, 'observed': observed, 'calculated': str(calculated), 'ok': ok})
        if not ok:
            issue('amount_mismatch', field)
    net = vat_sum = Decimal(0)
    complete = bool(result['items'])
    for i, item in enumerate(result['items']):
        if any(item[k] is None for k in ('quantity', 'unit_price', 'line_total', 'vat_rate')):
            issue('missing_item_field', f'items.{i}')
            complete = False
            continue
        check(f'items.{i}.line_total', item['line_total'], money(Decimal(item['quantity']) * Decimal(item['unit_price'])))
        total, rate = Decimal(item['line_total']), Decimal(item['vat_rate']) / 100
        if item['total_basis'] == 'unknown' or rate < 0:
            complete = False
            continue
        line_net = total if item['total_basis'] == 'net' else money(total / (1 + rate))
        net += line_net
        vat_sum += money(line_net * rate) if item['total_basis'] == 'net' else total - line_net
    totals = result['totals']
    # Genel indirimleri farklı KDV matrahlarına kanıt olmadan dağıtmayız.
    if any(totals[k] is not None and Decimal(totals[k]) != 0 for k in ('discount', 'withholding')):
        issue('adjustment_review', 'İndirim/tevkifat okundu; matrah dağılımı elle doğrulanmalı')
        complete = False
    if complete:
        for key, calculated in [('net', net), ('vat', vat_sum), ('gross', net + vat_sum)]:
            if totals[key] is not None:
                check('totals.' + key, totals[key], money(calculated))
    if all(totals[k] is not None for k in ('net', 'vat', 'gross')):
        check('totals.gross_identity', totals['gross'], Decimal(totals['net']) + Decimal(totals['vat']))
    if totals['payable'] is not None and totals['gross'] is not None:
        if totals['withholding'] is not None:
            check('totals.payable', totals['payable'], Decimal(totals['gross']) - Decimal(totals['withholding']))
        elif Decimal(totals['payable']) != Decimal(totals['gross']):
            issue('payable_difference', 'Ödenecek tutar farkının nedeni doğrulanmalı')
    result['needs_review'] = bool(result['issues'])
    return result


def run(path, template=None):
    """Tek PDF için tek fatura kaydı döndürür; süre ve süreç tepe belleğini ölçer."""
    started = time.perf_counter()
    pages = extract(path)
    if template is None:
        result = parse(pages, str(path))
    else:
        from fatura.extraction.layout import adapt_pages
        adapted, problems = adapt_pages(pages, template)
        result = parse(adapted, str(path))
        # Denetim için kaynak metin her zaman özgün haliyle tutulur.
        result['pages'] = pages
        result['issues'].extend(problems)
        result['needs_review'] = bool(result['issues'])
    result['metrics'] = {'elapsed_seconds': round(time.perf_counter() - started, 4),
                         'process_peak_rss_bytes': peak_rss_bytes()}
    return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('pdf', type=Path)
    cli.add_argument('-o', '--output', type=Path, required=True)
    cli.add_argument('--template', type=Path, help='Konum tabanlı tablo şablonu (JSON)')
    args = cli.parse_args()
    try:
        from fatura.extraction.layout import load_template
        template = load_template(args.template) if args.template else None
        result = run(args.pdf, template)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"{len(result['items'])} kalem; kontrol gerekli: {result['needs_review']}")
    except Exception as exc:
        cli.exit(1, f'İşlem başarısız: {exc}\n')

if __name__ == '__main__':
    main()
