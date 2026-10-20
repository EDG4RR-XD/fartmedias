"""Verify axes and stacking; run with: python -m unittest test_flips.py"""
import ast
import io
import unittest
from pathlib import Path
from PIL import Image, ImageOps

source=ast.parse(Path(__file__).with_name('bot.py').read_text())
function=next(node for node in source.body if isinstance(node,ast.FunctionDef) and node.name=='transformed_image_bytes')
namespace={'Image':Image,'ImageOps':ImageOps,'io':io}
exec(compile(ast.Module(body=[function],type_ignores=[]),'<transform>','exec'),namespace)
transform=namespace['transformed_image_bytes']

class FlipTest(unittest.TestCase):
    def test_axes_and_stack(self):
        im=Image.new('RGB',(2,2));im.putdata([(255,0,0),(0,255,0),(0,0,255),(255,255,0)])
        raw=io.BytesIO();im.save(raw,'PNG')
        def pixels(h,v):
            out=Image.open(transform(raw.getvalue(),h,v));return list(out.getdata())
        self.assertEqual(pixels(False,True),[(0,0,255),(255,255,0),(255,0,0),(0,255,0)])
        self.assertEqual(pixels(True,False),[(0,255,0),(255,0,0),(255,255,0),(0,0,255)])
        self.assertEqual(pixels(True,True),[(255,255,0),(0,0,255),(0,255,0),(255,0,0)])

if __name__=='__main__':unittest.main()
