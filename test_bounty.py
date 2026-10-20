import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock,patch
from test_tracking import TrackingTests
import bot as b
import economy as e


class BountyTests(unittest.IsolatedAsyncioTestCase):
    interaction=TrackingTests.interaction
    message=TrackingTests.message
    seed=TrackingTests.seed

    async def asyncSetUp(self):
        await TrackingTests.asyncSetUp(self)
        self.old_active=b.ACTIVE_ITEMS
        b.ACTIVE_ITEMS=[dict(item,weight=2) for item in b.CATALOG.values()]
        b.db.execute('CREATE TABLE settings(guild INTEGER PRIMARY KEY,channel INTEGER,next_spawn REAL)')
        b.db.execute('INSERT INTO settings VALUES(1,2,0)');b.db.commit()

    async def asyncTearDown(self):
        b.ACTIVE_ITEMS=self.old_active
        await TrackingTests.asyncTearDown(self)

    def add(self,owner=1,card='cat',rarity='gold',guild=1):
        with b.db:
            b.db.execute('INSERT OR REPLACE INTO bounties VALUES(?,?,?,?)',(guild,owner,card,rarity))

    async def test_exact_pair_boost_no_stacking_or_other_guild(self):
        before={(i['id'],r):w for i,r,w in b.bounty_spawn_pool(1)}
        self.add();self.add(owner=2)
        after={(i['id'],r):w for i,r,w in b.bounty_spawn_pool(1)}
        for key in before:
            self.assertEqual(after[key],before[key]*(1.5 if key==('cat','gold') else 1))
        self.assertEqual(before,{(i['id'],r):w for i,r,w in b.bounty_spawn_pool(2)})

    async def test_spawn_notification_and_ten_minute_expiry(self):
        self.add();self.add(owner=2)
        channel=S(id=2,send=AsyncMock(return_value=S(id=100)))
        with patch.object(b.random,'choices',return_value=[(b.CATALOG['cat'],'gold',1)]):
            self.assertTrue(await b.spawn(1,channel))
        kw=channel.send.call_args.kwargs
        self.assertIn('<@1>',kw['content']);self.assertIn('<@2>',kw['content'])
        allowed=kw['allowed_mentions'].to_dict()
        self.assertEqual(set(allowed['users']),{1,2})
        self.assertNotIn('everyone',allowed.get('parse',[]))
        self.assertIn('10 minutes',kw['embed'].footer.text)
        row=b.db.execute('SELECT * FROM spawns').fetchone()
        self.assertAlmostEqual(row['expires']-time.time(),600,delta=2)
        self.assertEqual(b.db.execute('SELECT next_spawn FROM settings').fetchone()[0],row['expires'])

    async def test_normal_spawn_five_minutes_and_no_ping(self):
        self.add(card='cat',rarity='gold')
        channel=S(id=2,send=AsyncMock(return_value=S(id=100)))
        with patch.object(b.random,'choices',return_value=[(b.CATALOG['dog'],'normal',1)]):
            await b.spawn(1,channel)
        self.assertIsNone(channel.send.call_args.kwargs['content'])
        self.assertAlmostEqual(b.db.execute('SELECT expires FROM spawns').fetchone()[0]-time.time(),300,delta=2)

    async def test_bounty_command_replaces_and_rejects_invalid(self):
        i=self.interaction()
        await b.bountyset.callback(i,'cat','gold')
        await b.bountyset.callback(i,'dog','silver')
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM bounties').fetchone()[0],1)
        row=b.db.execute('SELECT * FROM bounties').fetchone()
        self.assertEqual((row['card'],row['rarity']),('dog','silver'))
        await b.bountyset.callback(i,'missing','gold')
        self.assertEqual(b.db.execute('SELECT card FROM bounties').fetchone()[0],'dog')
        self.assertFalse(i.response.send_message.call_args.kwargs['ephemeral'])

    async def test_only_target_holder_claim_clears_bounty(self):
        self.add(rarity='normal')
        self.seed();await b.on_message(self.message(2,101))
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM bounties').fetchone()[0],1)
        self.seed(200);await b.on_message(self.message(1,201))
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM bounties').fetchone()[0],0)
        self.add(rarity='gold');self.seed(300)
        denied=self.message(1,301);await b.on_message(denied)
        denied.channel.send.assert_awaited_once_with("Users can't claim two cards in a row!")
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM bounties').fetchone()[0],1)

    async def test_bounty_expired_spawn_disappears_and_keeps_subscription(self):
        self.add();self.seed(expiry=-1)
        msg=S(delete=AsyncMock())
        channel=S(fetch_message=AsyncMock(return_value=msg))
        with patch.object(b.bot,'get_channel',return_value=channel):
            await b.expire_spawns()
        msg.delete.assert_awaited_once()
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM spawns').fetchone()[0],0)
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM bounties').fetchone()[0],1)

    async def test_bounties_persist(self):
        self.add()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'saved.db'
            dest=sqlite3.connect(path);b.db.backup(dest);dest.close()
            restored=sqlite3.connect(path)
            self.assertEqual(restored.execute('SELECT card,rarity FROM bounties').fetchone(),('cat','gold'))
            restored.close()


if __name__=='__main__':
    unittest.main()
