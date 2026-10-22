import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, patch
import bot as b
import economy as eco
from test_bot import new_db

class Profiles(unittest.IsolatedAsyncioTestCase):
    async def test_customization_favorites_and_cleanup(self):
        db=new_db();eco.migrate(db)
        with patch.object(b,'db',db):
            card=eco.allocate_card_id(db,1,10,'test','normal')
            i=S(guild_id=1,user=S(id=10,display_name='Tester'),response=S(send_message=AsyncMock(),defer=AsyncMock()),followup=S(send=AsyncMock()))
            await b.profilecustomize.callback(i,accentcolour='#64D545',footerimage='https://example.com/image.png',favoritecard=card,setcompletion=True)
            await b.profilecustomize.callback(i,accentcolour='112233')
            row=db.execute('SELECT * FROM profile_customizations').fetchone()
            self.assertEqual(row['accentcolour'],0x112233)
            self.assertEqual(row['favoritecard'],card)
            self.assertTrue(row['setcompletion'])
            await b.favorite.callback(i,card)
            self.assertIn('❤️',b.card_index(card))
            await b.profile.callback(i)
            embed=i.followup.send.call_args.kwargs['embed']
            self.assertEqual(embed.colour.value,0x112233)
            self.assertEqual(embed.image.url,'https://example.com/image.png')
            self.assertIn('Favorite card',[f.name for f in embed.fields])
            await b.favorite.callback(i,card)
            self.assertNotIn('❤️',b.card_index(card))
            await b.favorite.callback(i,card)
            db.execute('UPDATE cards SET owner=20 WHERE id=?',(card,))
            self.assertNotIn('❤️',b.card_index(card))
            self.assertIsNone(db.execute('SELECT favoritecard FROM profile_customizations').fetchone()[0])
            await b.profilecustomize.callback(i,accentcolour='bad',setcompletion=False)
            self.assertEqual(db.execute('SELECT setcompletion FROM profile_customizations').fetchone()[0],1)
        db.close()

    def test_sparkle_cutoff_and_flips(self):
        for rarity in eco.RARITIES:
            expected=rarity in ('holographic','inverted','glitch')
            self.assertEqual('✨' in b.variant_label(rarity),expected)
            self.assertIn('✨',b.variant_label(rarity,True,False))
            self.assertIn('✨',b.variant_label(rarity,False,True))
