import os, tempfile, sqlite3, time, unittest, asyncio
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, patch
os.environ['DATA_DIR']=tempfile.mkdtemp()
import bot as b

class Chains(unittest.IsolatedAsyncioTestCase):
    async def test_intervening_claims_spawns_and_resets(self):
        old=b.db,b.eco.SET_DEFINITIONS,b.CATALOG,b.spawn_lock
        b.db=sqlite3.connect(':memory:'); old[0].backup(b.db); b.db.row_factory=sqlite3.Row
        b.eco.SET_DEFINITIONS={'pets':{'name':'Pets','members':{'cat','dog'}}}
        b.CATALOG={k:{'id':k,'name':k,'image_url':''} for k in ('cat','dog','bird')}
        b.spawn_lock=asyncio.Lock()
        channel=S(id=2,send=AsyncMock(return_value=S(id=100)),fetch_message=AsyncMock(return_value=S(edit=AsyncMock(),delete=AsyncMock())))
        async def claim(owner,card='cat'):
            with b.db:
                b.db.execute("INSERT OR REPLACE INTO spawns(guild,channel,message,country,expires,rarity) VALUES(1,2,100,?,?,'normal')",(card,time.time()+300))
            await b.on_message(S(id=101,content='claim',webhook_id=None,guild=S(id=1),author=S(id=owner,bot=False,display_name=str(owner)),channel=channel))
        def streak(owner):
            r=b.db.execute('SELECT streak FROM chain_streaks WHERE guild=1 AND owner=?',(owner,)).fetchone()
            return r[0] if r else 0
        try:
            with patch.object(b,'set_card_embed_image',AsyncMock(return_value=None)):
                await claim(1); await claim(2)
                before=b.eco.balance(b.db,1,1)
                await claim(1,'dog')
                self.assertEqual((streak(1),streak(2)),(2,1))
                self.assertGreaterEqual(b.eco.balance(b.db,1,1)-before,1100)
                self.assertIn('+100 bonus',channel.send.call_args.args[0])
                with patch.object(b,'bounty_spawn_pool',return_value=[(b.CATALOG['bird'],'normal',1)]):
                    await b.spawn(1,channel)
                self.assertEqual(streak(1),2)
                with b.db: b.db.execute('UPDATE spawns SET expires=0')
                with patch.object(b.bot,'get_channel',return_value=channel): await b.expire_spawns()
                self.assertEqual(streak(1),2)
                await claim(2,'bird')
                self.assertEqual((streak(1),streak(2)),(2,0))
                await claim(1)
                self.assertEqual(streak(1),3)
                self.assertIn('+200 bonus',channel.send.call_args.args[0])
                with tempfile.TemporaryDirectory() as folder:
                    disk=sqlite3.connect(folder+'/save.db');b.db.backup(disk);b.db.close();b.db=disk;b.db.row_factory=sqlite3.Row
                    await claim(1,'dog');self.assertEqual(streak(1),4)
        finally:
            b.db.close();b.db,b.eco.SET_DEFINITIONS,b.CATALOG,b.spawn_lock=old
