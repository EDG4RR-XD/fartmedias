import io
import json
import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, patch
from PIL import Image
import bot as b
import economy as eco
from test_bot import new_db


class SortAndBounce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = new_db()
        eco.migrate(self.db)
        self.dbpatch = patch.object(b, 'db', self.db)
        self.dbpatch.start()
        self.catalogpatch = patch.dict(b.CATALOG, {
            'a': {'id':'a','name':'Zebra','image_url':'https://example.com/a.png','sell_value':10},
            'b': {'id':'b','name':'Apple','image_url':'https://example.com/b.png','sell_value':100},
        })
        self.catalogpatch.start()
        self.ids = [eco.allocate_card_id(self.db,1,10,item,rarity) for item,rarity in [('a','normal'),('b','glitch'),('a','gold')]]

    def tearDown(self):
        self.catalogpatch.stop()
        self.dbpatch.stop()
        self.db.close()

    def rows(self):
        return self.db.execute('SELECT * FROM cards WHERE guild=1 AND owner=10').fetchall()

    def choose(self, mode):
        self.db.execute('INSERT OR REPLACE INTO collection_sorts VALUES(1,10,?)',(mode,))

    async def test_all_modes_and_pagination(self):
        self.assertEqual(len(b.SORT_MODES),27)
        for mode in b.SORT_MODES:
            self.choose(mode)
            self.assertEqual(set(r['id'] for r in b.sort_collection(self.rows(),1,10)),set(self.ids))
        for mode, expected in [('name_ascending',[2,1,3]),('rarity_descending',[2,3,1]),('copies_descending',[1,3,2]),('id_descending',[3,2,1])]:
            self.choose(mode)
            self.assertEqual([r['id'] for r in b.sort_collection(self.rows(),1,10)],expected)
        i=S(guild_id=1,user=S(id=10,display_name='Tester'),response=S(send_message=AsyncMock()))
        await b.sort_command.callback(i,'name_ascending')
        await b.send_collection(i,None,1)
        self.assertIn('#2',i.response.send_message.call_args.kwargs['embed'].description.splitlines()[0])
        self.assertEqual([r['id'] for r in b.sort_collection(self.rows(),2,10)], self.ids)
        choices=await b.sort_autocomplete(i,'modifier count descending')
        self.assertEqual(choices[0].value,'modifiers_descending')
        self.assertLessEqual(len(await b.sort_autocomplete(i,'')),25)

    def test_bounce_frames_stack_and_no_clipping(self):
        source=Image.new('RGBA',(40,50),(100,130,160,255))
        out=io.BytesIO();source.save(out,'PNG')
        overlay=io.BytesIO();Image.new('RGBA',source.size,(255,0,0,30)).save(overlay,'PNG')
        for rarity in eco.RARITIES:
            data=b.transformed_image_bytes(out.getvalue(),True,True,['admin','cloudy','spooky','bouncy'],{'spooky':overlay.getvalue()})
            animation=Image.open(data)
            self.assertEqual(animation.format,'GIF')
            self.assertGreater(animation.n_frames,2)
            self.assertEqual(animation.info['loop'],0)
            self.assertEqual(animation.size,(40,54))
            bounds=[]
            duration=0
            for n in range(animation.n_frames):
                animation.seek(n)
                duration += animation.info['duration']
                box=animation.convert('RGBA').getbbox();bounds.append(box)
                self.assertEqual(box[3]-box[1],50)
            self.assertEqual(duration,1000)
            self.assertEqual(min(box[1] for box in bounds),0)
            self.assertEqual(max(box[1] for box in bounds),4)

    async def test_profile_thumbnail_attachment_and_footer(self):
        card=self.ids[1]
        self.db.execute('INSERT INTO profile_customizations(guild,owner,favoritecard,footerimage) VALUES(1,10,?,?)',(card,'https://example.com/footer.png'))
        self.db.execute('UPDATE cards SET modifiers=? WHERE id=?',(json.dumps(['bouncy']),card))
        i=S(guild_id=1,user=S(id=10,display_name='Tester'),response=S(defer=AsyncMock(),send_message=AsyncMock()),followup=S(send=AsyncMock()))
        out=io.BytesIO();Image.new('RGB',(40,50),'red').save(out,'PNG')
        with patch.object(b,'download_modifier_image',new=AsyncMock(return_value=out.getvalue())):
            await b.profile.callback(i)
        kwargs=i.followup.send.call_args.kwargs
        self.assertEqual(kwargs['embed'].thumbnail.url,'attachment://favorite.gif')
        self.assertEqual(kwargs['file'].filename,'favorite.gif')
        self.assertEqual(kwargs['embed'].image.url,'https://example.com/footer.png')
        kwargs['file'].close()

    async def test_rigged_bouncy_accepts_every_rarity(self):
        self.db.execute('CREATE TABLE settings(guild INTEGER PRIMARY KEY,channel INTEGER,next_spawn REAL)')
        self.db.execute('INSERT INTO settings VALUES(1,42,0)')
        i=S(guild_id=1,user=S(id=10),response=S(defer=AsyncMock(),send_message=AsyncMock()),followup=S(send=AsyncMock()))
        channel=S(id=42)
        with patch.object(b.bot,'get_channel',return_value=channel), patch.object(b,'spawn',new=AsyncMock(return_value=True)) as spawn:
            for rarity in eco.RARITIES:
                await b.spawnrigged.callback(i,'a',rarity,bouncy=True)
                self.assertIn('bouncy',spawn.call_args.args[-1])
                self.assertEqual(spawn.call_args.args[3],rarity)
        self.assertIn('bouncy',{p.name for p in b.spawnspamrigged.parameters})


    async def test_manual_order_moves_and_validation(self):
        i=S(guild_id=1,user=S(id=10),response=S(send_message=AsyncMock()))
        await b.manualsort.callback(i,3,True)
        await b.manualsort.callback(i,1)
        self.assertEqual([r['id'] for r in b.sort_collection(self.rows(),1,10)],[3,1,2])
        await b.manualsort.callback(i,3)
        self.assertEqual(b.manual_ids(1,10),[3,1])
        await b.manualsortbefore.callback(i,2,3)
        self.assertEqual(b.manual_ids(1,10),[2,3,1])
        await b.manualsortafter.callback(i,2,1)
        self.assertEqual(b.manual_ids(1,10),[3,1,2])
        await b.manualsortbefore.callback(i,999,1)
        self.assertEqual(b.manual_ids(1,10),[3,1,2])
        self.choose('name_ascending')
        self.choose('manual')
        self.assertEqual([r['id'] for r in b.sort_collection(self.rows(),1,10)],[3,1,2])
        await b.sortindex.callback(i)
        self.assertIn('27. Manual order',i.response.send_message.call_args.args[0])

    def test_hyper_stack_and_canvas(self):
        source=Image.new('RGBA',(40,50),(100,130,160,255))
        out=io.BytesIO();source.save(out,'PNG')
        for mods in (['hyper'],['hyper','bouncy'],['hyper','bouncy','admin','cloudy']):
            animation=Image.open(b.transformed_image_bytes(out.getvalue(),True,True,mods))
            self.assertEqual(animation.format,'GIF')
            self.assertEqual(animation.n_frames,40)
            self.assertEqual(animation.size,(65,69 if 'bouncy' in mods else 65))
            duration=0
            for n in range(animation.n_frames):
                animation.seek(n)
                duration+=animation.info['duration']
                box=animation.convert('RGBA').getbbox()
                self.assertTrue(box and box[0]>=0 and box[2]<=65)
            self.assertEqual(duration,2000)
        self.assertIn('hyper',{p.name for p in b.spawnrigged.parameters})
        self.assertIn('hyper',{p.name for p in b.spawnspamrigged.parameters})
