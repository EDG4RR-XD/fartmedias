import os
import tempfile
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, patch
import bot as b
from test_bot import new_db
import economy as eco

class LockListings(unittest.IsolatedAsyncioTestCase):
    async def test_filtered_pages_and_search_markers(self):
        db=new_db()
        eco.migrate(db)
        with patch.object(b, 'db', db):
            for n in range(23):
                eco.allocate_card_id(db, 1, 10, 'test', 'normal', 1, 0)
            eco.allocate_card_id(db, 2, 10, 'test', 'normal')
            eco.allocate_card_id(db, 1, 11, 'test', 'normal')
            db.executemany('INSERT INTO sell_locks VALUES(1,10,?)', [(i,) for i in range(1,22)])
            self.assertEqual(b.card_index(1), '#1 🔒')
            self.assertEqual(b.card_index(22), '#22')
            i=S(guild_id=1,user=S(id=10,display_name='Tester'),response=S(send_message=AsyncMock()))
            await b.collectionlocked.callback(i, page=2)
            embed=i.response.send_message.call_args.kwargs['embed']
            self.assertTrue(embed.description.startswith('#21 🔒'))
            self.assertIn('21 cards · Page 2/2', embed.footer.text)
            await b.collectionunlocked.callback(i)
            embed=i.response.send_message.call_args.kwargs['embed']
            self.assertIn('#22',embed.description)
            self.assertIn('#23',embed.description)
            self.assertNotIn('🔒',embed.description)
            self.assertIn('2 cards',embed.footer.text)
            await b.collectionsearch.callback(i)
            self.assertIn('#1 🔒', i.response.send_message.call_args.kwargs['embed'].description)
            self.assertEqual(b.matched_card_ids(1,10,'')[0]['flip_horizontal'], 1)
            await b.cardsearch.callback(i)
            self.assertIn('#1 🔒', i.response.send_message.call_args.kwargs['embed'].description)
        db.close()
