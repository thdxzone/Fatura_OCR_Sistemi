"""Harfleri düşen numaralarda isteğe bağlı, yalnızca yerel ikinci okuma."""
import re
import sys


def check_identifier(image, regions, crops, backend):
    if 'invoice_number' not in regions or backend.name != 'tesseract':
        return
    primary = crops.get('regions',{}).get('invoice_number',[])
    text = ''.join(word['text'] for word in primary)
    attempts = crops.get('identifier_attempts',{}).get('invoice_number',[primary])
    ambiguous = text.isdigit() and any('$' in word['text'] for attempt in attempts for word in attempt)
    if not ambiguous:
        return
    crops['identifier_review'] = True
    crops['identifier_unresolved'] = True
    # Vision zorunlu bağımlılık değildir. Diğer platformlarda belirsiz değer doldurulmaz.
    if sys.platform != 'darwin':
        return
    try:
        from fatura.ocr.backends import VisionBackend
        second = VisionBackend()(image, {'invoice_number':regions['invoice_number']})
        observations = second['regions'].get('invoice_number',[])
        candidate = ''.join(word['text'] for word in observations)
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9]+',candidate):
            crops.setdefault('identifier_attempts',{}).setdefault('invoice_number',[]).append(observations)
            crops['regions']['invoice_number'] = observations
            crops['identifier_unresolved'] = False
    except Exception:
        # Tek alanın ek kontrolü bütün belgenin okunmasını engellemez.
        pass
