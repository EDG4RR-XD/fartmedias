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
        self.assertEqual([self.claim(rarity=r)[1] for r in eco.RARITIES],[1000,0,0,0,1000,5000])
        self.claim(rarity='obsidian')
        self.assertEqual(eco.balance(self.db,1,1),7000)
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
        want=self.claim(2,'cat','obsidian')[0]
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


if __name__=='__main__':
    unittest.main()
