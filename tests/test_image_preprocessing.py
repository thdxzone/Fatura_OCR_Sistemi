"""Tablo çizgisi temizliği kaynak görsele dokunmadan metin vuruşlarını korumalıdır."""
import unittest
from PIL import Image, ImageDraw
from fatura.ocr.preprocessing import remove_table_lines, align_regions_to_grid


class PreprocessingTests(unittest.TestCase):
    def test_lines_removed_glyph_and_original_preserved(self):
        source = Image.new('RGB', (200,200), 'white')
        draw = ImageDraw.Draw(source)
        draw.line([(10,120),(190,120)],fill='black',width=2)
        draw.line([(20,10),(20,190)],fill='black',width=2)
        draw.rectangle((80,60,84,70),fill='black')
        original = source.tobytes()
        cleaned = remove_table_lines(source)
        self.assertEqual(source.tobytes(), original)
        self.assertEqual(cleaned.getpixel((20,100)), (255,255,255))
        self.assertEqual(cleaned.getpixel((100,120)), (255,255,255))
        self.assertEqual(cleaned.getpixel((82,65)), (0,0,0))

    def test_regions_snap_only_to_nearby_grid(self):
        source = Image.new('RGB', (200,200), 'white')
        draw = ImageDraw.Draw(source)
        for x in [40,140]:
            draw.line([(x,10),(x,190)],fill='black')
        regions = {'item.0.unit_price':[.22,.3,.68,.6], 'invoice_number':[.22,.3,.68,.6]}
        aligned = align_regions_to_grid(source, regions)
        self.assertAlmostEqual(aligned['item.0.unit_price'][0],43/200)
        self.assertAlmostEqual(aligned['item.0.unit_price'][2],137/200)
        self.assertEqual(aligned['invoice_number'],regions['invoice_number'])

    def test_grid_recovers_missing_rows_without_reading_amounts(self):
        from fatura.ocr.preprocessing import recover_grid_rows
        source = Image.new('RGB',(300,240),'white')
        draw = ImageDraw.Draw(source)
        for y in [60,90,120,150,180]:
            draw.line([(10,y),(285,y+2)],fill='black',width=1)
        for y in [70,100,130,160]:
            draw.rectangle((60,y,75,y+8),fill='black')
        regions={'totals.net':[.7,.85,.95,.95],
                 'item.0.name':[.1,.25,.4,.75],
                 'item.0.quantity':[.4,.25,.55,.75],
                 'item.0.line_total':[.8,.25,.95,.75]}
        recovered=recover_grid_rows(source,regions)
        self.assertEqual(len({key.split('.')[1] for key in recovered if key.startswith('item.')}),4)
        self.assertLess(recovered['item.0.name'][3],recovered['item.1.name'][1])
