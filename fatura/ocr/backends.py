"""Ortak OCR çıktısı üreten yerel motorlar; Tesseract üç işletim sisteminde kullanılabilir."""
import csv
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageOps

from fatura.paths import MODELS_DIR, SCRIPTS_DIR


class VisionBackend:
    """Apple Vision isteğe bağlı macOS hızlandırıcısıdır."""
    name = 'apple_vision'

    def __init__(self):
        if sys.platform != 'darwin':
            raise ValueError('Vision yalnızca macOS üzerinde çalışır; --ocr-engine tesseract kullanın')
        if not shutil.which('swift'):
            raise ValueError('Vision için Swift araçları gerekli')

    def __call__(self, image, regions=None):
        command = ['swift', '-module-cache-path', str(Path(tempfile.gettempdir()) / 'fatura-swift-cache'),
                   str(SCRIPTS_DIR / 'vision_ocr.swift'), str(image)]
        if regions is None:
            result = json.loads(subprocess.check_output(command, text=True))
        else:
            with tempfile.TemporaryDirectory(prefix='fatura-regions-') as directory:
                config = Path(directory) / 'regions.json'
                config.write_text(json.dumps(regions), encoding='utf-8')
                result = json.loads(subprocess.check_output(command + [str(config)], text=True))
        result['engine'] = self.name
        return result


def prepare_quantity(image):
    """Boş kenarları azaltır, rakamı büyütür; yeni karakter eklemez."""
    gray = ImageOps.autocontrast(image.convert('L'))
    # Tarama zemini açık renkte olabilir; koyu mürekkebin sınırı kullanılır.
    ink = gray.point(lambda p: 255 if p < 150 else 0)
    bounds = ink.getbbox()
    if bounds is None:
        return None
    cropped = gray.crop(bounds)
    scale = max(2, min(6, 64 // max(1, cropped.height)))
    resized = cropped.resize((cropped.width * scale, cropped.height * scale), Image.Resampling.LANCZOS)
    return ImageOps.expand(resized, border=16, fill=255)


class TesseractBackend:
    """Tesseract CLI adaptörü; tüm koordinatlar sol üst başlangıçlı pikseldir."""
    name = 'tesseract'

    def __init__(self, executable='tesseract', languages='tur+eng', tessdata=None):
        self.executable = shutil.which(executable)
        if self.executable is None:
            raise ValueError('Tesseract bulunamadı; kurulumu yapın veya --tesseract ile tam yol verin')
        self.languages = languages
        local_data = MODELS_DIR / 'tessdata'
        if tessdata is None and all((local_data / (lang + '.traineddata')).exists() for lang in languages.split('+')):
            tessdata = local_data
        self.tessdata = tessdata
        command = [self.executable, '--list-langs']
        if tessdata:
            command.extend(['--tessdata-dir', str(tessdata)])
        available = set(subprocess.check_output(command, text=True).splitlines()[1:])
        missing = set(languages.split('+')) - available
        if missing:
            raise ValueError('Eksik Tesseract dil dosyaları: ' + ', '.join(sorted(missing)))

    def cleaned(self):
        """Aynı motorun çizgi temizlemeli kopyasını oluşturur; ikinci model yüklemez."""
        backend = TesseractBackend(self.executable, self.languages, self.tessdata)
        backend.table_cleanup = True
        return backend

    def recognize(self, image, psm=6, digits=False, whitelist=None):
        """TSV çıktısından metin, güven ve kutu bilgilerini alır."""
        with tempfile.TemporaryDirectory(prefix='fatura-tesseract-') as directory:
            path = Path(directory) / 'input.png'
            image.save(path)
            command = [self.executable, str(path), 'stdout', '-l', ('eng' if digits and 'eng' in self.languages.split('+') else self.languages), '--psm', str(psm)]
            if self.tessdata:
                command.extend(['--tessdata-dir', str(self.tessdata)])
            if digits:
                command.extend(['-c', 'tessedit_char_whitelist=0123456789,.'])
            elif whitelist:
                command.extend(['-c', 'tessedit_char_whitelist=' + whitelist])
            command.extend(['-c', 'tessedit_create_tsv=1', '-c', 'tessedit_create_txt=0'])
            proc = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
        observations = []
        for word in csv.DictReader(io.StringIO(proc.stdout), delimiter='\t', quoting=csv.QUOTE_NONE):
            if word['level'] != '5' or not word['text'].strip():
                continue
            left, top, width, height = (int(word[k]) for k in ('left', 'top', 'width', 'height'))
            observations.append({'text': word['text'], 'confidence': float(word['conf']) / 100,
                                 'bbox': [left, top, left + width, top + height]})
        return observations

    def quantity(self, image):
        """Çok basamaklı/kesirli miktarı iki segmentasyonla okur; anlaşmazlığı saklar."""
        prepared = prepare_quantity(image)
        if prepared is None:
            return [], []
        attempts = [self.recognize(prepared, psm=psm, digits=True) for psm in (8, 13)]
        texts = [''.join(word['text'] for word in attempt) for attempt in attempts]
        # İki okuma da aynı geçerli metni vermeden otomatik miktar kabul edilmez.
        valid = texts[0] == texts[1] and re.fullmatch(r'\d+(?:[.,]\d+)*', texts[0])
        if not valid:
            binary = prepared.point(lambda p:255 if p>150 else 0)
            attempts.extend(self.recognize(binary,psm=psm,digits=True) for psm in (6,7,8,13))
            readings=[''.join(w['text'] for w in attempt) for attempt in attempts]
            nonempty=[t for t in readings if re.fullmatch(r'\d+(?:[.,]\d+)*',t)]
            if not nonempty:
                from PIL import ImageFilter
                thickened=prepared.filter(ImageFilter.MinFilter(3))
                attempts.extend(self.recognize(thickened,psm=psm,digits=True) for psm in (8,13))
                nonempty=[''.join(w['text'] for w in a) for a in attempts if a]
                nonempty=[t for t in nonempty if re.fullmatch(r'\d+(?:[.,]\d+)*',t)]
            if len(nonempty)>=2 and len(set(nonempty))==1:
                return [{'text':nonempty[0],'confidence':min(w['confidence'] for a in attempts for w in a),
                         'bbox':[0,0,image.width,image.height]}], attempts
            return [], attempts
        return [{'text': texts[0], 'confidence': min(w['confidence'] for a in attempts for w in a),
                 'bbox': [0, 0, image.width, image.height]}], attempts

    def identifier(self, image):
        """Numarayı ayrı okur; benzer karakterleri yazılımla birbirine dönüştürmez."""
        primary = self.recognize(image)
        text = ''.join(o['text'] for o in primary).strip()
        if re.fullmatch(r'[A-Za-z0-9]+', text):
            return primary, [primary], text.isdigit()
        prepared = ImageOps.autocontrast(image.convert('L'))
        prepared = prepared.resize((prepared.width * 3, prepared.height * 3))
        prepared = prepared.point(lambda p: 255 if p > 150 else 0)
        prepared = ImageOps.expand(prepared, border=10, fill=255)
        attempts = [self.recognize(prepared, psm=psm,
                    whitelist='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for psm in (8, 13)]
        values = [''.join(o['text'] for o in attempt) for attempt in attempts]
        if values[0] == values[1] and re.fullmatch(r'[A-Za-z0-9]+', values[0]):
            # Alternatif okumadan gelen değer adaydır; kullanıcı kontrolü zorunlu kalır.
            return attempts[0], [primary, *attempts], True
        return primary, [primary, *attempts], True

    def __call__(self, path, regions=None):
        with Image.open(path) as original:
            image = original.convert('RGB')
        if getattr(self, 'table_cleanup', False):
            from fatura.ocr.preprocessing import remove_table_lines
            image = remove_table_lines(image)
        result = {'width': image.width, 'height': image.height,
                  'languages': self.languages.split('+'), 'engine': self.name}
        if regions is None:
            # Seyrek metin modu bağımsız satır numaralarını da arar.
            result['lines'] = self.recognize(image, psm=3 if getattr(self, 'table_cleanup', False) else 11)
            return result
        result['regions'], result['quantity_attempts'] = {}, {}
        for key, (x0, y0, x1, y1) in regions.items():
            crop = image.crop((round(x0*image.width), round(y0*image.height),
                               round(x1*image.width), round(y1*image.height)))
            if key.endswith(('.description', '.discount')) and not crop.convert('L').point(lambda p: 255 if p < 175 else 0).getbbox():
                observations = []
            elif key == 'invoice_number':
                observations, attempts, review = self.identifier(crop)
                result.setdefault('identifier_attempts', {})[key] = attempts
                if review:
                    result['identifier_review'] = True
            elif key.endswith('.quantity'):
                observations, attempts = self.quantity(crop)
                result['quantity_attempts'][key] = attempts
            elif key.endswith('.vat_rate'):
                # Küçük yüzde işaretini 4/7/9 sanan ilk okumada büyüterek yeniden dene.
                all_attempts = []
                observations = []
                for scale in (1, 2, 3):
                    resized = crop.resize((crop.width*scale, crop.height*scale), Image.Resampling.LANCZOS)
                    padded = ImageOps.expand(resized, border=10, fill='white')
                    attempts = [self.recognize(padded, psm=psm) for psm in (8, 13)]
                    all_attempts.extend(attempts)
                    texts = [''.join(o['text'] for o in a).replace(' ', '') for a in attempts]
                    candidate = texts[0].lstrip('%').replace(',', '.')
                    if texts[0] == texts[1] and re.fullmatch(r'\d+(?:\.\d+)?', candidate):
                        if 0 <= float(candidate) <= 100:
                            observations = attempts[0]
                            break
                result.setdefault('field_attempts', {})[key] = all_attempts

            else:
                observations = self.recognize(crop)
            result['regions'][key] = observations
        return result
