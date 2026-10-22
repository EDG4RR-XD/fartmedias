import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image, ImageOps, ImageChops
os.environ['DATA_DIR'] = tempfile.mkdtemp()
import bot as b

def png(image):
    out = io.BytesIO(); image.save(out, 'PNG'); return out.getvalue()

class ModifierTests(unittest.IsolatedAsyncioTestCase):
    def test_multiply_alpha_and_final_flips(self):
        source = Image.new('RGBA', (3, 4))
        source.putdata([(240, 120, 60, a) for a in (0, 64, 128, 255)*3])
        result = Image.open(b.transformed_image_bytes(png(source), modifiers=['admin']))
        self.assertEqual(result.size, source.size)
        self.assertEqual(result.getchannel('A').tobytes(), source.getchannel('A').tobytes())
        with Image.open(b.ROOT/'admin_gradient.png') as g:
            expected = ImageChops.multiply(source.convert('RGB'), g.convert('RGB').resize(source.size, Image.Resampling.BILINEAR))
        self.assertEqual(result.convert('RGB').tobytes(), expected.tobytes())
        flipped = Image.open(b.transformed_image_bytes(png(source), True, True, ['admin']))
        self.assertEqual(flipped.tobytes(), ImageOps.flip(ImageOps.mirror(result)).tobytes())


    def test_cloudy_uses_screen_and_preserves_alpha(self):
        source = Image.new('RGBA', (3, 4))
        source.putdata([(25, 80, 160, a) for a in (0, 64, 128, 255)*3])
        result = Image.open(b.transformed_image_bytes(png(source), modifiers=['cloudy']))
        self.assertEqual(result.size, source.size)
        self.assertEqual(result.getchannel('A').tobytes(), source.getchannel('A').tobytes())
        with Image.open(b.ROOT/'cloudy.png') as g:
            expected = ImageChops.screen(source.convert('RGB'), g.convert('RGB').resize(source.size, Image.Resampling.BILINEAR))
        self.assertEqual(result.convert('RGB').tobytes(), expected.tobytes())

    async def test_rigged_restrictions_and_regular_spawns(self):
        interaction=SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await b.spawnrigged.callback(interaction, next(iter(b.CATALOG)), 'gold', admin=True)
        self.assertIn('ADMIN only supports', interaction.response.send_message.call_args.args[0])
        with b.db:
            b.db.execute('DELETE FROM spawns WHERE guild=987')
            b.db.execute('INSERT OR REPLACE INTO settings VALUES(987,1,0)')
        channel=SimpleNamespace(id=1, send=AsyncMock(return_value=SimpleNamespace(id=123)))
        with patch.object(b,'set_card_embed_image',new=AsyncMock(return_value=None)) as render:
            await b.spawn(987,channel, next(iter(b.CATALOG.values())), 'normal')
            row=b.db.execute('SELECT * FROM spawns WHERE guild=987').fetchone()
            self.assertNotIn('admin', b.card_modifiers(row))
            b.db.execute('DELETE FROM spawns WHERE guild=987')
            await b.spawn(987,channel,next(iter(b.CATALOG.values())),None,True,True,['admin'])
            row=b.db.execute('SELECT * FROM spawns WHERE guild=987').fetchone()
            self.assertIn(row['rarity'], b.ADMIN_RARITIES)
            self.assertEqual(b.card_modifiers(row),['admin'])
            self.assertEqual(render.call_args.kwargs['modifiers'],['admin'])

    async def test_artist_credit(self):
        import discord
        embed=discord.Embed()
        with patch.object(b,'card_image_payload',new=AsyncMock(return_value=(None,None))):
            await b.set_card_embed_image(embed, {'ARTIST:':'Example Artist'},'normal')
        self.assertEqual(embed.fields[0].value,'art by Example Artist')

if __name__=='__main__': unittest.main()
