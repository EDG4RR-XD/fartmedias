import asyncio
import logging
import os
import random
import sqlite3
import time
import json
import math
from urllib.parse import urlparse
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import economy as eco

load_dotenv()
logging.basicConfig(level=logging.INFO)
DATA = Path(os.getenv('DATA_DIR', './data'))
DATA.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA / 'flags.db')
db.row_factory = sqlite3.Row
db.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings(guild INTEGER PRIMARY KEY, channel INTEGER, next_spawn REAL);
CREATE TABLE IF NOT EXISTS spawns(guild INTEGER PRIMARY KEY, channel INTEGER, message INTEGER, country TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER, owner INTEGER, country TEXT);
''')
eco.migrate(db)
with db:
    if not db.execute("SELECT 1 FROM metadata WHERE key='five_minute_schedule'").fetchone():
        db.execute('UPDATE settings SET next_spawn=MIN(next_spawn,?)', (time.time()+300,))
        db.execute("INSERT INTO metadata VALUES('five_minute_schedule','1')")

# Customize items.json. Keep IDs stable once a collectible has been claimed.
ROOT = Path(__file__).resolve().parent
SPAWN_MIN_SECONDS = 300
SPAWN_MAX_SECONDS = 300
CLAIM_SECONDS = 300
with (ROOT / 'items.json').open(encoding='utf-8') as f:
    ITEMS = json.load(f)
if not isinstance(ITEMS, list) or not ITEMS:
    raise ValueError('items.json must contain a nonempty list of items.')
seen = set()
for item in ITEMS:
    if not isinstance(item, dict):
        raise ValueError('Each item must be a JSON object.')
    for key in ('id', 'name', 'image_url'):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError(f'Every item needs a nonempty {key}.')
    if len(item['id']) > 80 or len(item['name']) > 80:
        raise ValueError('Item IDs and names must be 80 characters or fewer.')
    if item['id'] in seen:
        raise ValueError(f"Duplicate item ID: {item['id']}")
    seen.add(item['id'])
    url = urlparse(item['image_url'])
    if url.scheme != 'https' or not url.netloc:
        raise ValueError('image_url must be a public HTTPS image link.')
    weight = item.get('weight', 10)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight < 0:
        raise ValueError('weight must be a finite number of zero or greater.')
    item['weight'] = weight
    price = item.get('sell_value', 100)
    if isinstance(price, bool) or not isinstance(price, int) or price < 0:
        raise ValueError('sell_value must be a nonnegative integer.')
    item['sell_value'] = price
    images = item.get('rarity_images', {})
    if not isinstance(images, dict) or set(images) - set(eco.RARITIES):
        raise ValueError('rarity_images must be an object with known rarity names.')
    for link in images.values():
        if not isinstance(link, str) or urlparse(link).scheme != 'https' or not urlparse(link).netloc:
            raise ValueError('Rarity images must be public HTTPS links.')
    prices = item.get('rarity_sell_values', {})
    if not isinstance(prices, dict) or set(prices)-set(eco.RARITIES) or any(isinstance(v,bool) or not isinstance(v,int) or v<0 for v in prices.values()):
        raise ValueError('rarity_sell_values must map known rarities to nonnegative integers.')
ACTIVE_ITEMS = [item for item in ITEMS if item['weight'] > 0]
if not ACTIVE_ITEMS:
    raise ValueError('At least one item must have a weight above zero.')
# Store definitions so removing an item from the spawn pool keeps old cards viewable.
db.execute('CREATE TABLE IF NOT EXISTS item_defs(id TEXT PRIMARY KEY, name TEXT, image_url TEXT)')
with db:
    db.executemany('INSERT INTO item_defs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,image_url=excluded.image_url',
                   [(i['id'], i['name'], i['image_url']) for i in ITEMS])
with db:
    db.executemany('INSERT INTO item_extras VALUES(?,?) ON CONFLICT(id) DO UPDATE SET definition=excluded.definition',
                   [(i['id'],json.dumps(i)) for i in ITEMS])
CATALOG = {r['id']: dict(r) for r in db.execute('SELECT * FROM item_defs')}
for row in db.execute('SELECT * FROM item_extras'):
    CATALOG[row['id']] = json.loads(row['definition'])


def item_info(item_id):
    return CATALOG.get(item_id, {'id': item_id, 'name': item_id, 'image_url': None, 'sell_value': 100})


def image_for(item, rarity):
    return item.get('rarity_images', {}).get(rarity) or item.get('image_url')


def sale_price(item, rarity):
    return item.get('rarity_sell_values', {}).get(rarity, item.get('sell_value', 100))


class FlagBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents,
                         allowed_mentions=discord.AllowedMentions.none())

    async def setup_hook(self):
        guild_id = os.getenv('TEST_GUILD_ID')
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        spawn_loop.start()


bot = FlagBot()


@bot.event
async def on_message(message):
    if message.author.bot or message.webhook_id or message.guild is None:
        return
    if message.content.strip().casefold() != 'claim':
        return
    # A queued claim from before the image appeared must never collect a new spawn.
    async with spawn_lock:
        row = db.execute('SELECT * FROM spawns WHERE guild=? AND channel=? AND expires>?',
                         (message.guild.id, message.channel.id, time.time())).fetchone()
        if not row or message.id <= row['message']:
            return
        with db:
            claimed = db.execute('DELETE FROM spawns WHERE guild=? AND message=?',
                                 (message.guild.id, row['message'])).rowcount
            if not claimed:
                return
            card_id, earned = eco.award_claim(db, message.guild.id, message.author.id, row['country'], row['rarity'])
    item = item_info(row['country'])
    # Ownership is committed before any Discord API calls.
    try:
        await message.channel.send(f"{discord.utils.escape_markdown(message.author.display_name)} claimed **{discord.utils.escape_markdown(item['name'])} ({row['rarity']})**! Card #{card_id}. +{earned} chudlets.")
        original = await message.channel.fetch_message(row['message'])
        embed = discord.Embed(title=item['name'], description=f"{row['rarity'].title()} · Claimed! Card #{card_id}.", color=0x79B86D)
        image = image_for(item, row['rarity'])
        if image:
            embed.set_image(url=image)
        await original.edit(content=None, embed=embed, view=None)
    except discord.HTTPException:
        logging.exception('Claim saved but could not update its Discord message')


spawn_lock = asyncio.Lock()


async def spawn(guild_id, channel):
    async with spawn_lock:
        return await _spawn(guild_id, channel)


async def _spawn(guild_id, channel):
    active = db.execute('SELECT 1 FROM spawns WHERE guild=? AND expires>?', (guild_id, time.time())).fetchone()
    if active:
        return False
    # Reserve the next time before awaiting Discord to prevent repeated attempts.
    with db:
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?',
                   (time.time() + random.randint(SPAWN_MIN_SECONDS, SPAWN_MAX_SECONDS), guild_id))
    item = random.choices(ACTIVE_ITEMS, weights=[i['weight'] for i in ACTIVE_ITEMS])[0]
    rarity = random.choices(list(eco.RARITIES), weights=list(eco.RARITIES.values()))[0]
    embed = discord.Embed(title=item['name'], description=f"{rarity.title()} · Type **Claim** in this channel to collect this image.", color=0x79B86D)
    embed.set_image(url=image_for(item, rarity))
    embed.set_footer(text=f'Expires in {CLAIM_SECONDS // 60} minutes.')
    message = await channel.send(embed=embed)
    spawned_at = time.time()
    with db:
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?', (spawned_at + 300, guild_id))
        db.execute('INSERT OR REPLACE INTO spawns(guild,channel,message,country,expires,rarity) VALUES(?,?,?,?,?,?)',
                   (guild_id, channel.id, message.id, item['id'], spawned_at + CLAIM_SECONDS, rarity))
    return True


@tasks.loop(seconds=5)
async def spawn_loop():
    for row in db.execute('SELECT * FROM settings WHERE next_spawn<=?', (time.time(),)).fetchall():
        try:
            channel = bot.get_channel(row['channel']) or await bot.fetch_channel(row['channel'])
            await spawn(row['guild'], channel)
        except discord.HTTPException:
            logging.exception('Could not spawn in guild %s', row['guild'])
            with db:
                db.execute('UPDATE settings SET next_spawn=? WHERE guild=?', (time.time()+300, row['guild']))


@spawn_loop.before_loop
async def before_spawns():
    await bot.wait_until_ready()


@bot.tree.command(name='setup', description='Choose the image spawn channel (Manage Server required).')
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    perms = channel.permissions_for(interaction.guild.me)
    if not all((perms.view_channel, perms.send_messages, perms.embed_links, perms.read_message_history)):
        return await interaction.response.send_message('Give me View Channel, Send Messages, Embed Links and Read Message History in that channel.', ephemeral=True)
    with db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?)', (interaction.guild_id, channel.id, time.time()))
    await interaction.response.send_message(f'Spawns enabled in {channel.mention}. First spawn within 5 seconds, then every 5 minutes.', ephemeral=True)


@bot.tree.command(name='spawn', description='Test a spawn (Manage Server required).')
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def force_spawn(interaction: discord.Interaction):
    row = db.execute('SELECT channel FROM settings WHERE guild=?', (interaction.guild_id,)).fetchone()
    if not row:
        return await interaction.response.send_message('Run /setup first.', ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(row['channel']) or await bot.fetch_channel(row['channel'])
    done = await spawn(interaction.guild_id, channel)
    await interaction.followup.send('Spawned.' if done else 'There is already an active image.', ephemeral=True)


@bot.tree.command(name='collection', description='View a collection and card IDs for trading.')
@app_commands.guild_only()
async def collection(interaction: discord.Interaction, member: discord.Member = None, page: app_commands.Range[int, 1, 100000] = 1):
    member = member or interaction.user
    total = db.execute('SELECT COUNT(*) FROM cards WHERE guild=? AND owner=?', (interaction.guild_id, member.id)).fetchone()[0]
    rows = db.execute('SELECT * FROM cards WHERE guild=? AND owner=? ORDER BY id LIMIT 20 OFFSET ?',
                      (interaction.guild_id, member.id, (page-1)*20)).fetchall()
    lines = [f"#{r['id']} · {discord.utils.escape_markdown(item_info(r['country'])['name'])} · {r['rarity']}" for r in rows]
    embed = discord.Embed(title=f'{member.display_name}’s collection', description='\n'.join(lines) or 'No cards on this page.')
    embed.set_footer(text=f'{total} cards · Page {page}/{max(1, (total+19)//20)} · Collections are per server')
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name='view', description='View a claimed image by its card ID.')
@app_commands.guild_only()
async def view_card(interaction: discord.Interaction, card_id: int):
    row = db.execute('SELECT * FROM cards WHERE id=? AND guild=?', (card_id, interaction.guild_id)).fetchone()
    if not row:
        return await interaction.response.send_message('No card with that ID in this server.', ephemeral=True)
    item = item_info(row['country'])
    embed = discord.Embed(title=item['name'], description=f"Card #{row['id']} · {row['rarity'].title()} · Owner: <@{row['owner']}>")
    image = image_for(item, row['rarity'])
    if image:
        embed.set_image(url=image)
    else:
        embed.set_footer(text='Legacy card: no image has been configured for this item.')
    await interaction.response.send_message(embed=embed, ephemeral=True)


class TradeView(discord.ui.View):
    def __init__(self, guild_id, sender, target, give, want):
        super().__init__(timeout=120)
        self.guild_id, self.sender, self.target, self.give, self.want = guild_id, sender, target, give, want
        self.finished = False

    @discord.ui.button(label='Accept trade', style=discord.ButtonStyle.green)
    async def accept(self, interaction, button):
        if interaction.user.id != self.target:
            return await interaction.response.send_message('Only the invited player can accept.', ephemeral=True)
        if self.finished:
            return await interaction.response.send_message('This offer has already been handled.', ephemeral=True)
        self.finished = True
        # Recheck both owners at acceptance, then exchange atomically.
        with db:
            rewards = eco.exchange(db,self.guild_id,self.sender,self.target,self.give,self.want)
            valid = rewards is not None
        self.stop()
        await interaction.response.edit_message(content=f'Trade completed. Players earned {rewards[0]} and {rewards[1]} chudlets respectively.' if valid else 'Trade cancelled: card ownership changed.', view=None)


@bot.tree.command(name='trade', description='Offer one of your cards in exchange for another player’s card.')
@app_commands.guild_only()
async def trade(interaction: discord.Interaction, member: discord.Member, give_id: int, want_id: int):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message('Choose another human player.', ephemeral=True)
    a = db.execute('SELECT * FROM cards WHERE id=? AND guild=? AND owner=?', (give_id, interaction.guild_id, interaction.user.id)).fetchone()
    b = db.execute('SELECT * FROM cards WHERE id=? AND guild=? AND owner=?', (want_id, interaction.guild_id, member.id)).fetchone()
    if not a or not b:
        return await interaction.response.send_message('Check the card IDs and their owners using /collection.', ephemeral=True)
    view = TradeView(interaction.guild_id, interaction.user.id, member.id, give_id, want_id)
    await interaction.response.send_message(f"{member.display_name}: {interaction.user.display_name} offers #{give_id} ({discord.utils.escape_markdown(item_info(a['country'])['name'])}, {a['rarity']}) for your #{want_id} ({discord.utils.escape_markdown(item_info(b['country'])['name'])}, {b['rarity']}). Accept within 2 minutes. Otherwise the offer expires.", view=view)


@bot.tree.command(name='balance', description='Check your chudlets balance.')
@app_commands.guild_only()
async def wallet(interaction: discord.Interaction):
    await interaction.response.send_message(f'You have {eco.balance(db,interaction.guild_id,interaction.user.id):,} chudlets.', ephemeral=True)


@bot.tree.command(name='list', description='All cards, with owned cards above unowned cards.')
@app_commands.guild_only()
async def card_list(interaction: discord.Interaction, page: app_commands.Range[int,1,100000] = 1):
    rows = db.execute('SELECT country,rarity,COUNT(*) AS n FROM cards WHERE guild=? AND owner=? GROUP BY country,rarity', (interaction.guild_id,interaction.user.id)).fetchall()
    owned = {}
    for row in rows:
        owned.setdefault(row['country'], {})[row['rarity']] = row['n']
    ids = set(CATALOG) | set(owned)
    yes = sorted((i for i in ids if i in owned), key=lambda i:item_info(i)['name'].casefold())
    no = sorted((i for i in ids if i not in owned), key=lambda i:item_info(i)['name'].casefold())
    pages = max(1,(max(len(yes),len(no))+2)//3)
    if page > pages:
        return await interaction.response.send_message(f'There are {pages} pages.',ephemeral=True)
    offset=(page-1)*3
    top=[]
    for i in yes[offset:offset+3]:
        counts=owned[i]
        variants=', '.join(f'{r}: {counts.get(r,0)}' for r in eco.RARITIES)
        top.append(f"**{discord.utils.escape_markdown(item_info(i)['name'])}** ({sum(counts.values())})\n{variants}")
    bottom=[discord.utils.escape_markdown(item_info(i)['name']) for i in no[offset:offset+3]]
    embed=discord.Embed(title='Card list')
    embed.add_field(name=f'Owned ({len(yes)})',value='\n'.join(top) or 'None on this page.',inline=False)
    embed.add_field(name=f'Not owned ({len(no)})',value='\n'.join(bottom) or 'None on this page.',inline=False)
    embed.set_footer(text=f'Page {page}/{pages} · Use /list page:2 for the next page. Counts reflect current ownership.')
    await interaction.response.send_message(embed=embed,ephemeral=True)


class SellView(discord.ui.View):
    def __init__(self,guild,owner,card_id,price):
        super().__init__(timeout=60)
        self.guild,self.owner,self.card_id,self.price=guild,owner,card_id,price

    @discord.ui.button(label='Confirm sale',style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('This sale belongs to another player.',ephemeral=True)
        with db:
            sold=eco.sell(db,self.guild,self.owner,self.card_id,self.price)
        self.stop()
        await interaction.response.edit_message(content=f'Sold card #{self.card_id} for {self.price:,} chudlets.' if sold else 'Sale cancelled: you no longer own that card.',view=None)

    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('This sale belongs to another player.',ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content='Sale cancelled.',view=None)


@bot.tree.command(name='sell',description='Sell one owned card for its configured chudlet value.')
@app_commands.guild_only()
async def sell_card(interaction:discord.Interaction,card_id:int):
    row=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? AND id=?',(interaction.guild_id,interaction.user.id,card_id)).fetchone()
    if not row:
        return await interaction.response.send_message('You do not own that card in this server.',ephemeral=True)
    item=item_info(row['country'])
    price=sale_price(item,row['rarity'])
    await interaction.response.send_message(f"Sell #{card_id}, {discord.utils.escape_markdown(item['name'])} ({row['rarity']}), for {price:,} chudlets? This removes that copy from your collection. Confirm within 60 seconds.",view=SellView(interaction.guild_id,interaction.user.id,card_id,price),ephemeral=True)


class ShopSelect(discord.ui.Select):
    def __init__(self,owner):
        self.owner=owner
        super().__init__(placeholder='Choose a reward to buy', options=[discord.SelectOption(label=name,value=key,description=f'{price:,} chudlets') for key,(name,price) in eco.SHOP.items()])

    async def callback(self,interaction):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('Open your own /shop.',ephemeral=True)
        product=self.values[0]
        with db:
            purchase=eco.buy(db,interaction.guild_id,self.owner,product)
        if purchase is None:
            return await interaction.response.send_message('You do not have enough chudlets.',ephemeral=True)
        await interaction.response.send_message(f'Bought {eco.SHOP[product][0]}. Item #{purchase}. Redeem with /use-item item_id:{purchase}. Balance: {eco.balance(db,interaction.guild_id,self.owner):,} chudlets.',ephemeral=True)


@bot.tree.command(name='shop',description='Buy art rewards with chudlets.')
@app_commands.guild_only()
async def shop(interaction:discord.Interaction):
    menu=discord.ui.View(timeout=180)
    menu.add_item(ShopSelect(interaction.user.id))
    text='\n'.join(f'{name}: {price:,} chudlets' for name,price in eco.SHOP.values())
    await interaction.response.send_message(f'Balance: {eco.balance(db,interaction.guild_id,interaction.user.id):,} chudlets\n\n{text}\n\nSelecting a reward purchases one copy. Use /inventory to find purchased items.',view=menu,ephemeral=True)


@bot.tree.command(name='inventory',description='View purchased rewards and their redemption IDs.')
@app_commands.guild_only()
async def inventory(interaction:discord.Interaction,page:app_commands.Range[int,1,100000]=1):
    rows=db.execute('SELECT * FROM purchases WHERE guild=? AND owner=? ORDER BY id DESC LIMIT 15 OFFSET ?',(interaction.guild_id,interaction.user.id,(page-1)*15)).fetchall()
    text='\n'.join(f"#{r['id']} · {eco.SHOP[r['product']][0]} · {'Redeemed' if r['used'] else 'Unused'}" for r in rows)
    await interaction.response.send_message(text or 'No purchased items on this page.',ephemeral=True)


@bot.tree.command(name='use-item',description='Redeem a purchased art reward and receive a claim receipt.')
@app_commands.guild_only()
async def use_item(interaction:discord.Interaction,item_id:int):
    with db:
        product=eco.redeem(db,interaction.guild_id,interaction.user.id,item_id)
    if product is None:
        return await interaction.response.send_message('That item is already redeemed or is not yours. Use /receipt to display an existing redemption.',ephemeral=True)
    await interaction.response.send_message(redemption_text(interaction.user.id,item_id,product))


def redemption_text(owner,item_id,product):
    return f'<@{owner}> redeemed **{eco.SHOP[product][0]}**. Receipt #{item_id}. Please ping @jonathan.sims with your claim and this receipt. This reward has been marked used.'


@bot.tree.command(name='receipt',description='Display one of your saved redemption receipts again.')
@app_commands.guild_only()
async def receipt(interaction:discord.Interaction,item_id:int):
    row=db.execute('SELECT * FROM purchases WHERE id=? AND guild=? AND owner=? AND used=1',(item_id,interaction.guild_id,interaction.user.id)).fetchone()
    if not row:
        return await interaction.response.send_message('No redeemed item with that ID belongs to you.',ephemeral=True)
    await interaction.response.send_message(redemption_text(interaction.user.id,item_id,row['product']))


@bot.tree.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = 'You need Manage Server permission for this command.'
    else:
        logging.error('Command failed', exc_info=(type(error), error, error.__traceback__))
        text = 'Command failed. Check the hosting logs and my channel permissions.'
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in your .env file or hosting environment.')
    bot.run(token)
