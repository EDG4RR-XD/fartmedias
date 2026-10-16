import asyncio
import logging
import os
import random
import sqlite3
import time
import unicodedata
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
# name, flag, relative spawn weight, accepted alternate names
FLAGS = [
    ('Canada', '🇨🇦', 10, []), ('Japan', '🇯🇵', 10, []),
    ('United States', '🇺🇸', 10, ['usa', 'us', 'united states of america']),
    ('United Kingdom', '🇬🇧', 10, ['uk', 'britain']),
    ('France', '🇫🇷', 10, []), ('Germany', '🇩🇪', 10, []),
    ('Brazil', '🇧🇷', 8, []), ('Mexico', '🇲🇽', 8, []),
    ('Australia', '🇦🇺', 8, []), ('India', '🇮🇳', 8, []),
    ('South Africa', '🇿🇦', 6, []), ('South Korea', '🇰🇷', 6, ['republic of korea']),
    ('Norway', '🇳🇴', 5, []), ('Greece', '🇬🇷', 5, []),
    ('Iceland', '🇮🇸', 2, []), ('Bhutan', '🇧🇹', 1, []),
]
CATALOG = {f[0]: f for f in FLAGS}


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if c.isalnum())


class FlagBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())

    async def setup_hook(self):
        self.add_view(CatchView())
        guild_id = os.getenv('TEST_GUILD_ID')
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        spawn_loop.start()


bot = FlagBot()


class GuessModal(discord.ui.Modal, title='Name this country'):
    answer = discord.ui.TextInput(label='Country name', max_length=80)

    def __init__(self, guild_id, message_id):
        super().__init__()
        self.guild_id, self.message_id = guild_id, message_id

    async def on_submit(self, interaction):
        row = db.execute('SELECT * FROM spawns WHERE guild=? AND message=? AND expires>?',
                         (self.guild_id, self.message_id, time.time())).fetchone()
        if not row:
            return await interaction.response.send_message('This flag has expired or was caught.', ephemeral=True)
        flag = CATALOG[row['country']]
        if normalize(str(self.answer)) not in {normalize(n) for n in [flag[0], *flag[3]]}:
            return await interaction.response.send_message('Incorrect. You can try again.', ephemeral=True)
        # No awaits inside the transaction: only one catch can win.
        with db:
            claimed = db.execute('DELETE FROM spawns WHERE guild=? AND message=?',
                                 (self.guild_id, self.message_id)).rowcount
            if claimed:
                card_id = db.execute('INSERT INTO cards(guild,owner,country) VALUES(?,?,?)',
                                     (self.guild_id, interaction.user.id, flag[0])).lastrowid
        if not claimed:
            return await interaction.response.send_message('Someone caught it first.', ephemeral=True)
        await interaction.response.send_message(f'{interaction.user.display_name} caught {flag[1]} {flag[0]}! Card #{card_id}.')
        try:
            message = await interaction.channel.fetch_message(self.message_id)
            await message.edit(content='This flag was caught.', embed=None, view=None)
        except discord.HTTPException:
            pass


class CatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Catch', style=discord.ButtonStyle.green, custom_id='flagbot:catch:v1')
    async def catch(self, interaction, button):
        await interaction.response.send_modal(GuessModal(interaction.guild_id, interaction.message.id))


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
                   (time.time() + random.randint(600, 1200), guild_id))
    flag = random.choices(FLAGS, weights=[f[2] for f in FLAGS])[0]
    embed = discord.Embed(title='A wild flag appeared!', description=f'# {flag[1]}\nClick Catch and enter the country name.', color=0x79B86D)
    embed.set_footer(text='Expires in 5 minutes.')
    message = await channel.send(embed=embed, view=CatchView())
    with db:
        db.execute('INSERT OR REPLACE INTO spawns VALUES(?,?,?,?,?)',
                   (guild_id, channel.id, message.id, flag[0], time.time() + 300))
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


@bot.tree.command(name='setup', description='Choose the flag spawn channel (Manage Server required).')
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
    await interaction.followup.send('Spawned.' if done else 'There is already an active flag.', ephemeral=True)


@bot.tree.command(name='collection', description='View a collection and card IDs for trading.')
@app_commands.guild_only()
async def collection(interaction: discord.Interaction, member: discord.Member = None, page: app_commands.Range[int, 1, 100000] = 1):
    member = member or interaction.user
    total = db.execute('SELECT COUNT(*) FROM cards WHERE guild=? AND owner=?', (interaction.guild_id, member.id)).fetchone()[0]
    rows = db.execute('SELECT * FROM cards WHERE guild=? AND owner=? ORDER BY id LIMIT 20 OFFSET ?',
                      (interaction.guild_id, member.id, (page-1)*20)).fetchall()
    lines = [f"#{r['id']} · {CATALOG[r['country']][1]} {r['country']}" for r in rows]
    embed = discord.Embed(title=f'{member.display_name}’s collection', description='\n'.join(lines) or 'No cards on this page.')
    embed.set_footer(text=f'{total} cards · Page {page}/{max(1, (total+19)//20)} · Collections are per server')
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
    await interaction.response.send_message(f"{member.display_name}: {interaction.user.display_name} offers #{give_id} ({a['country']}) for your #{want_id} ({b['country']}). Accept within 2 minutes. Otherwise the offer expires.", view=view)


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
