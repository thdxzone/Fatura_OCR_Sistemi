"""Konum tabanlı tablo şablonları; belge dışı vergi esası varsayımı yapılmaz."""
from copy import deepcopy
import json
from pathlib import Path

HEADER = 'Ürün | Açıklama | Miktar | Birim | Birim Fiyat | KDV | Satır Toplam | Esas'
FIELDS = ('name', 'description', 'quantity', 'unit', 'unit_price', 'vat_rate', 'line_total')


def load_template(path):
    """Kullanıcının belirlediği sütun sınırlarını ve başlıkları doğrular."""
    template = json.loads(Path(path).read_text(encoding='utf-8'))
    columns = template['columns']
    if set(columns) != set(FIELDS):
        raise ValueError('Şablon yedi kalem sütununu içermeli: ' + ', '.join(FIELDS))
    ranges = []
    for field, bounds in columns.items():
        if len(bounds) != 2 or not all(isinstance(x, (int, float)) for x in bounds):
            raise ValueError(f'Geçersiz sütun sınırı: {field}')
        if not 0 <= bounds[0] < bounds[1]:
            raise ValueError(f'Geçersiz sütun aralığı: {field}')
        ranges.append(bounds)
    ranges.sort()
    if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
        raise ValueError('Şablon sütunları çakışmamalı')
    if not template.get('header_contains') or not template.get('stop_prefixes'):
        raise ValueError('Başlık ve tablo bitiş etiketleri gerekli')
    return template


def adapt_pages(pages, template):
    """Yalnızca tanınan tablolardaki hücreleri ortak ayrıştırıcıya aktarır.

    Ham sayfalar değiştirilmez. Başlıksız devam sayfası otomatik kabul edilmez.
    Satır kaydırmaları bir önceki kaleme tahminle birleştirilmez.
    """
    adapted = deepcopy(pages)
    problems = []
    for page in adapted:
        in_table = False
        # Vergi esası sadece belgenin açık ifadesinden okunur.
        page_text = '\n'.join(line['text'] for line in page['lines'])
        basis = 'unknown'
        net_marker = template.get('net_marker')
        gross_marker = template.get('gross_marker')
        net = bool(net_marker and net_marker in page_text)
        gross = bool(gross_marker and gross_marker in page_text)
        if net != gross:
            basis = 'net' if net else 'gross'
        for line in page['lines']:
            text = line['text']
            if all(token in text for token in template['header_contains']):
                in_table = True
                line['text'] = HEADER
                continue
            if any(text.startswith(prefix) for prefix in template['stop_prefixes']):
                in_table = False
            if not in_table:
                continue
            cells = {field: [] for field in FIELDS}
            outside = []
            for word in line.get('words', []):
                # Sol kenar hücre aidiyetini belirler; sütunu aşan sözcük kontrol ister.
                matches = [field for field, (left, right) in template['columns'].items()
                           if left <= word['x0'] < right]
                if len(matches) != 1:
                    outside.append(word['text'])
                    continue
                field = matches[0]
                cells[field].append(word['text'])
                if word['x1'] > template['columns'][field][1]:
                    outside.append(word['text'])
            if outside or not line.get('words'):
                problems.append({'code': 'layout_overflow', 'detail': f"Sayfa {page['page']}: {text}"})
            values = [' '.join(cells[field]) for field in FIELDS]
            if any('|' in value for value in values):
                problems.append({'code': 'layout_separator', 'detail': text})
                continue
            line['text'] = ' | '.join(values + [basis])
    return adapted, problems
