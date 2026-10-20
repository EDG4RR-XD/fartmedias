import os
import tempfile
import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock,patch

os.environ['DATA_DIR']=tempfile.mkdtemp()
from test_tracking import database
import bot as b
import economy as e


class CardShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_db,self.old_sets=b.db,e.SET_DEFINITIONS
        b.db=database();e.SET_DEFINITIONS={}
        self.db=b.db

    async def asyncTearDown(self):
        self.db.close();b.db,e.SET_DEFINITIONS=self.old_db,self.old_sets

    def interaction(self,user=1,request=10):
        return S(id=request,guild_id=1,user=S(id=user,display_name='Player'),response=S(send_message=AsyncMock(),edit_message=AsyncMock()))

    async def test_price_is_five_times_actual_sale_value(self):
        view=b.CardShopView(1,1)
        view.card_id=b.ITEMS[0]['id'];view.rarity='gold';view.refresh_controls()
        expected=b.sale_price(b.item_info(view.card_id),'gold')*5
        self.assertIn(f'{expected:,}',view.purchase.label)
        self.assertIn(f'{expected:,}',view.make_embed().fields[-1].value)

    async def test_purchase_balance_ownership_and_idempotency(self):
        with self.db:
            e.credit(self.db,1,1,1000)
            result=e.buy_card(self.db,1,1,'cat','gold',500,11)
            self.assertEqual(result[1],0)
            self.assertEqual(e.buy_card(self.db,1,1,'cat','gold',500,11),result)
            self.assertIsNone(e.buy_card(self.db,1,2,'cat','gold',500,11))
        self.assertEqual(e.balance(self.db,1,1),500)
        card=self.db.execute('SELECT * FROM cards').fetchone()
        self.assertEqual((card['guild'],card['owner'],card['country'],card['rarity']),(1,1,'cat','gold'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM discoveries').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM milestones').fetchone()[0],0)

    async def test_insufficient_funds_and_invalid_price_no_card(self):
        with self.db:
            self.assertIsNone(e.buy_card(self.db,1,1,'cat','normal',500,12))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],0)
        with self.assertRaises(ValueError):
            with self.db:e.buy_card(self.db,1,1,'cat','normal',-1,13)
        with self.assertRaises(ValueError):
            with self.db:e.buy_card(self.db,1,1,'cat','missing',100,14)

    async def test_named_set_completion_pays_once(self):
        e.SET_DEFINITIONS={'pets':{'name':'Pets','members':{'cat','dog'}}}
        with self.db:
            self.db.execute("INSERT INTO cards(guild,owner,country,rarity) VALUES(1,1,'cat','normal')")
            e.credit(self.db,1,1,2000)
            self.assertEqual(e.buy_card(self.db,1,1,'dog','gold',500,15)[1],10000)
            self.assertEqual(e.buy_card(self.db,1,1,'dog','silver',500,16)[1],0)
        self.assertEqual(e.balance(self.db,1,1),11000)

    async def test_rarity_completion_pays_once(self):
        with self.db:
            for rarity in list(e.RARITIES)[:-1]:
                self.db.execute("INSERT INTO cards(guild,owner,country,rarity) VALUES(1,1,'cat',?)",(rarity,))
            e.credit(self.db,1,1,2000)
            self.assertEqual(e.buy_card(self.db,1,1,'cat',list(e.RARITIES)[-1],500,17)[1],5000)
            self.assertEqual(e.buy_card(self.db,1,1,'cat','normal',500,18)[1],0)
        self.assertEqual(e.balance(self.db,1,1),6000)

    async def test_ui_card_and_rarity_selection_pagination_and_owner(self):
        view=b.CardShopView(1,1)
        self.assertTrue(view.purchase.disabled)
        cards=next(c for c in view.children if isinstance(c,b.CardShopCardSelect))
        rarities=next(c for c in view.children if isinstance(c,b.CardShopRaritySelect))
        self.assertEqual(len(cards.options),min(25,len(b.ITEMS)))
        self.assertFalse(await view.interaction_check(self.interaction(user=2)))
        self.assertTrue(await view.interaction_check(self.interaction()))
        cards._values=[b.ITEMS[0]['id']]
        await cards.callback(self.interaction())
        self.assertTrue(view.purchase.disabled)
        rarities._values=['normal'];await rarities.callback(self.interaction())
        self.assertFalse(view.purchase.disabled)
        if len(b.ITEMS)>25:
            await view.next_page.callback(self.interaction())
            self.assertEqual(view.page,1)
            self.assertEqual(len(cards.options),min(25,len(b.ITEMS)-25))
            self.assertTrue(view.purchase.disabled)

    async def test_ui_purchase_no_repeat_and_public(self):
        view=b.CardShopView(1,1);view.card_id=b.ITEMS[0]['id'];view.rarity='normal';view.refresh_controls()
        price=b.sale_price(b.item_info(view.card_id),'normal')*5
        with self.db:e.credit(self.db,1,1,price*3)
        await view.purchase.callback(self.interaction())
        self.assertTrue(view.finished)
        before=e.balance(self.db,1,1)
        await view.purchase.callback(self.interaction(request=19))
        self.assertEqual(e.balance(self.db,1,1),before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],1)
        i=self.interaction();await b.cardshop.callback(i)
        self.assertFalse(i.response.send_message.call_args.kwargs['ephemeral'])

    async def test_purchase_failure_rolls_back_everything(self):
        with self.db:e.credit(self.db,1,1,1000)
        with self.assertRaises(RuntimeError):
            with self.db:
                with patch.object(e,'set_bonus',side_effect=RuntimeError('test rollback')):
                    e.buy_card(self.db,1,1,'cat','normal',500,21)
        self.assertEqual(e.balance(self.db,1,1),1000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cards').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM card_purchases').fetchone()[0],0)


if __name__=='__main__':unittest.main()
