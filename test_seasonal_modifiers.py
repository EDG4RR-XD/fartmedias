import io,os,tempfile,unittest
from datetime import date,timedelta
from unittest.mock import patch,AsyncMock
from types import SimpleNamespace
from PIL import Image,ImageOps
os.environ['DATA_DIR']=tempfile.mkdtemp()
import bot as b

def png(image):
    out=io.BytesIO();image.save(out,'PNG');return out.getvalue()

class SeasonalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config={k:{'name':k.upper(),'TALL':k+'-tall.png','WIDE':k+'-wide.png'} for k in b.SEASONAL_WINDOWS}
        self.patch=patch.object(b,'MODIFIERS',self.config);self.patch.start()
    def tearDown(self):self.patch.stop()
    def test_date_boundaries_and_ten_percent(self):
        for key,(month,start,end) in b.SEASONAL_WINDOWS.items():
            for day in (start,end):
                with patch.object(b.random,'random',return_value=.099):
                    self.assertEqual(b.roll_seasonal_modifiers({'orientation':'TALL'},date(2026,month,day)),[key])
                with patch.object(b.random,'random',return_value=.10):
                    self.assertEqual(b.roll_seasonal_modifiers({'orientation':'TALL'},date(2026,month,day)),[])
            for outside in (date(2026,month,start)-timedelta(days=1),date(2026,month,end)+timedelta(days=1)):
                with patch.object(b.random,'random',return_value=0):
                    self.assertEqual(b.roll_seasonal_modifiers({'orientation':'TALL'},outside),[])
    def test_orientation_matching_and_unconfigured(self):
        self.assertEqual(b.seasonal_image({'orientation':'TALL'},'spooky'),'spooky-tall.png')
        self.assertEqual(b.seasonal_image({'orientation':'wide'},'spooky'),'spooky-wide.png')
        with patch.object(b.random,'random',return_value=0):
            self.assertEqual(b.roll_seasonal_modifiers({},date(2026,10,22)),[])
            self.config['spooky']['TALL']=''
            self.assertEqual(b.roll_seasonal_modifiers({'orientation':'TALL'},date(2026,10,22)),[])
    def test_overlays_follow_admin_and_flips(self):
        source=Image.new('RGBA',(3,4),(255,255,255,255))
        overlay=Image.new('RGBA',(3,4));overlay.putpixel((0,0),(255,0,255,255))
        raw=png(source)
        base=Image.open(b.transformed_image_bytes(raw,True,True,['admin']))
        expected=Image.alpha_composite(base,overlay)
        actual=Image.open(b.transformed_image_bytes(raw,True,True,['admin','spooky'],{'spooky':png(overlay)}))
        self.assertEqual(actual.tobytes(),expected.tobytes())
        self.assertEqual(actual.getpixel((0,0)),(255,0,255,255))
    def test_stacking_all_seasonal_overlays(self):
        source=Image.new('RGBA',(2,2))
        overlays={}
        for key,position in [('spooky',(0,0)),('hungry',(0,1)),('festive',(1,0))]:
            im=Image.new('RGBA',(2,2));im.putpixel(position,(255,0,0,255));overlays[key]=png(im)
        result=Image.open(b.transformed_image_bytes(png(source),False,True,list(overlays),overlays))
        self.assertEqual(sum(result.getchannel('A').getdata()),765)
    async def test_force_outside_window_and_removed_options(self):
        parameters={p.name for p in b.spawnrigged.parameters}
        self.assertTrue({'spooky','hungry','festive'}<=parameters)
        self.assertFalse({'tall','wide'}&parameters)
        self.assertFalse({'spooky','hungry','festive','tall','wide'} & {p.name for p in b.spawnspamrigged.parameters})
        item=next(iter(b.CATALOG.values()));edited=dict(item,orientation='TALL')
        interaction=SimpleNamespace(guild_id=987,response=SimpleNamespace(send_message=AsyncMock(),defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        with b.db:b.db.execute('INSERT OR REPLACE INTO settings VALUES(987,123,0)')
        with patch.dict(b.CATALOG,{item['id']:edited}),patch.object(b.bot,'get_channel',return_value=object()),patch.object(b,'spawn',new=AsyncMock(return_value=True)) as spawn:
            await b.spawnrigged.callback(interaction,item['id'],spooky=True,hungry=True,festive=True)
            self.assertEqual(spawn.call_args.args[-1],['spooky','hungry','festive'])

if __name__=='__main__':unittest.main()
