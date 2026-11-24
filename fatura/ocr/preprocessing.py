"""OCR için çizgili tabloların okuma kopyasını hazırlar; kaynak belgeyi değiştirmez."""
from PIL import Image, ImageDraw, ImageOps


def long_runs(image, minimum):
    """Koyu, kesintisiz uzun yatay çizgileri bulur; kısa harf vuruşlarını seçmez."""
    width, height = image.size
    pixels = image.tobytes()
    for y in range(height):
        start = None
        offset = y * width
        for x in range(width + 1):
            dark = x < width and pixels[offset+x] < 155
            if dark and start is None:
                start = x
            elif not dark and start is not None:
                if x-start >= minimum:
                    yield start, y, x-1
                start = None


def remove_table_lines(image):
    """Uzun yatay/dikey çizgileri beyaz maskeyle temizler; koordinatlar aynı kalır."""
    gray = ImageOps.autocontrast(image.convert('L'))
    mask = Image.new('L', image.size, 0)
    draw = ImageDraw.Draw(mask)
    for left, y, right in long_runs(gray, max(45, image.width//22)):
        draw.line([(left,y),(right,y)], fill=255, width=3)
    transposed = gray.transpose(Image.Transpose.TRANSPOSE)
    for top, x, bottom in long_runs(transposed, max(45, image.height//35)):
        draw.line([(x,top),(x,bottom)], fill=255, width=3)
    cleaned = image.convert('RGB').copy()
    cleaned.paste('white', mask=mask)
    return cleaned


def align_regions_to_grid(image, regions):
    """Yalnızca yakındaki gerçek çizgilerle hücre kenarlarını hizalar."""
    gray = ImageOps.autocontrast(image.convert('L'))
    vertical = [(x, top, bottom) for top, x, bottom in
                long_runs(gray.transpose(Image.Transpose.TRANSPOSE), max(45, image.height//35))]
    aligned = {}
    for key, bounds in regions.items():
        value = list(bounds)
        if key.startswith('item.'):
            y0, y1 = bounds[1]*image.height, bounds[3]*image.height
            edges = [x for x, top, bottom in vertical
                     if min(bottom,y1)-max(top,y0) > (y1-y0)*.65]
            for index in (0,2):
                x = bounds[index]*image.width
                near = [edge for edge in edges if abs(edge-x) <= image.width*.022]
                if near:
                    edge = min(near,key=lambda edge:abs(edge-x))
                    # Çizgiyi değil, çizginin içindeki metni keseriz.
                    value[index] = (edge + (3 if index==0 else -3))/image.width
            if value[2] <= value[0]:
                value = list(bounds)
        aligned[key] = value
    return aligned


def enhance_header_words(image, ocr, backend):
    """Küçük tablo başlığını büyütüp koordinatları tekrar kaynak ölçeğine taşır."""
    from copy import deepcopy
    import re
    from statistics import median
    anchors = [w for w in ocr['lines'] if re.sub(r'[^A-Z]', '', w['text'].upper()) == 'MIKTAR']
    if not anchors:
        return ocr
    anchor = anchors[0]
    height = median(max(1,w['bbox'][3]-w['bbox'][1]) for w in ocr['lines'])
    center = (anchor['bbox'][1]+anchor['bbox'][3])/2
    top, bottom = max(0,int(center-height*2.3)), min(image.height,int(center+height*2.3))
    crop = image.crop((0,top,image.width,bottom))
    enlarged = crop.resize((crop.width*3,crop.height*3), Image.Resampling.LANCZOS)
    words = backend.recognize(enlarged,psm=6)
    result = deepcopy(ocr)
    # İlk okumadaki doğru başlıklar ikinci denemede kaybolmasın.
    result['lines'] = list(result['lines'])
    for word in words:
        b = word['bbox']
        word['bbox'] = [b[0]/3,b[1]/3+top,b[2]/3,b[3]/3+top]
        result['lines'].append(word)
    return result


def recover_grid_rows(image, regions):
    """OCR'nin kaçırdığı satırları tam genişlikli yatay tablo çizgilerinden bulur."""
    from copy import deepcopy
    item_keys = [key for key in regions if key.startswith('item.')]
    if not item_keys or 'totals.net' not in regions:
        return regions
    template = {key.split('.',2)[2]:value for key,value in regions.items() if key.startswith('item.0.')}
    if not all(key in template for key in ('name','quantity','line_total')):
        return regions
    top = template['name'][1]*image.height
    bottom = regions['totals.net'][1]*image.height
    left = template['name'][0]*image.width
    right = template['line_total'][0]*image.width
    gray = ImageOps.autocontrast(image.convert('L'))
    from PIL import ImageFilter
    # Hafif eğik çizgilerin farklı piksel satırlarına dağılmasını birleştiririz.
    detection = gray.filter(ImageFilter.MinFilter(7))
    ys = sorted({y for start,y,end in long_runs(detection,max(50,image.width//3))
                 if top-25 <= y < bottom and start < left+12 and end > right})
    groups = []
    for y in ys:
        if not groups or y-groups[-1][-1] > 5:
            groups.append([y])
        else:
            groups[-1].append(y)
    edges = [sum(group)/len(group) for group in groups]
    cells = [(a+3,b-3) for a,b in zip(edges,edges[1:]) if b-a > 18]
    # Boş basılı çizelge satırları ürün değildir; hücre içinde gerçek mürekkep aranır.
    cleaned=remove_table_lines(image).convert('L')
    written=[]
    for y0,y1 in cells:
        bounds=template.get('unit_price',template['name'])
        crop=cleaned.crop((int(bounds[0]*image.width)+4,int(y0)+2,int(bounds[2]*image.width)-4,int(y1)-2))
        ink=sum(pixel<150 for pixel in crop.tobytes())
        if ink > max(15,crop.width*crop.height*.004):
            written.append((y0,y1))
    cells=written
    count = len({key.split('.')[1] for key in item_keys})
    if len(cells) <= count:
        return regions
    result = {key:value for key,value in deepcopy(regions).items() if not key.startswith('item.')}
    for i,(y0,y1) in enumerate(cells):
        for key,b in template.items():
            result[f'item.{i}.{key}'] = [b[0],y0/image.height,b[2],y1/image.height]
    return result
