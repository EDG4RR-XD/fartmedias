import datetime
import asyncio
import importlib
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

os.environ['DATA_DIR']=tempfile.mkdtemp()
import bot as bot_module
import economy as eco


def new_db():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.executescript('CREATE TABLE cards(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,owner INTEGER,country TEXT); CREATE TABLE spawns(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER,country TEXT,expires REAL);')
    return db


class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.db=new_db()
        eco.migrate(self.db)

    def claim(self, owner=1,item='cat',rarity='normal'):
        with self.db:
            return eco.award_claim(self.db,1,owner,item,rarity)

    def test_claim_bonuses_and_milestones(self):
        rewards=[self.claim()[1] for _ in range(10)]
        self.assertEqual(rewards,[1000,0,0,0,1000,0,0,0,0,1000])
        with self.db:
            self.assertTrue(eco.sell(self.db,1,1,10,100))
        self.assertEqual(self.claim()[1],0)
        self.assertFalse(eco.sell(self.db,1,1,10,100))

    def test_variants_same_base_and_set_once(self):
        rewards=[self.claim(rarity=r)[1] for r in eco.RARITIES]
        self.assertEqual(rewards[0],1000)
        self.assertEqual(rewards[-1],5000)
        self.assertEqual(sum(rewards),9000)
        self.claim(rarity='obsidian')
        self.assertEqual(eco.balance(self.db,1,1),9000)
        with self.db:
            self.db.execute("DELETE FROM cards WHERE rarity='obsidian'")
        self.assertEqual(self.claim(rarity='obsidian')[1],0)

    def test_trade_authorization_bonuses_replay_and_no_milestone(self):
        a=self.claim()[0]
        b=self.claim(2,'dog')[0]
        with self.db:
            self.assertIsNone(eco.exchange(self.db,1,3,2,a,b))
            self.assertEqual(eco.exchange(self.db,1,1,2,a,b),[100,100])
            self.assertIsNone(eco.exchange(self.db,1,1,2,a,b))
        self.assertEqual(eco.balance(self.db,1,1),1100)
        # Incoming traded copies do not award a five-copy claim bonus.
        for _ in range(4):
            self.claim(1,'bird')
        other=self.claim(2,'bird')[0]
        with self.db:
            eco.exchange(self.db,1,1,2,b,other)
        self.assertEqual(self.claim(1,'bird')[1],0)  # sixth, not fifth

    def test_shop_balance_ownership_and_redemption_replay(self):
        with self.db:
            self.assertIsNone(eco.buy(self.db,1,1,'free_ych'))
            eco.credit(self.db,1,1,5000)
            item=eco.buy(self.db,1,1,'free_ych')
            self.assertIsNotNone(item)
            self.assertEqual(eco.balance(self.db,1,1),0)
            self.assertIsNone(eco.buy(self.db,1,1,'free_ych'))
            self.assertIsNone(eco.redeem(self.db,1,2,item))
            self.assertIsNone(eco.redeem(self.db,2,1,item))
            self.assertEqual(eco.redeem(self.db,1,1,item),'free_ych')
            self.assertIsNone(eco.redeem(self.db,1,1,item))

    def test_sale_owner_and_server(self):
        card=self.claim()[0]
        with self.db:
            self.assertFalse(eco.sell(self.db,2,1,card,200))
            self.assertFalse(eco.sell(self.db,1,2,card,200))
            self.assertTrue(eco.sell(self.db,1,1,card,200))
        self.assertEqual(eco.balance(self.db,1,1),1200)

    def test_legacy_migration_idempotent(self):
        db=new_db()
        db.execute("INSERT INTO cards(guild,owner,country) VALUES(1,1,'cat')")
        db.commit()
        eco.migrate(db); eco.migrate(db)
        self.assertEqual(db.execute('SELECT rarity FROM cards').fetchone()[0],'normal')
        with db:
            self.assertEqual(eco.award_claim(db,1,1,'cat','silver')[1],0)
        self.assertEqual(eco.balance(db,1,1),0)

    def test_set_completed_by_trade(self):
        for rarity in list(eco.RARITIES)[:-1]:
            self.claim(rarity=rarity)
        want=self.claim(2,'cat',list(eco.RARITIES)[-1])[0]
        give=self.claim(1,'dog')[0]
        with self.db:
            self.assertEqual(eco.exchange(self.db,1,1,2,give,want)[0],5100)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old=bot_module.db
        bot_module.db=new_db()
        eco.migrate(bot_module.db)
        bot_module.db.execute('CREATE TABLE settings(guild INTEGER PRIMARY KEY,channel INTEGER,next_spawn REAL)')
        bot_module.spawn_lock=asyncio.Lock()

    async def asyncTearDown(self):
        bot_module.db.close()
        bot_module.db=self.old

    def msg(self,user=1,content='Claim',channel=2,mid=101):
        return S(id=mid,content=content,webhook_id=None,guild=S(id=1),author=S(id=user,display_name='Test',bot=False),channel=S(id=channel,send=AsyncMock(),fetch_message=AsyncMock(return_value=S(edit=AsyncMock()))))

    async def test_concurrent_claims_and_wrong_messages(self):
        db=bot_module.db
        with db:
            db.execute("INSERT INTO spawns VALUES(1,2,100,'Canada',?,'gold')",(time.time()+300,))
        for m in [self.msg(content='Claim Canada'),self.msg(channel=3),self.msg(mid=99)]:
            await bot_module.on_message(m)
        await asyncio.gather(bot_module.on_message(self.msg()),bot_module.on_message(self.msg(user=2)))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],1)
        self.assertEqual(db.execute('SELECT rarity FROM cards').fetchone()[0],'gold')
        self.assertEqual(sum(r[0] for r in db.execute('SELECT amount FROM wallets')),1000)

    async def test_spawn_rarity_and_five_minutes(self):
        db=bot_module.db
        with db:
            db.execute('INSERT INTO settings VALUES(1,2,0)')
        channel=S(id=2,send=AsyncMock(return_value=S(id=100)))
        results=await asyncio.gather(bot_module.spawn(1,channel),bot_module.spawn(1,channel))
        self.assertEqual(results.count(True),1)
        row=db.execute('SELECT * FROM spawns').fetchone()
        self.assertIn(row['rarity'],eco.RARITIES)
        self.assertTrue(channel.send.call_args.kwargs['embed'].image.url)
        self.assertEqual(db.execute('SELECT next_spawn FROM settings').fetchone()[0],row['expires'])
        self.assertAlmostEqual(row['expires']-time.time(),300,delta=2)

    async def test_list_owned_above_missing_and_view_variant(self):
        with bot_module.db:
            card,_=eco.award_claim(bot_module.db,1,1,'Canada','silver')
        i=S(guild_id=1,user=S(id=1),response=S(send_message=AsyncMock()))
        await bot_module.card_list.callback(i,1)
        embed=i.response.send_message.call_args.kwargs['embed']
        self.assertIn('Canada',embed.fields[0].value)
        self.assertIn('Japan',embed.fields[1].value)
        self.assertFalse(embed.fields[0].inline)
        await bot_module.view_card.callback(i,card)
        self.assertIn('Silver',i.response.send_message.call_args.kwargs['embed'].description)


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_checks(self):
        import discord
        for command in (bot_module.cgive,bot_module.ctake,bot_module.spawnrigged,bot_module.backup_data):
            i=S(permissions=discord.Permissions.none(),user=S(id=5),created_at=datetime.datetime.now(datetime.timezone.utc))
            denied=False
            for check in command.checks:
                try:
                    result=check(i)
                    if hasattr(result,'__await__'):
                        await result
                except discord.app_commands.MissingPermissions:
                    denied=True
                    break
            self.assertTrue(denied,command.name)

    async def test_adjustment_capped_and_idempotent(self):
        db=new_db();eco.migrate(db)
        with db:
            self.assertEqual(eco.adjust_balance(db,1,9,2,300,100),(300,300))
            self.assertEqual(eco.adjust_balance(db,1,9,2,300,100),(300,300))
            self.assertEqual(eco.adjust_balance(db,1,9,2,-500,101),(-300,0))
        self.assertEqual(eco.balance(db,1,2),0)
        self.assertEqual(eco.balance(db,2,2),0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM wallet_adjustments').fetchone()[0],2)
        db.close()

    async def test_backup_full_restore_and_server_scope(self):
        import io,json,zipfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'live.db'
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE cards(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,owner INTEGER,country TEXT); CREATE TABLE spawns(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER,country TEXT,expires REAL);")
            eco.migrate(db)
            with db:
                eco.award_claim(db,1,123456789012345678,'cat','silver')
                eco.award_claim(db,2,222222222222222222,'dog','gold')
                eco.credit(db,1,123456789012345678,6000)
                purchase=eco.buy(db,1,123456789012345678,'free_ych')
            for guild in (None,1):
                payload=bot_module.export_database(path,guild)
                with zipfile.ZipFile(io.BytesIO(payload)) as z:
                    self.assertEqual(set(z.namelist()),{'flags.db','user-data.json','RESTORE.txt'})
                    data=json.loads(z.read('user-data.json'))
                    self.assertEqual(len(data['tables']['cards']),2 if guild is None else 1)
                    self.assertIsInstance(data['tables']['cards'][0]['owner'],str)
                    dest=Path(folder)/f'restore-{guild}.db';dest.write_bytes(z.read('flags.db'))
                restored=sqlite3.connect(dest);restored.row_factory=sqlite3.Row
                self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(eco.balance(restored,1,123456789012345678),2000)
                self.assertEqual(restored.execute('SELECT id FROM purchases').fetchone()[0],purchase)
                if guild:
                    self.assertEqual(restored.execute('SELECT COUNT(*) FROM wallets WHERE guild=2').fetchone()[0],0)
                restored.close()
            db.close()

    async def test_all_backup_nonowner_rejected(self):
        old=bot_module.bot.is_owner
        bot_module.bot.is_owner=AsyncMock(return_value=False)
        try:
            i=S(user=S(id=5),response=S(send_message=AsyncMock()))
            await bot_module.backup_data.callback(i,'all')
            self.assertTrue(i.response.send_message.call_args.kwargs['ephemeral'])
        finally:
            bot_module.bot.is_owner=old

    async def test_rigged_spawn(self):
        db=new_db();eco.migrate(db)
        db.execute('CREATE TABLE settings(guild INTEGER PRIMARY KEY,channel INTEGER,next_spawn REAL)')
        db.execute('INSERT INTO settings VALUES(1,2,0)');db.commit()
        old=bot_module.db;bot_module.db=db;bot_module.spawn_lock=asyncio.Lock()
        try:
            channel=S(id=2,send=AsyncMock(return_value=S(id=900)))
            self.assertTrue(await bot_module.spawn(1,channel,bot_module.CATALOG['Japan'],'obsidian'))
            row=db.execute('SELECT * FROM spawns').fetchone()
            self.assertEqual((row['country'],row['rarity']),('Japan','obsidian'))
            self.assertFalse(await bot_module.spawn(1,channel,bot_module.CATALOG['Canada'],'gold'))
        finally:
            bot_module.db=old;db.close()


class NewFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=new_db();eco.migrate(self.db)
        self.old_definitions=eco.SET_DEFINITIONS
        eco.SET_DEFINITIONS={}

    async def asyncTearDown(self):
        eco.SET_DEFINITIONS=self.old_definitions
        self.db.close()

    async def test_transfer_atomic_replay_and_no_minting(self):
        with self.db:
            eco.credit(self.db,1,1,1000)
            self.assertFalse(eco.transfer(self.db,1,1,2,1001,1))
            self.assertFalse(eco.transfer(self.db,1,1,1,100,2))
            self.assertFalse(eco.transfer(self.db,1,1,2,0,3))
            self.assertTrue(eco.transfer(self.db,1,1,2,400,4))
            self.assertTrue(eco.transfer(self.db,1,1,2,400,4))
            self.assertFalse(eco.transfer(self.db,1,1,3,400,4))
            self.assertFalse(eco.transfer(self.db,2,1,2,100,5))
        self.assertEqual(eco.balance(self.db,1,1),600)
        self.assertEqual(eco.balance(self.db,1,2),400)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM currency_transfers').fetchone()[0],1)

    async def test_sets_claim_and_recompletion(self):
        eco.configure_sets(self.db,[{'id':'pets','name':'Pets'}],[{'id':'cat','sets':['pets']},{'id':'dog','sets':['pets']}])
        with self.db:
            self.assertEqual(eco.award_claim(self.db,1,1,'cat','normal')[1],1000)
            card,reward=eco.award_claim(self.db,1,1,'dog','gold')
            self.assertEqual(reward,11000)
            self.assertEqual(eco.card_set_bonus(self.db,1,1),0)
            self.assertEqual(eco.card_set_bonus(self.db,2,1),0)
            self.db.execute('DELETE FROM cards WHERE id=?',(card,))
            self.assertEqual(eco.award_claim(self.db,1,1,'dog','silver')[1],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM completed_card_sets').fetchone()[0],1)

    async def test_sets_trade_and_legacy_existing_ownership(self):
        eco.configure_sets(self.db,[{'id':'pets','name':'Pets'}],[{'id':'cat','sets':['pets']},{'id':'dog','sets':['pets']}])
        with self.db:
            eco.award_claim(self.db,1,1,'cat','normal')
            give,_=eco.award_claim(self.db,1,1,'bird','normal')
            want,_=eco.award_claim(self.db,1,2,'dog','normal')
            self.assertEqual(eco.exchange(self.db,1,1,2,give,want)[0],10100)
        eco.configure_sets(self.db,[{'id':'birds','name':'Birds'}],[{'id':'bird','sets':['birds']}])
        with self.db:
            self.assertEqual(eco.card_set_bonus(self.db,1,2),10000)
            self.assertEqual(eco.card_set_bonus(self.db,1,2),0)

    async def test_set_config_errors_empty_set_no_reward(self):
        with self.assertRaises(ValueError):
            eco.configure_sets(self.db,[],[{'id':'cat','sets':['missing']}])
        with self.assertRaises(ValueError):
            eco.configure_sets(self.db,[{'id':'x','name':'X'},{'id':'x','name':'Y'}],[])
        eco.configure_sets(self.db,[{'id':'empty','name':'Empty'}],[])
        with self.db:
            self.assertEqual(eco.card_set_bonus(self.db,1,1),0)

    async def test_draw_no_payment_and_empty_pool(self):
        old_db,old_pool=bot_module.db,bot_module.DRAW_POOL
        bot_module.db=self.db
        try:
            i=S(guild_id=1,user=S(id=1),response=S(send_message=AsyncMock()))
            bot_module.DRAW_POOL=[]
            await bot_module.draw.callback(i)
            self.assertTrue(i.response.send_message.call_args.kwargs['ephemeral'])
            bot_module.DRAW_POOL=[{'image_url':'https://example.com/oc.png'}]
            await bot_module.draw.callback(i)
            embed=i.response.send_message.call_args.kwargs['embed']
            self.assertEqual(embed.description,'Draw this OC (Coloured, any) for 7500 Chudlets!')
            self.assertEqual(embed.image.url,'https://example.com/oc.png')
            self.assertEqual(eco.balance(self.db,1,1),0)
        finally:
            bot_module.db,bot_module.DRAW_POOL=old_db,old_pool

    async def test_new_variant_odds_and_image_overrides(self):
        self.assertEqual(len(eco.RARITIES),16)
        self.assertAlmostEqual(sum(eco.RARITIES.values()),100)
        self.assertEqual(eco.RARITIES['silver'],10)
        item={'image_url':'https://example.com/base.png','rarity_images':{'glitch':'https://example.com/glitch.png'}}
        self.assertEqual(bot_module.image_for(item,'glitch'),'https://example.com/glitch.png')
        self.assertEqual(bot_module.image_for(item,'pearl'),'https://example.com/base.png')


if __name__=='__main__':
    unittest.main()
