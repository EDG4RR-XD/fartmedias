import asyncio
import ast
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

os.environ['DATA_DIR']=tempfile.mkdtemp()
import bot as b
import economy as e


def database():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript('CREATE TABLE cards(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,owner INTEGER,country TEXT); CREATE TABLE spawns(guild INTEGER PRIMARY KEY,channel INTEGER,message INTEGER,country TEXT,expires REAL); CREATE TABLE claim_turns(guild INTEGER PRIMARY KEY,owner INTEGER NOT NULL); CREATE TABLE bounties(guild INTEGER,owner INTEGER,card TEXT,rarity TEXT,PRIMARY KEY(guild,owner));')
    e.migrate(conn)
    return conn


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_db,self.old_catalog,self.old_sets=b.db,b.CATALOG,e.SET_DEFINITIONS
        b.db=database();b.spawn_lock=asyncio.Lock()
        b.CATALOG={i:{'id':i,'name':i.title(),'image_url':'https://example.com/'+i+'.png'} for i in ('cat','dog','bird')}
        e.SET_DEFINITIONS={'pets':{'name':'Pets','members':{'cat','dog'}}}

    async def asyncTearDown(self):
        b.db.close();b.db,b.CATALOG,e.SET_DEFINITIONS=self.old_db,self.old_catalog,self.old_sets

    def interaction(self,owner=1,guild=1):
        return S(guild_id=guild,user=S(id=owner,display_name='Tester'),response=S(send_message=AsyncMock()))

    def message(self,owner,mid,guild=1,content='Claim',channel=2):
        return S(id=mid,content=content,webhook_id=None,guild=S(id=guild),author=S(id=owner,display_name='Tester',bot=False),channel=S(id=channel,send=AsyncMock(),fetch_message=AsyncMock(return_value=S(edit=AsyncMock()))))

    def seed(self,mid=100,guild=1,expiry=300):
        with b.db:
            b.db.execute("INSERT OR REPLACE INTO spawns VALUES(?,2,?,'cat',?,'normal')",(guild,mid,time.time()+expiry))

    def own(self,item,rarity='normal',owner=1,guild=1):
        with b.db:
            return b.db.execute('INSERT INTO cards(guild,owner,country,rarity) VALUES(?,?,?,?)',(guild,owner,item,rarity)).lastrowid

    async def test_claim_alternation_exact_error_and_no_award(self):
        self.seed();await b.on_message(self.message(1,101))
        before=e.balance(b.db,1,1)
        self.seed(200);denied=self.message(1,201)
        await b.on_message(denied)
        denied.channel.send.assert_awaited_once_with("Users can't claim two cards in a row!")
        self.assertEqual(e.balance(b.db,1,1),before)
        self.assertIsNotNone(b.db.execute('SELECT 1 FROM spawns').fetchone())
        await b.on_message(self.message(2,202))
        self.seed(300);await b.on_message(self.message(1,301))
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],3)

    async def test_same_player_other_server_allowed_and_failed_claim_ignored(self):
        self.seed();await b.on_message(self.message(1,101))
        self.seed(200,guild=2);await b.on_message(self.message(1,201,guild=2))
        self.seed(300);await b.on_message(self.message(2,301,content='hello'))
        await b.on_message(self.message(2,302,channel=9))
        self.assertEqual(b.db.execute('SELECT owner FROM claim_turns WHERE guild=1').fetchone()[0],1)
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],2)

    async def test_concurrent_claims_only_one_winner(self):
        self.seed()
        await asyncio.gather(b.on_message(self.message(1,101)),b.on_message(self.message(2,102)))
        self.assertEqual(b.db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],1)

    async def test_last_claim_survives_reopen(self):
        self.seed();await b.on_message(self.message(1,101))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'snapshot.db'
            disk=sqlite3.connect(path);b.db.backup(disk);disk.close()
            b.db.close();b.db=sqlite3.connect(path);b.db.row_factory=sqlite3.Row
            self.seed(200)
            m=self.message(1,201);await b.on_message(m)
            m.channel.send.assert_awaited_once_with("Users can't claim two cards in a row!")
            # Reconnect in memory before the temporary directory is removed.
            replacement=database();b.db.backup(replacement);b.db.close();b.db=replacement

    async def test_settrack_counts_unique_and_copies_missing_public(self):
        self.own('cat');self.own('cat','gold');self.own('dog',owner=2);self.own('dog',guild=2)
        i=self.interaction();await b.settrack.callback(i,'pets',1)
        kwargs=i.response.send_message.call_args.kwargs;embed=kwargs['embed']
        self.assertFalse(kwargs['ephemeral'])
        self.assertIn('1/2',embed.description);self.assertIn('2 copies',embed.description)
        self.assertIn('Cat',embed.fields[0].value);self.assertIn('Dog',embed.fields[1].value)
        self.assertEqual(e.balance(b.db,1,1),0)

    async def test_raritytrack_unique_copies_and_retired(self):
        self.own('cat');self.own('cat');self.own('cat','gold');self.own('cat','old_variant')
        i=self.interaction();await b.raritytrack.callback(i,'cat')
        embed=i.response.send_message.call_args.kwargs['embed']
        self.assertIn(f'2/{len(e.RARITIES)}',embed.description)
        self.assertIn('Normal · 2 copies',embed.fields[0].value)
        self.assertIn('Silver',embed.fields[1].value)
        self.assertIn('Old Variant',embed.fields[2].value)

    async def test_itemtrack_owned_and_missing_no_reward(self):
        self.own('cat');self.own('cat','gold')
        i=self.interaction();await b.itemtrack.callback(i,1)
        kwargs=i.response.send_message.call_args.kwargs
        self.assertFalse(kwargs['ephemeral'])
        self.assertIn('1/3',kwargs['embed'].description)
        self.assertIn('Cat · 2 copies',kwargs['embed'].fields[0].value)
        self.assertIn('Dog',kwargs['embed'].fields[1].value)
        self.assertEqual(e.balance(b.db,1,1),0)

    async def test_invalid_choices_and_autocomplete(self):
        for command,args in ((b.settrack,('unknown',1)),(b.raritytrack,('unknown',)),(b.itemtrack,(99,))):
            i=self.interaction();await command.callback(i,*args)
            self.assertFalse(i.response.send_message.call_args.kwargs['ephemeral'])
        self.assertEqual((await b.autocomplete_set(self.interaction(),'Pet'))[0].value,'pets')
        self.assertEqual((await b.autocomplete_rarity_card(self.interaction(),'Cat'))[0].value,'cat')

    async def test_every_explicit_command_response_public(self):
        tree=ast.parse(Path(b.__file__).read_text())
        settings=[kw.value for node in ast.walk(tree) if isinstance(node,ast.Call) for kw in node.keywords if kw.arg=='ephemeral']
        self.assertTrue(settings)
        self.assertTrue(all(isinstance(v,ast.Constant) and v.value is False for v in settings))


if __name__=='__main__':
    unittest.main()
