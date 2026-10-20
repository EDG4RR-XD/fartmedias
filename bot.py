import io
import tempfile
import zipfile
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
    db.execute('CREATE TABLE IF NOT EXISTS claim_turns(guild INTEGER PRIMARY KEY, owner INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS bounties(guild INTEGER, owner INTEGER, card TEXT, rarity TEXT, PRIMARY KEY(guild,owner))')
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
sets_path = ROOT / 'sets.json'
with sets_path.open(encoding='utf-8') if sets_path.exists() else io.StringIO('[]') as f:
    eco.configure_sets(db,json.load(f),ITEMS)
# /draw uses its own pool. An absent file is treated as an empty pool.
draw_path = ROOT / 'draw_pool.json'
with draw_path.open(encoding='utf-8') if draw_path.exists() else io.StringIO('[]') as f:
    DRAW_POOL = json.load(f)
if not isinstance(DRAW_POOL,list):
    raise ValueError('draw_pool.json must contain a list.')
for entry in DRAW_POOL:
    if not isinstance(entry,dict) or not isinstance(entry.get('image_url'),str) or urlparse(entry['image_url']).scheme != 'https' or not urlparse(entry['image_url']).netloc:
        raise ValueError('Each draw entry needs a public HTTPS image_url.')
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
        last_claim = db.execute('SELECT owner FROM claim_turns WHERE guild=?', (message.guild.id,)).fetchone()
        if last_claim and last_claim['owner'] == message.author.id:
            await message.channel.send("Users can't claim two cards in a row!")
            return
        with db:
            claimed = db.execute('DELETE FROM spawns WHERE guild=? AND message=?',
                                 (message.guild.id, row['message'])).rowcount
            if not claimed:
                return
            card_id, earned = eco.award_claim(db, message.guild.id, message.author.id, row['country'], row['rarity'])
            db.execute('DELETE FROM bounties WHERE guild=? AND owner=? AND card=? AND rarity=?', (message.guild.id,message.author.id,row['country'],row['rarity']))
            db.execute('INSERT INTO claim_turns VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET owner=excluded.owner', (message.guild.id, message.author.id))
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


async def spawn(guild_id, channel, chosen_item=None, chosen_rarity=None):
    async with spawn_lock:
        return await _spawn(guild_id, channel, chosen_item, chosen_rarity)


async def _spawn(guild_id, channel, chosen_item=None, chosen_rarity=None):
    active = db.execute('SELECT 1 FROM spawns WHERE guild=? AND expires>?', (guild_id, time.time())).fetchone()
    if active:
        return False
    # Reserve the next time before awaiting Discord to prevent repeated attempts.
    with db:
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?',
                   (time.time() + random.randint(SPAWN_MIN_SECONDS, SPAWN_MAX_SECONDS), guild_id))
    pool=bounty_spawn_pool(guild_id,chosen_item,chosen_rarity)
    if not pool:
        raise ValueError('No positive spawn weights configured')
    item,rarity,_=random.choices(pool,weights=[pair[2] for pair in pool])[0]
    holders=[r['owner'] for r in db.execute('SELECT owner FROM bounties WHERE guild=? AND card=? AND rarity=? ORDER BY owner',(guild_id,item['id'],rarity))]
    lifetime=600 if holders else CLAIM_SECONDS
    embed = discord.Embed(title=item['name'], description=f"{rarity.title()} · Type **Claim** in this channel to collect this image.", color=0x79B86D)
    embed.set_image(url=image_for(item, rarity))
    embed.set_footer(text=f'Expires in {lifetime // 60} minutes.'+(' · Active bounty' if holders else ''))
    first=holders[:50]
    mentions=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=user) for user in first],replied_user=False)
    message=await channel.send(content=('Bounty target appeared! '+' '.join(f'<@{user}>' for user in first)) if first else None,embed=embed,allowed_mentions=mentions)
    spawned_at = time.time()
    with db:
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?', (spawned_at + lifetime, guild_id))
        db.execute('INSERT OR REPLACE INTO spawns(guild,channel,message,country,expires,rarity) VALUES(?,?,?,?,?,?)',
                   (guild_id, channel.id, message.id, item['id'], spawned_at + lifetime, rarity))
    # Keep mention batches within Discord content/mention limits.
    for offset in range(50,len(holders),50):
        batch=holders[offset:offset+50]
        try:
            await channel.send('Bounty target appeared! '+' '.join(f'<@{user}>' for user in batch),allowed_mentions=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=user) for user in batch],replied_user=False))
        except discord.HTTPException:
            logging.exception('Could not send an additional bounty notification')
    return True


@tasks.loop(seconds=5)
async def spawn_loop():
    await expire_spawns()
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
        return await interaction.response.send_message('Give me View Channel, Send Messages, Embed Links and Read Message History in that channel.', ephemeral=False)
    with db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?)', (interaction.guild_id, channel.id, time.time()))
    await interaction.response.send_message(f'Spawns enabled in {channel.mention}. First spawn within 5 seconds, then every 5 minutes.', ephemeral=False)


@bot.tree.command(name='spawn', description='Test a spawn (Manage Server required).')
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def force_spawn(interaction: discord.Interaction):
    row = db.execute('SELECT channel FROM settings WHERE guild=?', (interaction.guild_id,)).fetchone()
    if not row:
        return await interaction.response.send_message('Run /setup first.', ephemeral=False)
    await interaction.response.defer(ephemeral=False)
    channel = bot.get_channel(row['channel']) or await bot.fetch_channel(row['channel'])
    done = await spawn(interaction.guild_id, channel)
    await interaction.followup.send('Spawned.' if done else 'There is already an active image.', ephemeral=False)


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
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name='view', description='View a claimed image by its card ID.')
@app_commands.guild_only()
async def view_card(interaction: discord.Interaction, card_id: int):
    row = db.execute('SELECT * FROM cards WHERE id=? AND guild=?', (card_id, interaction.guild_id)).fetchone()
    if not row:
        return await interaction.response.send_message('No card with that ID in this server.', ephemeral=False)
    item = item_info(row['country'])
    embed = discord.Embed(title=item['name'], description=f"Card #{row['id']} · {row['rarity'].title()} · Owner: <@{row['owner']}>")
    image = image_for(item, row['rarity'])
    if image:
        embed.set_image(url=image)
    else:
        embed.set_footer(text='Legacy card: no image has been configured for this item.')
    await interaction.response.send_message(embed=embed, ephemeral=False)


class TradeView(discord.ui.View):
    def __init__(self, guild_id, sender, target, give, want):
        super().__init__(timeout=120)
        self.guild_id, self.sender, self.target, self.give, self.want = guild_id, sender, target, give, want
        self.finished = False

    @discord.ui.button(label='Accept trade', style=discord.ButtonStyle.green)
    async def accept(self, interaction, button):
        if interaction.user.id != self.target:
            return await interaction.response.send_message('Only the invited player can accept.', ephemeral=False)
        if self.finished:
            return await interaction.response.send_message('This offer has already been handled.', ephemeral=False)
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
        return await interaction.response.send_message('Choose another human player.', ephemeral=False)
    a = db.execute('SELECT * FROM cards WHERE id=? AND guild=? AND owner=?', (give_id, interaction.guild_id, interaction.user.id)).fetchone()
    b = db.execute('SELECT * FROM cards WHERE id=? AND guild=? AND owner=?', (want_id, interaction.guild_id, member.id)).fetchone()
    if not a or not b:
        return await interaction.response.send_message('Check the card IDs and their owners using /collection.', ephemeral=False)
    view = TradeView(interaction.guild_id, interaction.user.id, member.id, give_id, want_id)
    await interaction.response.send_message(f"{member.display_name}: {interaction.user.display_name} offers #{give_id} ({discord.utils.escape_markdown(item_info(a['country'])['name'])}, {a['rarity']}) for your #{want_id} ({discord.utils.escape_markdown(item_info(b['country'])['name'])}, {b['rarity']}). Accept within 2 minutes. Otherwise the offer expires.", view=view)


@bot.tree.command(name='balance', description='Check your chudlets balance.')
@app_commands.guild_only()
async def wallet(interaction: discord.Interaction):
    await interaction.response.send_message(f'You have {eco.balance(db,interaction.guild_id,interaction.user.id):,} chudlets.', ephemeral=False)


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
        return await interaction.response.send_message(f'There are {pages} pages.',ephemeral=False)
    offset=(page-1)*3
    top=[]
    for i in yes[offset:offset+3]:
        counts=owned[i]
        variants=', '.join(f'{r}: {counts.get(r,0)}' for r in eco.RARITIES)
        top.append(f"**{discord.utils.escape_markdown(item_info(i)['name'])}** ({sum(counts.values())})\n{variants}")
    bottom=[discord.utils.escape_markdown(item_info(i)['name']) for i in no[offset:offset+3]]
    embed=discord.Embed(title='Card list')
    embed.add_field(name=f'Owned ({len(yes)})',value=top[0][:1024] if top else 'None on this page.',inline=False)
    for text in top[1:]:
        embed.add_field(name='Owned, continued',value=text[:1024],inline=False)
    embed.add_field(name=f'Not owned ({len(no)})',value='\n'.join(bottom) or 'None on this page.',inline=False)
    embed.set_footer(text=f'Page {page}/{pages} · Use /list page:2 for the next page. Counts reflect current ownership.')
    await interaction.response.send_message(embed=embed,ephemeral=False)


class SellView(discord.ui.View):
    def __init__(self,guild,owner,card_id,price):
        super().__init__(timeout=60)
        self.guild,self.owner,self.card_id,self.price=guild,owner,card_id,price

    @discord.ui.button(label='Confirm sale',style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('This sale belongs to another player.',ephemeral=False)
        with db:
            sold=eco.sell(db,self.guild,self.owner,self.card_id,self.price)
        self.stop()
        await interaction.response.edit_message(content=f'Sold card #{self.card_id} for {self.price+eco.sale_bonus(db,self.guild,self.owner):,} chudlets (including Mass Credit bonus).' if sold else 'Sale cancelled: you no longer own that card.',view=None)

    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('This sale belongs to another player.',ephemeral=False)
        self.stop()
        await interaction.response.edit_message(content='Sale cancelled.',view=None)


@bot.tree.command(name='sell',description='Sell one owned card for its configured chudlet value.')
@app_commands.guild_only()
async def sell_card(interaction:discord.Interaction,card_id:int):
    row=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? AND id=?',(interaction.guild_id,interaction.user.id,card_id)).fetchone()
    if not row:
        return await interaction.response.send_message('You do not own that card in this server.',ephemeral=False)
    item=item_info(row['country'])
    price=sale_price(item,row['rarity'])
    bonus=eco.sale_bonus(db,interaction.guild_id,interaction.user.id)
    await interaction.response.send_message(f"Sell #{card_id}, {discord.utils.escape_markdown(item['name'])} ({row['rarity']}), for {price+bonus:,} chudlets (including {bonus:,} Mass Credit bonus)? This removes that copy from your collection. Confirm within 60 seconds.",view=SellView(interaction.guild_id,interaction.user.id,card_id,price),ephemeral=False)


class ShopSelect(discord.ui.Select):
    def __init__(self,owner):
        self.owner=owner
        super().__init__(placeholder='Choose a reward to buy', options=[discord.SelectOption(label=name,value=key,description=f'{price:,} chudlets') for key,(name,price) in eco.SHOP.items()])

    async def callback(self,interaction):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('Open your own /shop.',ephemeral=False)
        product=self.values[0]
        with db:
            purchase=eco.buy(db,interaction.guild_id,self.owner,product)
        if purchase is None:
            return await interaction.response.send_message('You do not have enough chudlets.',ephemeral=False)
        await interaction.response.send_message(f'Bought {eco.SHOP[product][0]}. Item #{purchase}. Redeem with /use-item item_id:{purchase}. Balance: {eco.balance(db,interaction.guild_id,self.owner):,} chudlets.',ephemeral=False)


@bot.tree.command(name='shop',description='Buy art rewards with chudlets.')
@app_commands.guild_only()
async def shop(interaction:discord.Interaction):
    menu=discord.ui.View(timeout=180)
    menu.add_item(ShopSelect(interaction.user.id))
    text='\n'.join(f'{name}: {price:,} chudlets' for name,price in eco.SHOP.values())
    await interaction.response.send_message(f'Balance: {eco.balance(db,interaction.guild_id,interaction.user.id):,} chudlets\n\n{text}\n\nSelecting a reward purchases one copy. Use /inventory to find purchased items.',view=menu,ephemeral=False)


@bot.tree.command(name='inventory',description='View purchased rewards and their redemption IDs.')
@app_commands.guild_only()
async def inventory(interaction:discord.Interaction,page:app_commands.Range[int,1,100000]=1):
    rows=db.execute('SELECT * FROM purchases WHERE guild=? AND owner=? ORDER BY id DESC LIMIT 15 OFFSET ?',(interaction.guild_id,interaction.user.id,(page-1)*15)).fetchall()
    text='\n'.join(f"#{r['id']} · {eco.SHOP[r['product']][0]} · {'Redeemed' if r['used'] else 'Unused'}" for r in rows)
    await interaction.response.send_message(text or 'No purchased items on this page.',ephemeral=False)


@bot.tree.command(name='use-item',description='Redeem a purchased art reward and receive a claim receipt.')
@app_commands.guild_only()
async def use_item(interaction:discord.Interaction,item_id:int):
    with db:
        product=eco.redeem(db,interaction.guild_id,interaction.user.id,item_id)
    if product is None:
        return await interaction.response.send_message('That item is already redeemed or is not yours. Use /receipt to display an existing redemption.',ephemeral=False)
    await interaction.response.send_message(redemption_text(interaction.user.id,item_id,product))


def redemption_text(owner,item_id,product):
    extra = ' Pick a character from https://toyhou.se/jsims/characters/folder:5635645 . Some requests may be declined; choose another character or request a refund from the owner.' if product == 'free_character' else ''
    return f'<@{owner}> redeemed **{eco.SHOP[product][0]}**. Receipt #{item_id}. Please ping @jonathan.sims with your claim and this receipt. This reward has been marked used.' + extra


@bot.tree.command(name='receipt',description='Display one of your saved redemption receipts again.')
@app_commands.guild_only()
async def receipt(interaction:discord.Interaction,item_id:int):
    row=db.execute('SELECT * FROM purchases WHERE id=? AND guild=? AND owner=? AND used=1',(item_id,interaction.guild_id,interaction.user.id)).fetchone()
    if not row:
        return await interaction.response.send_message('No redeemed item with that ID belongs to you.',ephemeral=False)
    await interaction.response.send_message(redemption_text(interaction.user.id,item_id,row['product']))


def export_database(path, guild_id=None):
    """Consistent SQLite snapshot, optionally limited to a single server."""
    source = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / 'flags.db'
            dest = sqlite3.connect(snapshot)
            dest.row_factory = sqlite3.Row
            try:
                source.backup(dest)
                tables = [r[0] for r in dest.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                with dest:
                    if guild_id is not None:
                        for table in tables:
                            quoted = '"' + table.replace('"','""') + '"'
                            if 'guild' in {r['name'] for r in dest.execute(f'PRAGMA table_info({quoted})')}:
                                dest.execute(f'DELETE FROM {quoted} WHERE guild IS NULL OR guild != ?', (guild_id,))
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed')
                exported = {}
                for table in tables:
                    quoted = '"' + table.replace('"','""') + '"'
                    exported[table] = [{k: str(v) if isinstance(v,int) and abs(v)>2**53-1 else v for k,v in dict(row).items()} for row in dest.execute(f'SELECT * FROM {quoted}')]
                # Package all committed pages in the main file, without a separate WAL.
                dest.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                dest.execute('PRAGMA journal_mode=DELETE')
            finally:
                dest.close()
            output = io.BytesIO()
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot,'flags.db')
                archive.writestr('user-data.json',json.dumps({'scope':'all' if guild_id is None else str(guild_id),'tables':exported},ensure_ascii=False,indent=2))
                archive.writestr('RESTORE.txt','flags.db is a consistent SQLite snapshot. Stop the bot before restoring it as DATA_DIR/flags.db. Keep a copy of the current database first. Do not restore over a running bot. A server-only export contains only that server; replacing a multi-server database with it removes other servers. user-data.json is for inspection. Discord snowflake IDs are strings to preserve precision. Pending offers exist only in memory and are not included. Tokens and .env are not included. Images are URLs, not downloaded image files. Never upload this archive to a public repository.\n')
            return output.getvalue()
    finally:
        source.close()


@bot.tree.command(name='backup',description='Download saved user data publicly. Admins: this server. Bot owner: all servers.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(scope=[app_commands.Choice(name='This server',value='server'),app_commands.Choice(name='All servers (bot owner only)',value='all')])
@app_commands.checks.cooldown(1,60.0,key=lambda i:i.user.id)
async def backup_data(interaction:discord.Interaction,scope:str='server'):
    if scope not in ('server','all'):
        return await interaction.response.send_message('Choose server or all.',ephemeral=False)
    if scope == 'all' and not await bot.is_owner(interaction.user):
        return await interaction.response.send_message('Only the Discord application owner/team can export every server. Choose This server instead.',ephemeral=False)
    await interaction.response.defer(ephemeral=False,thinking=True)
    path = db.execute('PRAGMA database_list').fetchone()['file']
    payload = await asyncio.to_thread(export_database,path,None if scope=='all' else interaction.guild_id)
    if len(payload) > interaction.filesize_limit:
        return await interaction.followup.send('This backup exceeds Discord’s attachment limit. Download a SQLite snapshot through your host instead.',ephemeral=False)
    filename=f"user-data-{'all' if scope=='all' else interaction.guild_id}-{int(time.time())}.zip"
    await interaction.followup.send('This ZIP is publicly visible in this channel. Download and store your copy securely. It contains flags.db for restoration and user-data.json for inspection. It is a snapshot from this moment.',file=discord.File(io.BytesIO(payload),filename=filename),ephemeral=False)


@bot.tree.command(name='cgive',description='Administrator: add chudlets to a player in this server.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cgive(interaction:discord.Interaction,member:discord.Member,amount:app_commands.Range[int,1,1000000000]):
    if member.bot:
        return await interaction.response.send_message('Choose a human player.',ephemeral=False)
    with db:
        result=eco.adjust_balance(db,interaction.guild_id,interaction.user.id,member.id,amount,interaction.id)
    await interaction.response.send_message(f'Added {result[0]:,} chudlets to {discord.utils.escape_markdown(member.display_name)}. Balance: {result[1]:,}.',ephemeral=False)


@bot.tree.command(name='ctake',description='Administrator: take chudlets from a player, stopping at zero.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def ctake(interaction:discord.Interaction,member:discord.Member,amount:app_commands.Range[int,1,1000000000]):
    if member.bot:
        return await interaction.response.send_message('Choose a human player.',ephemeral=False)
    with db:
        result=eco.adjust_balance(db,interaction.guild_id,interaction.user.id,member.id,-amount,interaction.id)
    await interaction.response.send_message(f'Removed {-result[0]:,} chudlets from {discord.utils.escape_markdown(member.display_name)}. Balance: {result[1]:,}.',ephemeral=False)


@bot.tree.command(name='spawnrigged',description='Administrator: spawn a specific card and rarity in the configured channel.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(rarity=[app_commands.Choice(name=r.title(),value=r) for r in eco.RARITIES])
async def spawnrigged(interaction:discord.Interaction,card:str,rarity:str='normal'):
    if card not in CATALOG or rarity not in eco.RARITIES:
        return await interaction.response.send_message('Choose a card from autocomplete and a valid rarity.',ephemeral=False)
    row=db.execute('SELECT channel FROM settings WHERE guild=?',(interaction.guild_id,)).fetchone()
    if not row:
        return await interaction.response.send_message('Run /setup first.',ephemeral=False)
    await interaction.response.defer(ephemeral=False)
    channel=bot.get_channel(row['channel']) or await bot.fetch_channel(row['channel'])
    success=await spawn(interaction.guild_id,channel,CATALOG[card],rarity)
    await interaction.followup.send('Chosen card spawned. Anyone can claim it normally.' if success else 'An image is already waiting. Claim it or wait for it to expire first.',ephemeral=False)


@spawnrigged.autocomplete('card')
async def autocomplete_card(interaction:discord.Interaction,current:str):
    matching=[item for item in CATALOG.values() if current.casefold() in item['name'].casefold() or current.casefold() in item['id'].casefold()]
    return [app_commands.Choice(name=f"{i['name']} ({i['id']})"[:100],value=i['id']) for i in matching[:25]]


@bot.tree.command(name='ctrade',description='Give another player some of your chudlets.')
@app_commands.guild_only()
async def ctrade(interaction:discord.Interaction,number:app_commands.Range[int,1,1000000000],user:discord.Member):
    if user.bot or user.id==interaction.user.id:
        return await interaction.response.send_message('Choose another human player.',ephemeral=False)
    with db:
        success=eco.transfer(db,interaction.guild_id,interaction.user.id,user.id,number,interaction.id)
    if not success:
        return await interaction.response.send_message('Transfer failed. Check your balance and the recipient’s balance limit.',ephemeral=False)
    await interaction.response.send_message(f'Sent {number:,} chudlets to {discord.utils.escape_markdown(user.display_name)}. Your balance: {eco.balance(db,interaction.guild_id,interaction.user.id):,}.',ephemeral=False)


@bot.tree.command(name='sets',description='See card sets, completion progress and claim earned set bonuses.')
@app_commands.guild_only()
async def sets_command(interaction:discord.Interaction,page:app_commands.Range[int,1,100000]=1):
    with db:
        earned=eco.card_set_bonus(db,interaction.guild_id,interaction.user.id)
    owned={r['country'] for r in db.execute('SELECT DISTINCT country FROM cards WHERE guild=? AND owner=?',(interaction.guild_id,interaction.user.id))}
    paid={r['set_id'] for r in db.execute('SELECT set_id FROM completed_card_sets WHERE guild=? AND owner=?',(interaction.guild_id,interaction.user.id))}
    definitions=list(eco.SET_DEFINITIONS.items())
    embed=discord.Embed(title='Card sets',description=f'Completion reward: 10,000 chudlets per set. Earned now: {earned:,}.')
    for key,definition in definitions[(page-1)*5:page*5]:
        members=definition['members']
        missing=sorted(members-owned,key=lambda item:item_info(item)['name'].casefold())
        names=', '.join(discord.utils.escape_markdown(item_info(i)['name']) for i in missing)
        status='Reward already collected.' if key in paid else 'Reward available on completion.'
        embed.add_field(name=definition['name'],value=f"{len(members & owned)}/{len(members)} owned. {status}\nMissing: {names[:800] or 'None'}" if members else 'No cards assigned yet; no reward.',inline=False)
    if not definitions:
        embed.add_field(name='No sets configured',value='Add sets.json and assign cards using the sets field in items.json.',inline=False)
    embed.set_footer(text=f'Page {page}/{max(1,(len(definitions)+4)//5)}. Any rarity counts. Each reward pays once per player and server.')
    await interaction.response.send_message(embed=embed,ephemeral=False)


@bot.tree.command(name='draw',description='Get a random OC drawing prompt. This command does not award chudlets.')
@app_commands.guild_only()
@app_commands.checks.cooldown(1,10.0,key=lambda i:i.user.id)
async def draw(interaction:discord.Interaction):
    if not DRAW_POOL:
        return await interaction.response.send_message('The drawing pool is empty. Ask the bot owner to add images to draw_pool.json.',ephemeral=False)
    choice=random.choice(DRAW_POOL)
    embed=discord.Embed(description='Draw this OC (Coloured, any) for 7500 Chudlets!')
    embed.set_image(url=choice['image_url'])
    embed.set_footer(text='Submit your drawing to the server’s art reviewer. Chudlets are awarded manually after approval.')
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='mass',description='Get an OC to doodle for Mass drawing rewards.')
@app_commands.guild_only()
@app_commands.checks.cooldown(1,10.0,key=lambda i:i.user.id)
async def mass(interaction:discord.Interaction):
    if not DRAW_POOL:
        return await interaction.response.send_message('The drawing pool is empty. Add images to draw_pool.json.')
    image=random.choice(DRAW_POOL)['image_url']
    embed=discord.Embed(description='Doodle this OC at any quality. Per doodle you make, you earn 10 chudlets. Minimum of 10 doodles. When completed, send the ZIP file to me. You can use this command again to get another oc.\n-# do /draw to earn more chudlets!')
    embed.set_image(url=image)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='massown',description='Get Mass drawing instructions for your own OC.')
@app_commands.guild_only()
async def massown(interaction:discord.Interaction):
    await interaction.response.send_message('Doodle one of your OCs at any quality. Per doodle you make, you earn 1 chudlet. Minimum of 10 doodles. When completed, send a screenshot of all the doodles to me.')

@bot.tree.command(name='mcgive',description='Administrator: give Mass Credit to a user.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def mcgive(interaction:discord.Interaction,member:discord.Member,amount:app_commands.Range[int,1,1000000000]):
    with db:
        delta,current=eco.adjust_mass_credit(db,interaction.guild_id,member.id,amount)
    await interaction.response.send_message(f'Gave {delta:,} Mass Credit to {member.mention}. Total: {current:,}. Sale bonus: +{current//10:,} chudlets per card.')

@bot.tree.command(name='mctake',description='Administrator: take Mass Credit from a user.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def mctake(interaction:discord.Interaction,member:discord.Member,amount:app_commands.Range[int,1,1000000000]):
    with db:
        delta,current=eco.adjust_mass_credit(db,interaction.guild_id,member.id,-amount)
    await interaction.response.send_message(f'Took {-delta:,} Mass Credit from {member.mention}. Total: {current:,}. Sale bonus: +{current//10:,} chudlets per card.')

@bot.tree.command(name='profile',description='View Chudlets, Mass Credit and rarest owned card.')
@app_commands.guild_only()
async def profile(interaction:discord.Interaction,user:discord.Member=None):
    member=user or interaction.user
    mc=eco.mass_credit(db,interaction.guild_id,member.id)
    money=eco.balance(db,interaction.guild_id,member.id)
    rows=db.execute('SELECT id,country,rarity FROM cards WHERE guild=? AND owner=?',(interaction.guild_id,member.id)).fetchall()
    order=list(eco.RARITIES)
    # RARITIES is ordered from most common to least common.
    rarest=max(rows,key=lambda row:order.index(row['rarity']) if row['rarity'] in order else -1) if rows else None
    description=(f"{item_info(rarest['country'])['name']} ({rarest['rarity'].title()}), Card #{rarest['id']}" if rarest else 'None')
    embed=discord.Embed(title=f'{member.display_name} | Profile')
    embed.add_field(name='Mass Credit',value=f'{mc:,}',inline=True)
    embed.add_field(name='Chudlets',value=f'{money:,}',inline=True)
    embed.add_field(name='Rarest card',value=discord.utils.escape_markdown(description),inline=False)
    embed.set_footer(text=f'Mass Credit sale bonus: +{mc//10:,} chudlets per card sold.')
    await interaction.response.send_message(embed=embed)


def gift_owned_card(guild,sender,recipient,card_id):
    if sender==recipient:
        return None
    row=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? AND id=?',(guild,sender,card_id)).fetchone()
    if not row:
        return None
    db.execute('UPDATE cards SET owner=? WHERE guild=? AND owner=? AND id=?',(recipient,guild,sender,card_id))
    db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)',(guild,recipient,row['country']))
    reward=eco.set_bonus(db,guild,recipient,row['country'])+eco.card_set_bonus(db,guild,recipient)
    return row['country'],row['rarity'],reward


@bot.tree.command(name='givecard',description='Give one of your cards to another player for free.')
@app_commands.guild_only()
async def givecard(interaction:discord.Interaction,card_id:int,user:discord.Member):
    if user.bot or user.id==interaction.user.id:
        return await interaction.response.send_message('Choose another human player.',ephemeral=False)
    with db:
        result=gift_owned_card(interaction.guild_id,interaction.user.id,user.id,card_id)
    if result is None:
        return await interaction.response.send_message('You do not own that card in this server. Check /collection.',ephemeral=False)
    item,rarity,reward=result
    text=f"Gave card #{card_id}, {discord.utils.escape_markdown(item_info(item)['name'])} ({rarity}), to {discord.utils.escape_markdown(user.display_name)}."
    if reward:
        text+=f' The recipient earned {reward:,} chudlets for collection completion.'
    await interaction.response.send_message(text,ephemeral=False)


class SellAllView(discord.ui.View):
    def __init__(self,guild,owner,quoted):
        super().__init__(timeout=60)
        self.guild,self.owner,self.quoted=guild,owner,quoted
        self.finished=False

    @discord.ui.button(label='Confirm: sell all cards',style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('This sale belongs to another player.',ephemeral=False)
        if self.finished:
            return await interaction.response.send_message('This sale has already been handled.',ephemeral=False)
        self.finished=True
        with db:
            current=db.execute('SELECT id,country,rarity FROM cards WHERE guild=? AND owner=? ORDER BY id',(self.guild,self.owner)).fetchall()
            current_quote=[(r['id'],sale_price(item_info(r['country']),r['rarity'])) for r in current]
            valid=current_quote==self.quoted
            if valid:
                total=sum(price for _,price in self.quoted)+eco.sale_bonus(db,self.guild,self.owner,len(self.quoted))
                db.execute('DELETE FROM cards WHERE guild=? AND owner=?',(self.guild,self.owner))
                eco.credit(db,self.guild,self.owner,total)
        self.stop()
        await interaction.response.edit_message(content=f'Sold {len(self.quoted)} cards for {total:,} chudlets.' if valid else 'Sale cancelled: your collection or its prices changed. Run /sellall again for a new quote.',view=None)

    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        if interaction.user.id != self.owner:
            return await interaction.response.send_message('This sale belongs to another player.',ephemeral=False)
        if self.finished:
            return await interaction.response.send_message('This sale has already been handled.',ephemeral=False)
        self.finished=True
        self.stop()
        await interaction.response.edit_message(content='Sale cancelled. No cards sold.',view=None)


@bot.tree.command(name='sellall',description='Sell every card you own in this server, after confirmation.')
@app_commands.guild_only()
async def sellall(interaction:discord.Interaction):
    rows=db.execute('SELECT id,country,rarity FROM cards WHERE guild=? AND owner=? ORDER BY id',(interaction.guild_id,interaction.user.id)).fetchall()
    if not rows:
        return await interaction.response.send_message('You have no cards to sell in this server.',ephemeral=False)
    quoted=[(r['id'],sale_price(item_info(r['country']),r['rarity'])) for r in rows]
    total=sum(price for _,price in quoted)+eco.sale_bonus(db,interaction.guild_id,interaction.user.id,len(quoted))
    await interaction.response.send_message(f'Sell ALL {len(rows)} of your cards, including every rarity and your last copies, for {total:,} chudlets? This removes your entire card collection in this server. Shop items are unaffected. Confirm within 60 seconds.',view=SellAllView(interaction.guild_id,interaction.user.id,quoted),ephemeral=False)


def owned_item_counts(guild_id,owner):
    return {r['country']:r['n'] for r in db.execute('SELECT country,COUNT(*) AS n FROM cards WHERE guild=? AND owner=? GROUP BY country',(guild_id,owner))}


def ownership_fields(embed,owned,missing,page):
    pages=max(1,(max(len(owned),len(missing))+4)//5)
    if page>pages:
        return None
    offset=(page-1)*5
    embed.add_field(name=f'Owned ({len(owned)})',value='\n'.join(owned[offset:offset+5]) or 'None on this page.',inline=False)
    embed.add_field(name=f'Missing ({len(missing)})',value='\n'.join(missing[offset:offset+5]) or 'None on this page.',inline=False)
    embed.set_footer(text=f'Page {page}/{pages} · Counts reflect current ownership in this server.')
    return embed


@bot.tree.command(name='settrack',description='Track which cards you own and are missing from a set.')
@app_commands.guild_only()
async def settrack(interaction:discord.Interaction,set:str,page:app_commands.Range[int,1,100000]=1):
    definition=eco.SET_DEFINITIONS.get(set)
    if definition is None:
        return await interaction.response.send_message('Choose a set from autocomplete.',ephemeral=False)
    counts=owned_item_counts(interaction.guild_id,interaction.user.id)
    members=sorted(definition['members'],key=lambda i:item_info(i)['name'].casefold())
    owned=[f"{discord.utils.escape_markdown(item_info(i)['name'])} · {counts[i]} copies" for i in members if i in counts]
    missing=[discord.utils.escape_markdown(item_info(i)['name']) for i in members if i not in counts]
    paid=db.execute('SELECT 1 FROM completed_card_sets WHERE guild=? AND owner=? AND set_id=?',(interaction.guild_id,interaction.user.id,set)).fetchone()
    total_copies=sum(counts.get(i,0) for i in members)
    embed=discord.Embed(title=definition['name'],description=f"{discord.utils.escape_markdown(interaction.user.display_name)} owns {len(owned)}/{len(members)} different cards in this set ({total_copies} copies total).\nAny rarity counts. Completion reward: 10,000 chudlets.\n"+('Reward already collected.' if paid else 'Use /sets to collect the reward when complete.'))
    embed=ownership_fields(embed,owned,missing,page)
    if embed is None:
        return await interaction.response.send_message('That page does not exist.',ephemeral=False)
    await interaction.response.send_message(embed=embed,ephemeral=False)


@settrack.autocomplete('set')
async def autocomplete_set(interaction:discord.Interaction,current:str):
    return [app_commands.Choice(name=f"{v['name']} ({k})"[:100],value=k) for k,v in eco.SET_DEFINITIONS.items() if current.casefold() in k.casefold() or current.casefold() in v['name'].casefold()][:25]


@bot.tree.command(name='raritytrack',description='Track owned and missing variants of one card.')
@app_commands.guild_only()
async def raritytrack(interaction:discord.Interaction,card:str):
    if card not in CATALOG:
        return await interaction.response.send_message('Choose a card from autocomplete.',ephemeral=False)
    counts={r['rarity']:r['n'] for r in db.execute('SELECT rarity,COUNT(*) AS n FROM cards WHERE guild=? AND owner=? AND country=? GROUP BY rarity',(interaction.guild_id,interaction.user.id,card))}
    active=list(eco.RARITIES)
    owned=[f'{rarity.replace("_"," ").title()} · {counts[rarity]} copies' for rarity in active if counts.get(rarity,0)]
    missing=[rarity.replace('_',' ').title() for rarity in active if not counts.get(rarity,0)]
    retired=[f'{r.replace("_"," ").title()} · {n} copies' for r,n in counts.items() if r not in active]
    paid=db.execute('SELECT 1 FROM completed_sets WHERE guild=? AND owner=? AND item=?',(interaction.guild_id,interaction.user.id,card)).fetchone()
    embed=discord.Embed(title=f"{item_info(card)['name']} variants",description=f"{discord.utils.escape_markdown(interaction.user.display_name)} owns {len(owned)}/{len(active)} current variants ({sum(counts.values())} copies total).\n"+('Rarity completion reward already collected.' if paid else 'Rarity completion reward: 5,000 chudlets.'))
    embed.add_field(name='Owned variants',value='\n'.join(owned) or 'None',inline=False)
    embed.add_field(name='Missing variants',value='\n'.join(missing) or 'None',inline=False)
    if retired:
        embed.add_field(name='Retired variants (excluded from progress)',value='\n'.join(retired)[:1024],inline=False)
    await interaction.response.send_message(embed=embed,ephemeral=False)


@raritytrack.autocomplete('card')
async def autocomplete_rarity_card(interaction:discord.Interaction,current:str):
    return await autocomplete_card(interaction,current)


@bot.tree.command(name='itemtrack',description='Track all collectible cards you own and are missing.')
@app_commands.guild_only()
async def itemtrack(interaction:discord.Interaction,page:app_commands.Range[int,1,100000]=1):
    counts=owned_item_counts(interaction.guild_id,interaction.user.id)
    ids=sorted(CATALOG.keys() | counts.keys(),key=lambda i:item_info(i)['name'].casefold())
    owned=[f"{discord.utils.escape_markdown(item_info(i)['name'])} · {counts[i]} copies" for i in ids if i in counts]
    missing=[discord.utils.escape_markdown(item_info(i)['name']) for i in ids if i not in counts]
    embed=discord.Embed(title='Item tracker',description=f"{discord.utils.escape_markdown(interaction.user.display_name)} owns {len(owned)}/{len(ids)} different collectible cards ({sum(counts.values())} copies total). Any rarity counts.")
    embed=ownership_fields(embed,owned,missing,page)
    if embed is None:
        return await interaction.response.send_message('That page does not exist.',ephemeral=False)
    await interaction.response.send_message(embed=embed,ephemeral=False)


def bounty_spawn_pool(guild_id,chosen_item=None,chosen_rarity=None):
    """Boost exact card/rarity pairs once, regardless of subscriber count."""
    targets={(r['card'],r['rarity']) for r in db.execute('SELECT card,rarity FROM bounties WHERE guild=?',(guild_id,))}
    cards=[chosen_item] if chosen_item is not None else ACTIVE_ITEMS
    rarities=[chosen_rarity] if chosen_rarity is not None else list(eco.RARITIES)
    pool=[]
    for item in cards:
        for rarity in rarities:
            weight=(1 if chosen_item is not None else item['weight'])*(1 if chosen_rarity is not None else eco.RARITIES[rarity])
            if (item['id'],rarity) in targets:
                weight*=1.5
            if weight>0:
                pool.append((item,rarity,weight))
    return pool


async def expire_spawns():
    for row in db.execute('SELECT * FROM spawns WHERE expires<=?',(time.time(),)).fetchall():
        with db:
            removed=db.execute('DELETE FROM spawns WHERE guild=? AND message=? AND expires<=?',(row['guild'],row['message'],time.time())).rowcount
        if not removed:
            continue
        try:
            channel=bot.get_channel(row['channel']) or await bot.fetch_channel(row['channel'])
            message=await channel.fetch_message(row['message'])
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logging.exception('Spawn expired in database but its Discord message could not be removed')


@bot.tree.command(name='bountyset',description='Boost one card and rarity, get pinged when it spawns, and extend its lifetime.')
@app_commands.guild_only()
@app_commands.choices(rarity=[app_commands.Choice(name=r.replace('_',' ').title(),value=r) for r in eco.RARITIES])
async def bountyset(interaction:discord.Interaction,card:str,rarity:str):
    available={item['id'] for item in ACTIVE_ITEMS}
    if card not in available or rarity not in eco.RARITIES or eco.RARITIES[rarity]<=0:
        return await interaction.response.send_message('Choose a card and rarity that can spawn randomly.',ephemeral=False)
    with db:
        db.execute('INSERT INTO bounties VALUES(?,?,?,?) ON CONFLICT(guild,owner) DO UPDATE SET card=excluded.card,rarity=excluded.rarity',
                   (interaction.guild_id,interaction.user.id,card,rarity))
    await interaction.response.send_message(f"Bounty set: **{discord.utils.escape_markdown(item_info(card)['name'])} ({rarity.replace('_',' ').title()})**. This exact combination gets 50% more spawn weight in this server. I’ll ping you when it appears, and it will last 10 minutes. This replaces your previous bounty and clears when you claim the target. Other players can still claim it; the consecutive-claim rule still applies.",ephemeral=False)


@bountyset.autocomplete('card')
async def autocomplete_bounty_card(interaction:discord.Interaction,current:str):
    return [app_commands.Choice(name=f"{i['name']} ({i['id']})"[:100],value=i['id']) for i in ACTIVE_ITEMS if current.casefold() in i['name'].casefold() or current.casefold() in i['id'].casefold()][:25]


class CardShopCardSelect(discord.ui.Select):
    def __init__(self,shop):
        self.shop=shop
        super().__init__(placeholder='1. Choose a card',row=0,options=shop.card_options())

    async def callback(self,interaction):
        self.shop.card_id=self.values[0]
        self.shop.refresh_controls()
        await interaction.response.edit_message(embed=self.shop.make_embed(),view=self.shop)


class CardShopRaritySelect(discord.ui.Select):
    def __init__(self,shop):
        self.shop=shop
        super().__init__(placeholder='2. Choose a rarity',row=1,options=[discord.SelectOption(label=r.replace('_',' ').title(),value=r) for r in eco.RARITIES])

    async def callback(self,interaction):
        self.shop.rarity=self.values[0]
        self.shop.refresh_controls()
        await interaction.response.edit_message(embed=self.shop.make_embed(),view=self.shop)


class CardShopView(discord.ui.View):
    def __init__(self,guild,owner):
        super().__init__(timeout=180)
        self.guild,self.owner=guild,owner
        self.cards=list(ITEMS)
        self.page=0
        self.card_id=None
        self.rarity=None
        self.finished=False
        self.add_item(CardShopCardSelect(self))
        self.add_item(CardShopRaritySelect(self))
        self.refresh_controls()

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner or interaction.guild_id!=self.guild:
            await interaction.response.send_message('Open your own /cardshop to buy cards.',ephemeral=False)
            return False
        if self.finished:
            await interaction.response.send_message('This purchase is complete. Open /cardshop again.',ephemeral=False)
            return False
        return True

    def card_options(self):
        return [discord.SelectOption(label=item['name'][:100],value=item['id'],description=f"Card ID: {item['id']}"[:100],default=item['id']==self.card_id) for item in self.cards[self.page*25:(self.page+1)*25]]

    def refresh_controls(self):
        self.previous_page.disabled=self.page==0
        self.next_page.disabled=(self.page+1)*25>=len(self.cards)
        self.purchase.disabled=self.card_id is None or self.rarity is None
        for child in self.children:
            if isinstance(child,CardShopCardSelect):
                child.options=self.card_options()
            if isinstance(child,CardShopRaritySelect):
                for option in child.options:
                    option.default=option.value==self.rarity
        self.purchase.label='Buy selected card'
        if self.card_id is not None and self.rarity is not None:
            self.purchase.label=f'Buy · {sale_price(item_info(self.card_id),self.rarity)*5:,} chudlets'[:80]

    def make_embed(self):
        embed=discord.Embed(title='Card shop',description=f'<@{self.owner}> · Balance: {eco.balance(db,self.guild,self.owner):,} chudlets\nChoose a card and a rarity, then press Buy. Each copy costs 5× its sell value. Purchases count toward completion.')
        if self.card_id is not None:
            item=item_info(self.card_id)
            embed.add_field(name='Card',value=discord.utils.escape_markdown(item['name']),inline=False)
            if self.rarity is not None:
                price=sale_price(item,self.rarity)
                embed.add_field(name='Rarity and price',value=f'{self.rarity.replace("_"," ").title()} · Buy: {price*5:,} chudlets · Sell: {price:,}',inline=False)
                image=image_for(item,self.rarity)
                if image:
                    embed.set_image(url=image)
        embed.set_footer(text=f'Card menu page {self.page+1}/{max(1,(len(self.cards)+24)//25)} · Expires after 3 minutes of inactivity.')
        return embed

    @discord.ui.button(label='Previous cards',style=discord.ButtonStyle.secondary,row=2)
    async def previous_page(self,interaction,button):
        self.page=max(0,self.page-1)
        self.card_id=None
        self.refresh_controls()
        await interaction.response.edit_message(embed=self.make_embed(),view=self)

    @discord.ui.button(label='Next cards',style=discord.ButtonStyle.secondary,row=2)
    async def next_page(self,interaction,button):
        self.page=min(max(0,(len(self.cards)-1)//25),self.page+1)
        self.card_id=None
        self.refresh_controls()
        await interaction.response.edit_message(embed=self.make_embed(),view=self)

    @discord.ui.button(label='Buy selected card',style=discord.ButtonStyle.green,row=3,disabled=True)
    async def purchase(self,interaction,button):
        if self.finished or self.card_id is None or self.rarity not in eco.RARITIES:
            return await interaction.response.send_message('Choose a card and rarity in an active shop first.',ephemeral=False)
        item=item_info(self.card_id)
        price=sale_price(item,self.rarity)*5
        with db:
            result=eco.buy_card(db,self.guild,self.owner,self.card_id,self.rarity,price,interaction.id)
        if result is None:
            return await interaction.response.send_message(f'You need {price:,} chudlets. Your balance is {eco.balance(db,self.guild,self.owner):,}.',ephemeral=False)
        self.finished=True
        self.stop()
        card_id,earned=result
        embed=discord.Embed(title='Card purchased',description=f"{discord.utils.escape_markdown(interaction.user.display_name)} bought **{discord.utils.escape_markdown(item['name'])} ({self.rarity.replace('_',' ').title()})** for {price:,} chudlets.\nCard #{card_id} added to your collection and completion progress.\nCompletion rewards: +{earned:,} chudlets.\nBalance: {eco.balance(db,self.guild,self.owner):,} chudlets.")
        image=image_for(item,self.rarity)
        if image:
            embed.set_image(url=image)
        await interaction.response.edit_message(embed=embed,view=None)


@bot.tree.command(name='cardshop',description='Buy collectible cards in any rarity for 5 times their sell price.')
@app_commands.guild_only()
async def cardshop(interaction:discord.Interaction):
    view=CardShopView(interaction.guild_id,interaction.user.id)
    await interaction.response.send_message(embed=view.make_embed(),view=view,ephemeral=False)


@bot.tree.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = 'Missing required permission: ' + ', '.join(error.missing_permissions)
    elif isinstance(error, app_commands.CommandOnCooldown):
        text = f'Try again in {error.retry_after:.0f} seconds.'
    else:
        logging.error('Command failed', exc_info=(type(error), error, error.__traceback__))
        text = 'Command failed. Check the hosting logs and my channel permissions.'
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=False)
    else:
        await interaction.response.send_message(text, ephemeral=False)


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in your .env file or hosting environment.')
    bot.run(token)
