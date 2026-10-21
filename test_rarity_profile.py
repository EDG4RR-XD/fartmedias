import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, patch
import bot as b
import economy as eco
from test_bot import new_db

class RarityProfile(unittest.IsolatedAsyncioTestCase):
    async def test_completion_toggle_and_migration(self):
        db=new_db();eco.migrate(db)
        db.execute('ALTER TABLE profile_customizations DROP COLUMN raritycompletion')
        db.execute('INSERT INTO profile_customizations(guild,owner,accentcolour) VALUES(1,10,123)')
        eco.migrate(db)
        self.assertEqual(db.execute('SELECT raritycompletion,accentcolour FROM profile_customizations').fetchone()[:], (0,123))
        with patch.object(b,'db',db):
            i=S(guild_id=1,user=S(id=10,display_name='Tester'),response=S(send_message=AsyncMock()))
            for rarity in eco.RARITIES:
                eco.allocate_card_id(db,1,10,'test',rarity)
            await b.profilecustomize.callback(i,raritycompletion=True)
            await b.profilecustomize.callback(i,setcompletion=False)
            await b.profile.callback(i)
            fields=i.response.send_message.call_args.kwargs['embed'].fields
            field=next(f for f in fields if f.name.startswith('Rarity sets completed'))
            self.assertEqual(field.name,'Rarity sets completed (1)')
            db.execute("DELETE FROM cards WHERE rarity='glitch'")
            await b.profile.callback(i)
            field=next(f for f in i.response.send_message.call_args.kwargs['embed'].fields if f.name.startswith('Rarity sets completed'))
            self.assertEqual(field.value,'None yet.')
            await b.profilecustomize.callback(i,raritycompletion=False)
            await b.profile.callback(i)
            self.assertFalse(any(f.name.startswith('Rarity sets completed') for f in i.response.send_message.call_args.kwargs['embed'].fields))
        db.close()
