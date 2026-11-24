"""Fatura notunu etiket ve konum kanıtıyla bulur; etiketsiz metni not saymaz."""
import re
import unicodedata


def normalized(text):
    text = text.upper().replace('İ', 'I')
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def text_rows(page):
    """OCR sözcüklerini satıra toplar; uzaktaki bağımsız blokları ayırır."""
    rows = []
    for line in sorted(page['lines'], key=lambda l: (l['bbox'][1] + l['bbox'][3]) / 2):
        box = line['bbox']
        center = (box[1] + box[3]) / 2
        height = max(1, box[3] - box[1])
        if not rows or abs(rows[-1]['center'] - center) > height * .55:
            rows.append({'center': center, 'parts': [line]})
        else:
            rows[-1]['parts'].append(line)
    groups = []
    for row in rows:
        current = None
        for line in sorted(row['parts'], key=lambda l: l['bbox'][0]):
            b = line['bbox']
            if current is None or b[0] - current['bbox'][2] > page.get('width', 600) * .065:
                current = {'text': line['text'], 'page': page['page'], 'bbox': list(b)}
                groups.append(current)
            else:
                current['text'] += ' ' + line['text']
                current['bbox'] = [min(current['bbox'][0], b[0]), min(current['bbox'][1], b[1]),
                                   max(current['bbox'][2], b[2]), max(current['bbox'][3], b[3])]
    return groups


def extract_note(pages):
    """Fatura Notu/Notlar başlığının altındaki aynı bloktaki metni ve kanıtı döndürür."""
    texts, evidence = [], []
    for page in pages:
        groups = text_rows(page)
        active = False
        previous_bottom = left = height = 0
        for group in groups:
            text, b = group['text'].strip(), group['bbox']
            match = re.match(r'^(FATURA NOTU|FATURA NOTLARI|NOTLAR)(?:\s*[:：]\s*(.*))?$', normalized(text))
            if match:
                active = True
                left, previous_bottom, height = b[0], b[3], max(1, b[3]-b[1])
                inline = re.split(r'[:：]', text, maxsplit=1)
                if len(inline) == 2 and inline[1].strip():
                    texts.append(inline[1].strip())
                    evidence.append(group)
                continue
            if not active or b[0] > left + height * 2:
                continue
            if b[1] - previous_bottom > height * 2.8:
                active = False
                continue
            if normalized(text).startswith(('SAYFA ', 'SCANNED WITH', 'HTTP://', 'HTTPS://', 'BANKA HESAP BILGILERI')):
                active = False
                continue
            texts.append(text)
            evidence.append(group)
            previous_bottom = max(previous_bottom, b[3])
    return '\n'.join(texts) or None, evidence
