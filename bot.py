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
# Customize items.json. Keep IDs stable once a collectible has been claimed.
ROOT = Path(__file__).resolve().parent
SPAWN_MIN_SECONDS = 600
SPAWN_MAX_SECONDS = 1200
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
ACTIVE_ITEMS = [item for item in ITEMS if item['weight'] > 0]
if not ACTIVE_ITEMS:
    raise ValueError('At least one item must have a weight above zero.')
# Store definitions so removing an item from the spawn pool keeps old cards viewable.
db.execute('CREATE TABLE IF NOT EXISTS item_defs(id TEXT PRIMARY KEY, name TEXT, image_url TEXT)')
with db:
    db.executemany('INSERT INTO item_defs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,image_url=excluded.image_url',
                   [(i['id'], i['name'], i['image_url']) for i in ITEMS])
CATALOG = {r['id']: dict(r) for r in db.execute('SELECT * FROM item_defs')}


def item_info(item_id):
    return CATALOG.get(item_id, {'id': item_id, 'name': item_id, 'image_url': None})


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
            card_id = db.execute('INSERT INTO cards(guild,owner,country) VALUES(?,?,?)',
                                 (message.guild.id, message.author.id, row['country'])).lastrowid
    item = item_info(row['country'])
    # Ownership is committed before any Discord API calls.
    try:
        await message.channel.send(f"{discord.utils.escape_markdown(message.author.display_name)} claimed **{discord.utils.escape_markdown(item['name'])}**! Card #{card_id}.")
        original = await message.channel.fetch_message(row['message'])
        embed = discord.Embed(title=item['name'], description=f'Claimed! Card #{card_id}.', color=0x79B86D)
        if item['image_url']:
            embed.set_image(url=item['image_url'])
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
    embed = discord.Embed(title=item['name'], description='Type **Claim** in this channel to collect this image.', color=0x79B86D)
    embed.set_image(url=item['image_url'])
    embed.set_footer(text=f'Expires in {CLAIM_SECONDS // 60} minutes.')
    message = await channel.send(embed=embed)
    with db:
        db.execute('INSERT OR REPLACE INTO spawns VALUES(?,?,?,?,?)',
                   (guild_id, channel.id, message.id, item['id'], time.time() + CLAIM_SECONDS))
    return True


@tasks.loop(seconds=30)
async def spawn_loop():
    for row in db.execute('SELECT * FROM settings WHERE next_spawn<=?', (time.time(),)).fetchall():
        try:
            channel = bot.get_channel(row['channel']) or await bot.fetch_channel(row['channel'])
            await spawn(row['guild'], channel)
        except discord.HTTPException:
            logging.exception('Could not spawn in guild %s', row['guild'])
            with db:
                db.execute('UPDATE settings SET next_spawn=? WHERE guild=?', (time.time()+600, row['guild']))


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
    await interaction.response.send_message(f'Spawns enabled in {channel.mention}. First spawn within 30 seconds, then every 10–20 minutes.', ephemeral=True)


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
    lines = [f"#{r['id']} · {discord.utils.escape_markdown(item_info(r['country'])['name'])}" for r in rows]
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
    embed = discord.Embed(title=item['name'], description=f"Card #{row['id']} · Owner: <@{row['owner']}>")
    if item['image_url']:
        embed.set_image(url=item['image_url'])
    else:
        embed.set_footer(text='Legacy card: no image has been configured for this item.')
    await interaction.response.send_message(embed=embed, ephemeral=True)


class TradeView(discord.ui.View):
    def __init__(self, guild_id, sender, target, give, want):
        super().__init__(timeout=120)
        self.guild_id, self.sender, self.target, self.give, self.want = guild_id, sender, target, give, want

    @discord.ui.button(label='Accept trade', style=discord.ButtonStyle.green)
    async def accept(self, interaction, button):
        if interaction.user.id != self.target:
            return await interaction.response.send_message('Only the invited player can accept.', ephemeral=True)
        # Recheck both owners at acceptance, then exchange atomically.
        with db:
            a = db.execute('SELECT owner FROM cards WHERE id=? AND guild=?', (self.give, self.guild_id)).fetchone()
            b = db.execute('SELECT owner FROM cards WHERE id=? AND guild=?', (self.want, self.guild_id)).fetchone()
            valid = a and b and a['owner'] == self.sender and b['owner'] == self.target
            if valid:
                db.execute('UPDATE cards SET owner=? WHERE id=?', (self.target, self.give))
                db.execute('UPDATE cards SET owner=? WHERE id=?', (self.sender, self.want))
        self.stop()
        await interaction.response.edit_message(content='Trade completed.' if valid else 'Trade cancelled: card ownership changed.', view=None)


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
    await interaction.response.send_message(f"{member.display_name}: {interaction.user.display_name} offers #{give_id} ({discord.utils.escape_markdown(item_info(a['country'])['name'])}) for your #{want_id} ({discord.utils.escape_markdown(item_info(b['country'])['name'])}). Accept within 2 minutes. Otherwise the offer expires.", view=view)


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
