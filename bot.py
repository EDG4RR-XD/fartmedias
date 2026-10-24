import io
import re
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
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageOps, ImageChops
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
    db.execute('CREATE TABLE IF NOT EXISTS chain_streaks(guild INTEGER, owner INTEGER NOT NULL, set_id TEXT NOT NULL, streak INTEGER NOT NULL, PRIMARY KEY(guild,owner))')
    # Preserve the existing server streak when upgrading to per-player chains.
    chain_pk = [row['name'] for row in db.execute('PRAGMA table_info(chain_streaks)') if row['pk']]
    if chain_pk == ['guild']:
        db.execute('ALTER TABLE chain_streaks RENAME TO chain_streaks_legacy')
        db.execute('CREATE TABLE chain_streaks(guild INTEGER, owner INTEGER NOT NULL, set_id TEXT NOT NULL, streak INTEGER NOT NULL, PRIMARY KEY(guild,owner))')
        db.execute('INSERT INTO chain_streaks SELECT guild,owner,set_id,streak FROM chain_streaks_legacy')
        db.execute('DROP TABLE chain_streaks_legacy')
    db.execute('CREATE TABLE IF NOT EXISTS bounties(guild INTEGER, owner INTEGER, card TEXT, rarity TEXT, PRIMARY KEY(guild,owner))')
with db:
    if not db.execute("SELECT 1 FROM metadata WHERE key='five_minute_schedule'").fetchone():
        db.execute('UPDATE settings SET next_spawn=MIN(next_spawn,?)', (time.time()+300,))
        db.execute("INSERT INTO metadata VALUES('five_minute_schedule','1')")

# Customize items.json. Keep IDs stable once a collectible has been claimed.
ROOT = Path(__file__).resolve().parent
ADMIN_RARITIES = {'normal', 'holographic', 'glitch', 'inverted'}
with (ROOT / 'modifiers.json').open(encoding='utf-8') as f:
    MODIFIERS = json.load(f)
CLOUDY_CHANCE = 0.02
BOUNCY_CHANCE = 0.02
HYPER_CHANCE = 0.02
BLOOD_MOON_CHANCE = 0.01
BLOOD_MOON_HALLOWEEN_CHANCE = 0.20
CHAIN_CHUDLETS_PER_EXTRA_CARD = 100
CHAIN_RARITY_STEP = 0.15  # 15% more weight per chain level for rarities above normal
CHAIN_RARITY_CAP = 3.0
SEASONAL_WINDOWS = {'spooky': (10, 21, 31), 'hungry': (11, 16, 26), 'festive': (12, 15, 25)}
MODIFIER_TIMEZONE = ZoneInfo(os.getenv('MODIFIER_TIMEZONE', 'America/New_York'))
for key in SEASONAL_WINDOWS:
    cfg = MODIFIERS[key]
    for orientation in ('TALL', 'WIDE'):
        if not isinstance(cfg.get(orientation, ''), str):
            raise ValueError(f'{key}.{orientation} must be an image path or HTTPS URL.')
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
    item['orientation'] = str(item.get('orientation', '')).upper()
    if item['orientation'] not in ('', 'TALL', 'WIDE'):
        raise ValueError('Item orientation must be TALL or WIDE.')
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


def sale_price(item, rarity, row=None):
    base = item.get('rarity_sell_values', {}).get(rarity, item.get('sell_value', 100))
    count = sum(flip_flags(row)) + len(set(card_modifiers(row))) if row is not None else 0
    return base + 1500 * count


def flip_flags(row):
    return bool(row['flip_horizontal']) if 'flip_horizontal' in row.keys() else False, bool(row['flip_vertical']) if 'flip_vertical' in row.keys() else False


def card_modifiers(row):
    return json.loads(row['modifiers']) if 'modifiers' in row.keys() else []


def modifier_names(modifiers):
    return ["ADMIN" if m == 'admin' else MODIFIERS.get(m, {}).get('name', m.upper()) for m in modifiers]


def variant_label(rarity, flip_horizontal=False, flip_vertical=False, modifiers=()):
    parts=[rarity.replace('_',' ').title()] + modifier_names(modifiers)
    if flip_horizontal:
        parts.append('Horizontal Flip')
    if flip_vertical:
        parts.append('Vertical Flip')
    order = list(eco.RARITIES)
    sparkles = flip_horizontal or flip_vertical or (rarity in order and order.index(rarity) >= order.index('holographic'))
    return ' + '.join(parts) + (' ✨' if sparkles else '')


def variant_suffix(flip_horizontal=False, flip_vertical=False):
    labels=[]
    if flip_horizontal:
        labels.append('H-Flip')
    if flip_vertical:
        labels.append('V-Flip')
    return f" [{' + '.join(labels)}]" if labels else ''


def seasonal_image(item, modifier):
    orientation = str(item.get('orientation', '')).upper()
    if orientation not in ('TALL', 'WIDE'):
        return ''
    return MODIFIERS[modifier].get(orientation, '')


def roll_seasonal_modifiers(item, today=None):
    today = today or datetime.now(MODIFIER_TIMEZONE).date()
    return [key for key, (month, start, end) in SEASONAL_WINDOWS.items()
            if today.month == month and start <= today.day <= end
            and seasonal_image(item, key) and random.random() < 0.10]


def roll_cloudy_modifier(rarity):
    return rarity in ADMIN_RARITIES and random.random() < CLOUDY_CHANCE


def roll_flip_modifiers():
    return random.random() < eco.HORIZONTAL_FLIP_CHANCE, random.random() < eco.VERTICAL_FLIP_CHANCE


def transformed_image_bytes(data, flip_horizontal=False, flip_vertical=False, modifiers=(), overlay_data=None):
    def apply_full_overlay(base_image, overlay_path, blend_mode):
        with Image.open(overlay_path) as overlay:
            overlay = overlay.convert('RGB').resize(base_image.size, Image.Resampling.BILINEAR)
        alpha = base_image.getchannel('A')
        base_rgb = base_image.convert('RGB')
        if blend_mode == 'multiply':
            blended = ImageChops.multiply(base_rgb, overlay)
        elif blend_mode == 'screen':
            blended = ImageChops.screen(base_rgb, overlay)
        else:
            raise ValueError(f'Unsupported blend mode: {blend_mode}')
        result = blended.convert('RGBA')
        result.putalpha(alpha)
        return result

    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        if modifiers:
            image = image.convert('RGBA')
        if 'admin' in modifiers:
            image = apply_full_overlay(image, ROOT / 'admin_gradient.png', 'multiply')
        if 'cloudy' in modifiers:
            image = apply_full_overlay(image, ROOT / MODIFIERS['cloudy']['image'], 'screen')
        if 'blood_moon' in modifiers:
            image = apply_full_overlay(image, ROOT / MODIFIERS['blood_moon']['image'], 'multiply')
        if flip_horizontal:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if flip_vertical:
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)  # Reflect across X-axis
        for key in ('spooky', 'hungry', 'festive'):
            if key in modifiers:
                with Image.open(io.BytesIO(overlay_data[key])) as overlay:
                    overlay = overlay.convert('RGBA')
                    # Fit the overlay to the card without resizing the card or its canvas.
                    overlay = overlay.resize(image.size, Image.Resampling.LANCZOS)
                    image = Image.alpha_composite(image, overlay)
        output = io.BytesIO()
        if {'bouncy', 'hyper'} & set(modifiers):
            # Keep the complete rendered card at its native resolution.
            image = image.convert('RGBA')
            spinning = 'hyper' in modifiers
            travel = max(2, round(image.height * 0.08)) if 'bouncy' in modifiers else 0
            side = math.ceil(math.hypot(image.width, image.height)) if spinning else None
            width, height = (side, side) if spinning else image.size
            frames = []
            # One rotation every two seconds; bounce keeps its one-second period.
            steps = 40 if spinning else 20
            for step in range(steps):
                y = round(travel * (1 + math.cos(2 * math.pi * step / 20)) / 2)
                rendered = image.rotate(-360 * step / steps, resample=Image.Resampling.BICUBIC, expand=True) if spinning else image
                canvas = Image.new('RGBA', (width, height + travel))
                canvas.paste(rendered, ((width - rendered.width) // 2, (height - rendered.height) // 2 + y))
                frame = canvas.convert('RGB').quantize(colors=255)
                frame.putpalette((frame.getpalette() + [0] * 768)[:765] + [0, 0, 0])
                frame.paste(255, mask=canvas.getchannel('A').point(lambda a: 255 if a < 128 else 0))
                frames.append(frame)
            frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                           duration=50, loop=0, disposal=2, transparency=255, optimize=False)
        else:
            image.save(output, format='PNG')
        output.seek(0)
        return output


async def download_modifier_image(session, source, label):
    """Fetch and verify an image. Retries cover transient image-host failures."""
    if not source.startswith(('https://', 'http://')):
        payload = await asyncio.to_thread((ROOT / source).read_bytes)
    else:
        last_error = None
        for attempt in range(3):
            try:
                async with session.get(source, headers={'User-Agent': 'Mozilla/5.0 (compatible; CardImageBot/1.0)'}) as response:
                    response.raise_for_status()
                    payload = await response.read()
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                logging.warning('Image download attempt %s/3 failed for %s: %r', attempt + 1, label, error)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise RuntimeError(f'{label} image could not be downloaded: {last_error}') from last_error
    try:
        with Image.open(io.BytesIO(payload)) as check:
            check.verify()
    except Exception as error:
        raise ValueError(f'{label} returned invalid image data') from error
    return payload


async def card_image_payload(item, rarity, flip_horizontal=False, flip_vertical=False, filename='card.png', modifiers=(), error_details=None):
    image = image_for(item, rarity)
    if not image:
        return None, None
    if not (flip_horizontal or flip_vertical or modifiers):
        return image, None
    try:
        timeout = aiohttp.ClientTimeout(total=35, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = await download_modifier_image(session, image, f'{item.get("id", "card")} ({rarity})')
            with Image.open(io.BytesIO(payload)) as original:
                actual_width, actual_height = ImageOps.exif_transpose(original).size
            actual_orientation = 'WIDE' if actual_width > actual_height else 'TALL'
            overlays = {}
            for key in SEASONAL_WINDOWS:
                if key in modifiers:
                    source = MODIFIERS[key].get(actual_orientation, '')
                    if not source:
                        raise ValueError(f'{key} has no {actual_orientation} overlay in modifiers.json')
                    overlays[key] = await download_modifier_image(session, source, f'{key} {actual_orientation}')
        transformed = await asyncio.to_thread(transformed_image_bytes, payload, flip_horizontal, flip_vertical, modifiers, overlays)
        if {'bouncy', 'hyper'} & set(modifiers):
            filename = str(Path(filename).with_suffix('.gif'))
        return f'attachment://{filename}', discord.File(transformed, filename=filename)
    except Exception as error:
        if error_details is not None:
            error_details.append(f'{type(error).__name__}: {str(error)[:220]}')
        logging.exception('Image transform failed for card=%s rarity=%s modifiers=%s horizontal=%s vertical=%s base_url=%s',
                          item.get('id'), rarity, modifiers, flip_horizontal, flip_vertical, image)
        return None, None


async def set_card_embed_image(embed, item, rarity, flip_horizontal=False, flip_vertical=False, *, thumbnail=False, filename='card.png', modifiers=()):
    embed.add_field(name='Artist', value='art by ' + discord.utils.escape_markdown(str(item.get('ARTIST:', 'Unknown'))), inline=False)
    errors = []
    image_ref, attachment = await card_image_payload(item, rarity, flip_horizontal, flip_vertical, filename=filename, modifiers=modifiers, error_details=errors)
    if image_ref:
        if thumbnail:
            embed.set_thumbnail(url=image_ref)
        else:
            embed.set_image(url=image_ref)
    elif flip_horizontal or flip_vertical or modifiers:
        detail = discord.utils.escape_markdown(errors[0]) if errors else 'Unknown image rendering error.'
        embed.add_field(name='Image temporarily unavailable', value=f'Image rendering failed: {detail}\nModifiers remain saved. Check the source image URL in items.json and Railway logs.', inline=False)
    return attachment


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
        prior_chain = db.execute('SELECT * FROM chain_streaks WHERE guild=? AND owner=?', (message.guild.id, message.author.id)).fetchone()
        matching_sets = sorted(key for key, entry in eco.SET_DEFINITIONS.items() if row['country'] in entry['members'])
        continuing = bool(prior_chain and prior_chain['owner'] == message.author.id and prior_chain['set_id'] in matching_sets)
        chain_set = prior_chain['set_id'] if continuing else (matching_sets[0] if matching_sets else None)
        streak = prior_chain['streak'] + 1 if continuing else (1 if chain_set else 0)
        chain_bonus = (streak - 1) * CHAIN_CHUDLETS_PER_EXTRA_CARD if streak else 0
        with db:
            claimed = db.execute('DELETE FROM spawns WHERE guild=? AND message=?',
                                 (message.guild.id, row['message'])).rowcount
            if not claimed:
                return
            card_id, earned = eco.award_claim(db, message.guild.id, message.author.id, row['country'], row['rarity'], row['flip_horizontal'], row['flip_vertical'])
            db.execute('UPDATE cards SET modifiers=? WHERE id=?', (row['modifiers'], card_id))
            if chain_bonus:
                eco.credit(db, message.guild.id, message.author.id, chain_bonus)
                earned += chain_bonus
            if chain_set:
                db.execute('INSERT INTO chain_streaks VALUES(?,?,?,?) ON CONFLICT(guild,owner) DO UPDATE SET set_id=excluded.set_id,streak=excluded.streak', (message.guild.id, message.author.id, chain_set, streak))
            else:
                db.execute('DELETE FROM chain_streaks WHERE guild=? AND owner=?', (message.guild.id, message.author.id))
            db.execute('DELETE FROM bounties WHERE guild=? AND owner=? AND card=? AND rarity=?', (message.guild.id,message.author.id,row['country'],row['rarity']))
            db.execute('INSERT INTO claim_turns VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET owner=excluded.owner', (message.guild.id, message.author.id))
    item = item_info(row['country'])
    if spam_active(message.guild.id):
        with db:
            db.execute('UPDATE settings SET next_spawn=? WHERE guild=?',(time.time(),message.guild.id))
    # Ownership is committed before any Discord API calls.
    try:
        fh, fv = flip_flags(row)
        await message.channel.send(f"{discord.utils.escape_markdown(message.author.display_name)} claimed **{discord.utils.escape_markdown(item['name'])} ({variant_label(row['rarity'], fh, fv, card_modifiers(row))})**! Card #{card_id}. +{earned} chudlets." + (f" Chain x{streak} ({eco.SET_DEFINITIONS[chain_set]['name']}); +{chain_bonus} bonus." if chain_bonus else (f" Chain x1 ({eco.SET_DEFINITIONS[chain_set]['name']})." if chain_set else "")))
        original = await message.channel.fetch_message(row['message'])
        embed = discord.Embed(title=item['name'], description=f"{variant_label(row['rarity'], fh, fv, card_modifiers(row))} · Claimed! Card #{card_id}.", color=0x79B86D)
        attachment = await set_card_embed_image(embed, item, row['rarity'], fh, fv, filename=f'claim-{card_id}.png', modifiers=card_modifiers(row))
        if attachment:
            await original.edit(content=None, embed=embed, attachments=[attachment], view=None)
        else:
            await original.edit(content=None, embed=embed, view=None)
    except discord.HTTPException:
        logging.exception('Claim saved but could not update its Discord message')


spawn_lock = asyncio.Lock()
spam_until = {}  # Persisted in SQLite so spam periods survive restarts.
with db:
    db.execute('CREATE TABLE IF NOT EXISTS spam_periods(guild INTEGER PRIMARY KEY, until_time REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS rigged_spam(guild INTEGER PRIMARY KEY, rarity TEXT, card TEXT, set_id TEXT, horizontal_flip INTEGER, vertical_flip INTEGER)')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(rigged_spam)')}
    for field in ('horizontal_flip', 'vertical_flip'):
        if field not in columns:
            db.execute(f'ALTER TABLE rigged_spam ADD COLUMN {field} INTEGER')

    if 'modifiers' not in columns:
        db.execute("ALTER TABLE rigged_spam ADD COLUMN modifiers TEXT NOT NULL DEFAULT '[]'")

with db:
    for table, identity in (('cards', 'id'), ('spawns', 'guild'), ('auction_listings', 'card_id'), ('rigged_spam', 'guild')):
        for row in db.execute(f'SELECT {identity},modifiers FROM {table}').fetchall():
            saved = json.loads(row['modifiers'])
            cleaned = [key for key in saved if key not in ('tall', 'wide')]
            if cleaned != saved:
                db.execute(f'UPDATE {table} SET modifiers=? WHERE {identity}=?', (json.dumps(cleaned), row[identity]))

def spam_active(guild):
    row=db.execute('SELECT until_time FROM spam_periods WHERE guild=?',(guild,)).fetchone()
    return bool(row and row['until_time'] > time.time())



async def spawn(guild_id, channel, chosen_item=None, chosen_rarity=None, horizontal_flip=None, vertical_flip=None, modifiers=None):
    async with spawn_lock:
        return await _spawn(guild_id, channel, chosen_item, chosen_rarity, horizontal_flip, vertical_flip, modifiers)


async def _spawn(guild_id, channel, chosen_item=None, chosen_rarity=None, horizontal_flip=None, vertical_flip=None, modifiers=None):
    active = db.execute('SELECT 1 FROM spawns WHERE guild=? AND expires>?', (guild_id, time.time())).fetchone()
    if active:
        return False
    # Reserve the next time before awaiting Discord to prevent repeated attempts.
    with db:
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?',
                   (time.time() + random.randint(SPAWN_MIN_SECONDS, SPAWN_MAX_SECONDS), guild_id))
    # Apply session filters only to automatic spawns. Explicit /spawnrigged wins.
    rigged_session = chosen_item is None and chosen_rarity is None and spam_active(guild_id)
    rig = None
    if rigged_session:
        rig=db.execute('SELECT * FROM rigged_spam WHERE guild=?',(guild_id,)).fetchone()
        if rig:
            if modifiers is None:
                modifiers = card_modifiers(rig)
            if horizontal_flip is None and rig['horizontal_flip'] is not None:
                horizontal_flip = bool(rig['horizontal_flip'])
            if vertical_flip is None and rig['vertical_flip'] is not None:
                vertical_flip = bool(rig['vertical_flip'])
            chosen_rarity=rig['rarity']
            if rig['card']:
                chosen_item=next((item for item in ITEMS if item['id']==rig['card']),None)
    modifiers = list(modifiers or [])
    if {'admin', 'cloudy'} & set(modifiers) and chosen_rarity and chosen_rarity not in ADMIN_RARITIES:
        raise ValueError('ADMIN requires normal, holographic, glitch or inverted.')
    pool=bounty_spawn_pool(guild_id,chosen_item,chosen_rarity)
    if rigged_session and rig and rig['set_id']:
        if rig['set_id']:
            allowed=eco.SET_DEFINITIONS[rig['set_id']]['members']
            pool=[pair for pair in pool if pair[0]['id'] in allowed]
    if {'admin', 'cloudy'} & set(modifiers):
        pool = [pair for pair in pool if pair[1] in ADMIN_RARITIES]
    forced_seasonal = frozenset(modifiers) & frozenset(SEASONAL_WINDOWS)
    if forced_seasonal:
        pool = [pair for pair in pool if all(seasonal_image(pair[0], key) for key in forced_seasonal)]
    if not pool:
        raise ValueError('No positive spawn weights configured')
    chain = db.execute('SELECT c.streak FROM chain_streaks c JOIN claim_turns t ON t.guild=c.guild AND t.owner=c.owner WHERE c.guild=?', (guild_id,)).fetchone()
    chain_level = max(0, chain['streak'] - 1) if chain else 0
    rarity_multiplier = min(CHAIN_RARITY_CAP, 1 + chain_level * CHAIN_RARITY_STEP)
    weights = [pair[2] * (rarity_multiplier if pair[1] != 'normal' and chosen_rarity is None else 1) for pair in pool]
    item,rarity,_=random.choices(pool,weights=weights)[0]
    modifiers = list(dict.fromkeys(modifiers + roll_seasonal_modifiers(item)))
    now = datetime.now(MODIFIER_TIMEZONE).date()
    moon_chance = BLOOD_MOON_HALLOWEEN_CHANCE if (now.month, now.day) == (10, 31) else BLOOD_MOON_CHANCE
    if 'blood_moon' not in modifiers and random.random() < moon_chance:
        modifiers.append('blood_moon')
    if 'cloudy' not in modifiers and roll_cloudy_modifier(rarity):
        modifiers.append('cloudy')
    if 'bouncy' not in modifiers and random.random() < BOUNCY_CHANCE:
        modifiers.append('bouncy')
    if 'hyper' not in modifiers and random.random() < HYPER_CHANCE:
        modifiers.append('hyper')
    rolled_horizontal, rolled_vertical = roll_flip_modifiers()
    flip_horizontal = rolled_horizontal if horizontal_flip is None else horizontal_flip
    flip_vertical = rolled_vertical if vertical_flip is None else vertical_flip
    holders=[r['owner'] for r in db.execute('SELECT owner FROM bounties WHERE guild=? AND card=? AND rarity=? ORDER BY owner',(guild_id,item['id'],rarity))]
    lifetime=600 if holders else CLAIM_SECONDS
    embed = discord.Embed(title=item['name'], description=f"{variant_label(rarity, flip_horizontal, flip_vertical, modifiers)} · Type **Claim** in this channel to collect this image.", color=0x79B86D)
    attachment = await set_card_embed_image(embed, item, rarity, flip_horizontal, flip_vertical, filename=f'spawn-{guild_id}.png', modifiers=modifiers)
    embed.set_footer(text=f'Expires in {lifetime // 60} minutes.'+(' · Active bounty' if holders else ''))
    first=holders[:50]
    mentions=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=user) for user in first],replied_user=False)
    send_kwargs=dict(content=('Bounty target appeared! '+' '.join(f'<@{user}>' for user in first)) if first else None,embed=embed,allowed_mentions=mentions)
    if attachment:
        send_kwargs['file']=attachment
    message=await channel.send(**send_kwargs)
    spawned_at = time.time()
    with db:
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?', (spawned_at + lifetime, guild_id))
        db.execute('INSERT OR REPLACE INTO spawns(guild,channel,message,country,expires,rarity,flip_horizontal,flip_vertical) VALUES(?,?,?,?,?,?,?,?)',
                   (guild_id, channel.id, message.id, item['id'], spawned_at + lifetime, rarity, int(flip_horizontal), int(flip_vertical)))
        db.execute('UPDATE spawns SET modifiers=? WHERE guild=?', (json.dumps(modifiers), guild_id))
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
    expired=db.execute('SELECT guild FROM spam_periods WHERE until_time<=?',(time.time(),)).fetchall()
    for expired_row in expired:
        with db:
            db.execute('DELETE FROM spam_periods WHERE guild=? AND until_time<=?',(expired_row['guild'],time.time()))
            db.execute('DELETE FROM rigged_spam WHERE guild=?',(expired_row['guild'],))
        settings=db.execute('SELECT channel FROM settings WHERE guild=?',(expired_row['guild'],)).fetchone()
        if settings:
            try:
                channel=bot.get_channel(settings['channel']) or await bot.fetch_channel(settings['channel'])
                await channel.send('Spawn spam has ended. Normal 5-minute spawning has resumed.')
            except discord.HTTPException:
                logging.exception('Could not announce end of spawn spam')
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


def card_index(card_id):
    locked = db.execute('SELECT 1 FROM sell_locks WHERE card_id=?', (card_id,)).fetchone()
    favorite = db.execute('SELECT 1 FROM card_favorites f JOIN cards c ON c.id=f.card_id AND c.guild=f.guild AND c.owner=f.owner WHERE c.id=?', (card_id,)).fetchone()
    return f"#{card_id}" + (' 🔒' if locked else '') + (' ❤️' if favorite else '')


# Autocomplete supports the full list; Discord static choices are limited to 25.
SORT_FIELDS = {
    'id': 'Card ID', 'name': 'Card name', 'rarity': 'Rarity',
    'value': 'Sale value', 'artist': 'Artist', 'orientation': 'Orientation',
    'copies': 'Copies of card type', 'variant_copies': 'Copies of exact variant',
    'modifiers': 'Modifier count', 'modifier_names': 'Modifier names',
    'flips': 'Flip count', 'locked': 'Locked', 'favorite': 'Favourite',
}
SORT_MODES = {f'{key}_{direction}': f'{label}: {direction}'
              for key, label in SORT_FIELDS.items() for direction in ('ascending', 'descending')}
SORT_MODES['manual'] = 'Manual order'


def sort_collection(rows, guild, owner):
    preference = db.execute('SELECT mode FROM collection_sorts WHERE guild=? AND owner=?', (guild, owner)).fetchone()
    mode = preference['mode'] if preference and preference['mode'] in SORT_MODES else 'id_ascending'
    if mode == 'manual':
        order = manual_ids(guild, owner)
        positions = {card: n for n, card in enumerate(order)}
        return sorted(rows, key=lambda r: (positions.get(r['id'], len(order)), r['id']))
    field, direction = mode.rsplit('_', 1)
    # Count across the whole collection, even in filtered searches.
    all_rows = db.execute('SELECT * FROM cards WHERE guild=? AND owner=?', (guild, owner)).fetchall()
    def variant(row):
        return (row['country'], row['rarity'], *flip_flags(row), tuple(sorted(card_modifiers(row))))
    from collections import Counter
    copies = Counter(r['country'] for r in all_rows)
    variants = Counter(variant(r) for r in all_rows)
    locks = {r[0] for r in db.execute('SELECT card_id FROM sell_locks WHERE guild=? AND owner=?', (guild, owner))}
    favorites = {r[0] for r in db.execute('SELECT card_id FROM card_favorites WHERE guild=? AND owner=?', (guild, owner))}
    def key(row):
        item = item_info(row['country'])
        mods = card_modifiers(row)
        horizontal, vertical = flip_flags(row)
        values = {
            'id': row['id'], 'name': item['name'].casefold(),
            'rarity': list(eco.RARITIES).index(row['rarity']) if row['rarity'] in eco.RARITIES else -1,
            'value': sale_price(item, row['rarity'], row), 'type': row['country'].casefold(),
            'artist': str(item.get('ARTIST:', 'Unknown')).casefold(),
            'orientation': item.get('orientation', ''), 'copies': copies[row['country']],
            'variant_copies': variants[variant(row)], 'modifiers': len(mods),
            'modifier_names': tuple(sorted(mods)), 'flips': int(horizontal) + int(vertical),
            'horizontal': horizontal, 'vertical': vertical, 'locked': row['id'] in locks,
            'favorite': row['id'] in favorites, 'admin': 'admin' in mods,
            'cloudy': 'cloudy' in mods, 'bouncy': 'bouncy' in mods,
            'seasonal': len(set(mods) & set(SEASONAL_WINDOWS)),
        }
        return values[field]
    # Card ID is the stable tie breaker in either direction.
    return sorted(sorted(rows, key=lambda r: r['id']), key=key, reverse=direction == 'descending')


@bot.tree.command(name='sort', description='Save your collection order: 27 options. Use /sortindex for the full list.')
@app_commands.guild_only()
@app_commands.describe(order='Search a field such as rarity, name, value, locked, or favourite; choose a direction.')
async def sort_command(interaction: discord.Interaction, order: str):
    if order not in SORT_MODES:
        return await interaction.response.send_message('Choose an order from autocomplete. There are 27 options; type part of a field or direction to search.', ephemeral=True)
    with db:
        db.execute('INSERT INTO collection_sorts VALUES(?,?,?) ON CONFLICT(guild,owner) DO UPDATE SET mode=excluded.mode',
                   (interaction.guild_id, interaction.user.id, order))
    await interaction.response.send_message(f'Collection order saved: {SORT_MODES[order]}. Applies to /collection, /collectionlocked, /collectionunlocked and /collectionsearch in this server.', ephemeral=True)


@sort_command.autocomplete('order')
async def sort_autocomplete(interaction: discord.Interaction, current: str):
    words = current.casefold().split()
    return [app_commands.Choice(name=label, value=mode) for mode, label in SORT_MODES.items()
            if all(word in (label + ' ' + mode).casefold() for word in words)][:25]


def manual_ids(guild, owner):
    row = db.execute('SELECT ids FROM manual_collection_orders WHERE guild=? AND owner=?', (guild, owner)).fetchone()
    owned = {r[0] for r in db.execute('SELECT id FROM cards WHERE guild=? AND owner=?', (guild, owner))}
    return [card for card in json.loads(row['ids']) if card in owned] if row else []


def save_manual(guild, owner, ids):
    with db:
        db.execute('INSERT OR REPLACE INTO manual_collection_orders VALUES(?,?,?)', (guild, owner, json.dumps(ids)))
        db.execute("INSERT OR REPLACE INTO collection_sorts VALUES(?,?,'manual')", (guild, owner))


@bot.tree.command(name='manualsort', description='Enter one card ID per use, in order. Reset starts a new order.')
@app_commands.guild_only()
async def manualsort(interaction: discord.Interaction, id: int, reset: bool = False):
    guild, owner = interaction.guild_id, interaction.user.id
    if not db.execute('SELECT 1 FROM cards WHERE id=? AND guild=? AND owner=?', (id, guild, owner)).fetchone():
        return await interaction.response.send_message('You must own that card in this server.', ephemeral=True)
    ids = [] if reset else manual_ids(guild, owner)
    if id in ids:
        return await interaction.response.send_message('That ID is already in your manual order. Use /manualsortbefore or /manualsortafter to move it.', ephemeral=True)
    ids.append(id)
    save_manual(guild, owner, ids)
    await interaction.response.send_message(f'Card #{id} placed at position {len(ids)}. Enter the next ID with /manualsort. Remaining cards follow in ID order.', ephemeral=True)


async def move_manual(interaction, card, target, after):
    guild, owner = interaction.guild_id, interaction.user.id
    rows = db.execute('SELECT * FROM cards WHERE guild=? AND owner=?', (guild, owner)).fetchall()
    owned = {r['id'] for r in rows}
    if card == target or card not in owned or target not in owned:
        return await interaction.response.send_message('Choose two different cards you own in this server.', ephemeral=True)
    ids = [r['id'] for r in sort_collection(rows, guild, owner)]
    ids.remove(card)
    ids.insert(ids.index(target) + int(after), card)
    save_manual(guild, owner, ids)
    await interaction.response.send_message(f'Card #{card} moved {"after" if after else "before"} #{target}. Manual order saved.', ephemeral=True)


@bot.tree.command(name='manualsortbefore', description='Move your card immediately before another card.')
@app_commands.guild_only()
async def manualsortbefore(interaction: discord.Interaction, id: int, target: int):
    await move_manual(interaction, id, target, False)


@bot.tree.command(name='manualsortafter', description='Move your card immediately after another card.')
@app_commands.guild_only()
async def manualsortafter(interaction: discord.Interaction, id: int, target: int):
    await move_manual(interaction, id, target, True)


@bot.tree.command(name='sortindex', description='List every collection sorting option and manual sorting command.')
@app_commands.guild_only()
async def sortindex(interaction: discord.Interaction):
    lines = [f'{n}. {label} ({mode})' for n, (mode, label) in enumerate(SORT_MODES.items(), 1)]
    await interaction.response.send_message('Collection sorting options:\n' + '\n'.join(lines) + '\n/manualsort id [reset] adds IDs one at a time. /manualsortbefore id target and /manualsortafter id target move cards. /sort order:manual restores your saved order.', ephemeral=True)


async def send_collection(interaction, member, page, locked=None):
    member = member or interaction.user
    query = 'FROM cards WHERE guild=? AND owner=?'
    params = (interaction.guild_id, member.id)
    if locked is not None:
        query += (' AND ' + ('' if locked else 'NOT ') +
                  'EXISTS (SELECT 1 FROM sell_locks WHERE card_id=cards.id)')
    total = db.execute('SELECT COUNT(*) ' + query, params).fetchone()[0]
    rows = sort_collection(db.execute('SELECT * ' + query, params).fetchall(), interaction.guild_id, member.id)
    rows = rows[(page-1)*20:page*20]
    lines = [f"{card_index(r['id'])} · {discord.utils.escape_markdown(item_info(r['country'])['name'])} · {variant_label(r['rarity'], *flip_flags(r), modifiers=card_modifiers(r))}" for r in rows]
    label = 'collection' if locked is None else ('locked cards' if locked else 'unlocked cards')
    embed = discord.Embed(title=f'{member.display_name}’s {label}', description='\n'.join(lines) or 'No cards on this page.')
    embed.set_footer(text=f'{total} cards · Page {page}/{max(1, (total+19)//20)} · Collections are per server')
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name='collectionlocked', description='View only locked cards in a collection.')
@app_commands.guild_only()
async def collectionlocked(interaction: discord.Interaction, member: discord.Member = None, page: app_commands.Range[int, 1, 100000] = 1):
    await send_collection(interaction, member, page, locked=True)


@bot.tree.command(name='collectionunlocked', description='View only unlocked cards in a collection.')
@app_commands.guild_only()
async def collectionunlocked(interaction: discord.Interaction, member: discord.Member = None, page: app_commands.Range[int, 1, 100000] = 1):
    await send_collection(interaction, member, page, locked=False)


@bot.tree.command(name='collection', description='View a collection and card IDs for trading.')
@app_commands.guild_only()
async def collection(interaction: discord.Interaction, member: discord.Member = None, page: app_commands.Range[int, 1, 100000] = 1):
    await send_collection(interaction, member, page)


@bot.tree.command(name='view', description='View a claimed image by its card ID.')
@app_commands.guild_only()
async def view_card(interaction: discord.Interaction, card_id: int):
    row = db.execute('SELECT * FROM cards WHERE id=? AND guild=?', (card_id, interaction.guild_id)).fetchone()
    if not row:
        return await interaction.response.send_message('No card with that ID in this server.', ephemeral=False)
    item = item_info(row['country'])
    fh, fv = flip_flags(row)
    embed = discord.Embed(title=item['name'], description=f"Card {card_index(row['id'])} · {variant_label(row['rarity'], fh, fv, card_modifiers(row))} · Owner: <@{row['owner']}>")
    attachment = await set_card_embed_image(embed, item, row['rarity'], fh, fv, filename=f'view-{row["id"]}.png', modifiers=card_modifiers(row))
    if not embed.image.url:
        embed.set_footer(text='Legacy card: no image has been configured for this item.')
    if attachment:
        await interaction.response.send_message(embed=embed, file=attachment, ephemeral=False)
    else:
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
    await interaction.response.send_message(f"{member.display_name}: {interaction.user.display_name} offers {card_index(give_id)} ({discord.utils.escape_markdown(item_info(a['country'])['name'])}, {variant_label(a['rarity'], *flip_flags(a), modifiers=card_modifiers(a))}) for your {card_index(want_id)} ({discord.utils.escape_markdown(item_info(b['country'])['name'])}, {variant_label(b['rarity'], *flip_flags(b), modifiers=card_modifiers(b))}). Accept within 2 minutes. Otherwise the offer expires.", view=view)


@bot.tree.command(name='balance', description='Check your chudlets balance.')
@app_commands.guild_only()
async def wallet(interaction: discord.Interaction):
    await interaction.response.send_message(f'You have {eco.balance(db,interaction.guild_id,interaction.user.id):,} chudlets.', ephemeral=False)


@bot.tree.command(name='list', description='All cards, with owned cards above unowned cards.')
@app_commands.guild_only()
async def card_list(interaction: discord.Interaction, page: app_commands.Range[int,1,100000] = 1):
    rows = db.execute('SELECT country,rarity,COUNT(*) AS n FROM cards WHERE guild=? AND owner=? GROUP BY country,rarity', (interaction.guild_id,interaction.user.id)).fetchall()
    locked_variants = {(r['country'], r['rarity']) for r in db.execute('SELECT country,rarity FROM cards WHERE guild=? AND owner=? AND id IN (SELECT card_id FROM sell_locks)', (interaction.guild_id, interaction.user.id))}
    favorite_variants = {(r['country'], r['rarity']) for r in db.execute('SELECT country,rarity FROM cards WHERE guild=? AND owner=? AND id IN (SELECT card_id FROM card_favorites WHERE guild=? AND owner=?)', (interaction.guild_id, interaction.user.id, interaction.guild_id, interaction.user.id))}
    flipped_variants = {(r['country'], r['rarity']) for r in db.execute('SELECT country,rarity FROM cards WHERE guild=? AND owner=? AND (flip_horizontal=1 OR flip_vertical=1)', (interaction.guild_id, interaction.user.id))}
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
        variants=', '.join(f"{r.title() + (' ✨' if (i,r) in flipped_variants or list(eco.RARITIES).index(r) >= list(eco.RARITIES).index('holographic') else '')}: {counts.get(r,0)}" + (' 🔒' if (i,r) in locked_variants else '') + (' ❤️' if (i,r) in favorite_variants else '') for r in eco.RARITIES)
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
    price=sale_price(item,row['rarity'],row)
    bonus=eco.sale_bonus(db,interaction.guild_id,interaction.user.id)
    await interaction.response.send_message(f"Sell {card_index(card_id)}, {discord.utils.escape_markdown(item['name'])} ({variant_label(row['rarity'], *flip_flags(row), modifiers=card_modifiers(row))}), for {price+bonus:,} chudlets (including {bonus:,} Mass Credit bonus)? This removes that copy from your collection. Confirm within 60 seconds.",view=SellView(interaction.guild_id,interaction.user.id,card_id,price),ephemeral=False)


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
async def spawnrigged(interaction:discord.Interaction,card:str,rarity:str='normal',horizontal_flip:bool=None,vertical_flip:bool=None,admin:bool=False,spooky:bool=False,hungry:bool=False,festive:bool=False,cloudy:bool=False,bouncy:bool=False,hyper:bool=False,blood_moon:bool=False):
    if (admin or cloudy) and rarity is not None and rarity not in ADMIN_RARITIES:
        return await interaction.response.send_message('ADMIN only supports normal, holographic, glitch and inverted. CLOUDY uses the same rarities.', ephemeral=True)
    modifiers = ['admin'] if admin else []
    if cloudy:
        modifiers.append('cloudy')
    if bouncy:
        modifiers.append('bouncy')
    if hyper:
        modifiers.append('hyper')
    if blood_moon:
        modifiers.append('blood_moon')
    for key, enabled in (('spooky', spooky), ('hungry', hungry), ('festive', festive)):
        if enabled:
            if card not in CATALOG or not seasonal_image(CATALOG[card], key):
                return await interaction.response.send_message(f'Set this card orientation to TALL or WIDE in items.json and configure the matching {key.upper()} image in modifiers.json.', ephemeral=True)
            modifiers.append(key)

    if card not in CATALOG or rarity not in eco.RARITIES:
        return await interaction.response.send_message('Choose a card from autocomplete and a valid rarity.',ephemeral=False)
    row=db.execute('SELECT channel FROM settings WHERE guild=?',(interaction.guild_id,)).fetchone()
    if not row:
        return await interaction.response.send_message('Run /setup first.',ephemeral=False)
    await interaction.response.defer(ephemeral=False)
    channel=bot.get_channel(row['channel']) or await bot.fetch_channel(row['channel'])
    success=await spawn(interaction.guild_id,channel,CATALOG[card],rarity,horizontal_flip,vertical_flip,modifiers)
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
    embed.set_footer(text='Submit your drawing to me. Chudlets are awarded manually after approval.')
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

@bot.tree.command(name='profilecustomize', description='Customize your profile. Omitted options keep their current values.')
@app_commands.guild_only()
@app_commands.describe(accentcolour='Hex colour, e.g. #64D545; use reset to clear.', footerimage='HTTP(S) image URL; use reset to clear.', favoritecard='Owned card ID; use 0 to clear.', setcompletion='Display currently completed card sets.', raritycompletion='Display cards for which you currently own every base rarity.')
async def profilecustomize(interaction: discord.Interaction, accentcolour: str = None, footerimage: str = None, favoritecard: int = None, setcompletion: bool = None, raritycompletion: bool = None):
    updates = {}
    if accentcolour is not None:
        colour = accentcolour.strip().lstrip('#')
        if accentcolour.lower() == 'reset':
            updates['accentcolour'] = None
        elif re.fullmatch(r'[0-9a-fA-F]{6}', colour):
            updates['accentcolour'] = int(colour, 16)
        else:
            return await interaction.response.send_message('Use a six-digit hex colour, such as #64D545.')
    if footerimage is not None:
        if footerimage.lower() == 'reset':
            updates['footerimage'] = None
        else:
            parsed = urlparse(footerimage)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or len(footerimage) > 2048:
                return await interaction.response.send_message('Use an HTTP or HTTPS image URL.')
            updates['footerimage'] = footerimage
    if favoritecard is not None:
        if favoritecard != 0 and not db.execute('SELECT 1 FROM cards WHERE id=? AND guild=? AND owner=?', (favoritecard, interaction.guild_id, interaction.user.id)).fetchone():
            return await interaction.response.send_message('Choose a card you own in this server, or 0 to clear it.')
        updates['favoritecard'] = favoritecard or None
    if setcompletion is not None:
        updates['setcompletion'] = int(setcompletion)
    if raritycompletion is not None:
        updates['raritycompletion'] = int(raritycompletion)
    if not updates:
        return await interaction.response.send_message('Choose any profile option to update. Use reset for the colour or image, or favoritecard:0 to clear the featured card.')
    with db:
        db.execute('INSERT OR IGNORE INTO profile_customizations(guild,owner) VALUES(?,?)', (interaction.guild_id, interaction.user.id))
        fields = ', '.join(key + '=?' for key in updates)
        db.execute('UPDATE profile_customizations SET ' + fields + ' WHERE guild=? AND owner=?', (*updates.values(), interaction.guild_id, interaction.user.id))
    await interaction.response.send_message('Profile updated. View it with /profile.')


@bot.tree.command(name='favorite', description='Toggle a heart beside one of your cards in listings.')
@app_commands.guild_only()
async def favorite(interaction: discord.Interaction, card_id: int):
    if not db.execute('SELECT 1 FROM cards WHERE id=? AND guild=? AND owner=?', (card_id, interaction.guild_id, interaction.user.id)).fetchone():
        return await interaction.response.send_message('Choose a card you own in this server.')
    args = (interaction.guild_id, interaction.user.id, card_id)
    with db:
        removed = db.execute('DELETE FROM card_favorites WHERE guild=? AND owner=? AND card_id=?', args).rowcount
        if not removed:
            db.execute('INSERT INTO card_favorites VALUES(?,?,?)', args)
    await interaction.response.send_message(f"{'Removed favorite from' if removed else 'Favorited'} card {card_index(card_id)}.")


@bot.tree.command(name='profile',description='View Chudlets, Mass Credit and rarest owned card.')
@app_commands.guild_only()
async def profile(interaction:discord.Interaction,user:discord.Member=None):
    member=user or interaction.user
    mc=eco.mass_credit(db,interaction.guild_id,member.id)
    money=eco.balance(db,interaction.guild_id,member.id)
    rows=db.execute('SELECT * FROM cards WHERE guild=? AND owner=?',(interaction.guild_id,member.id)).fetchall()
    order=list(eco.RARITIES)
    # RARITIES is ordered from most common to least common.
    rarest=max(rows,key=lambda row:order.index(row['rarity']) if row['rarity'] in order else -1) if rows else None
    description=(f"{item_info(rarest['country'])['name']} ({variant_label(rarest['rarity'], *flip_flags(rarest), modifiers=card_modifiers(rarest))}), Card {card_index(rarest['id'])}" if rarest else 'None')
    custom = db.execute('SELECT * FROM profile_customizations WHERE guild=? AND owner=?', (interaction.guild_id, member.id)).fetchone()
    embed=discord.Embed(title=f'{member.display_name} | Profile', colour=custom['accentcolour'] if custom and custom['accentcolour'] is not None else None)
    embed.add_field(name='Mass Credit',value=f'{mc:,}',inline=True)
    embed.add_field(name='Chudlets',value=f'{money:,}',inline=True)
    embed.add_field(name='Rarest card',value=discord.utils.escape_markdown(description),inline=False)
    attachment = None
    favorite = None
    if custom and custom['favoritecard']:
        favorite = db.execute('SELECT * FROM cards WHERE id=? AND guild=? AND owner=?', (custom['favoritecard'], interaction.guild_id, member.id)).fetchone()
        if favorite:
            await interaction.response.defer()
            attachment = await set_card_embed_image(embed, item_info(favorite['country']), favorite['rarity'], *flip_flags(favorite), thumbnail=True, filename='favorite.png', modifiers=card_modifiers(favorite))
            embed.add_field(name='Favorite card', value=f"{card_index(favorite['id'])} · {discord.utils.escape_markdown(item_info(favorite['country'])['name'])} · {variant_label(favorite['rarity'], *flip_flags(favorite), modifiers=card_modifiers(favorite))}", inline=False)
    if custom and custom['setcompletion']:
        owned = {r['country'] for r in rows}
        completed = [d['name'] for d in eco.SET_DEFINITIONS.values() if d['members'] and d['members'] <= owned]
        text = '\n'.join(completed) or 'None yet.'
        embed.add_field(name=f'Sets completed ({len(completed)}/{len(eco.SET_DEFINITIONS)})', value=text[:1024], inline=False)
    if custom and custom['raritycompletion']:
        rarities_by_card = {}
        for row in rows:
            rarities_by_card.setdefault(row['country'], set()).add(row['rarity'])
        completed = sorted((item for item, rarities in rarities_by_card.items() if set(eco.RARITIES) <= rarities), key=lambda item: item_info(item)['name'].casefold())
        names = [discord.utils.escape_markdown(item_info(item)['name']) for item in completed]
        text = '\n'.join(names) or 'None yet.'
        if len(text) > 1024:
            text = text[:1000].rsplit('\n', 1)[0] + '\n…'
        embed.add_field(name=f'Rarity sets completed ({len(completed)})', value=text, inline=False)
    if custom and custom['footerimage']:
        embed.set_image(url=custom['footerimage'])
    embed.set_footer(text=f'Mass Credit sale bonus: +{mc//10:,} chudlets per card sold.')
    sender = interaction.followup.send if favorite else interaction.response.send_message
    await sender(embed=embed, **({'file': attachment} if attachment else {}))


def gift_owned_card(guild,sender,recipient,card_id):
    if sender==recipient:
        return None
    row=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? AND id=?',(guild,sender,card_id)).fetchone()
    if not row:
        return None
    db.execute('UPDATE cards SET owner=? WHERE guild=? AND owner=? AND id=?',(recipient,guild,sender,card_id))
    db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)',(guild,recipient,row['country']))
    reward=eco.rarity_unlock_bonus(db,guild,recipient,row['country'],row['rarity'])+eco.set_bonus(db,guild,recipient,row['country'])+eco.card_set_bonus(db,guild,recipient)
    return row['country'],row['rarity'],reward,bool(row['flip_horizontal']) if 'flip_horizontal' in row.keys() else False,bool(row['flip_vertical']) if 'flip_vertical' in row.keys() else False


@bot.tree.command(name='givecard',description='Give one of your cards to another player for free.')
@app_commands.guild_only()
async def givecard(interaction:discord.Interaction,card_id:int,user:discord.Member):
    if user.bot or user.id==interaction.user.id:
        return await interaction.response.send_message('Choose another human player.',ephemeral=False)
    with db:
        result=gift_owned_card(interaction.guild_id,interaction.user.id,user.id,card_id)
    if result is None:
        return await interaction.response.send_message('You do not own that card in this server. Check /collection.',ephemeral=False)
    item,rarity,reward,flip_horizontal,flip_vertical=result
    modifiers=card_modifiers(db.execute('SELECT * FROM cards WHERE id=?',(card_id,)).fetchone())
    text=f"Gave card #{card_id}, {discord.utils.escape_markdown(item_info(item)['name'])} ({variant_label(rarity, flip_horizontal, flip_vertical, modifiers)}), to {discord.utils.escape_markdown(user.display_name)}."
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
            current=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? AND id NOT IN (SELECT card_id FROM sell_locks) ORDER BY id',(self.guild,self.owner)).fetchall()
            current_quote=[(r['id'],sale_price(item_info(r['country']),r['rarity'],r)) for r in current]
            valid=current_quote==self.quoted
            if valid:
                total=sum(price for _,price in self.quoted)+eco.sale_bonus(db,self.guild,self.owner,len(self.quoted))
                ids=[r['id'] for r in current]
                db.executemany('DELETE FROM cards WHERE guild=? AND owner=? AND id=?',[(self.guild,self.owner,i) for i in ids])
                eco.recycle_ids(db,ids)
                db.executemany('DELETE FROM sell_locks WHERE card_id=?',[(i,) for i in ids])
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
    rows=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? AND id NOT IN (SELECT card_id FROM sell_locks) ORDER BY id',(interaction.guild_id,interaction.user.id)).fetchall()
    if not rows:
        return await interaction.response.send_message('You have no cards to sell in this server.',ephemeral=False)
    quoted=[(r['id'],sale_price(item_info(r['country']),r['rarity'],r)) for r in rows]
    total=sum(price for _,price in quoted)+eco.sale_bonus(db,interaction.guild_id,interaction.user.id,len(quoted))
    await interaction.response.send_message(f'Sell {len(rows)} unlocked cards, including their last copies, for {total:,} chudlets? Locked cards will remain in your collection. Shop items are unaffected. Confirm within 60 seconds.',view=SellAllView(interaction.guild_id,interaction.user.id,quoted),ephemeral=False)


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
    await interaction.response.send_message(f"Bounty set: **{discord.utils.escape_markdown(item_info(card)['name'])} ({rarity.replace('_',' ').title()})**. This exact combination gets 50% more spawn weight in this server. I’ll ping you when it appears, and it will last 10 minutes. This replaces your previous bounty and clears when you claim the target. Other players can still claim it.",ephemeral=False)


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
                embed.add_field(name='Rarity and price',value=f'{self.rarity.replace("_"," " ).title()} · Buy: {price*5:,} chudlets · Sell: {price:,}\nShop purchases are always unflipped. Flip variants come from spawns only.',inline=False)
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
        # Flip modifiers are spawn-exclusive; purchased cards are always unflipped.
        with db:
            result=eco.buy_card(db,self.guild,self.owner,self.card_id,self.rarity,price,interaction.id,False,False)
        if result is None:
            return await interaction.response.send_message(f'You need {price:,} chudlets. Your balance is {eco.balance(db,self.guild,self.owner):,}.',ephemeral=False)
        self.finished=True
        self.stop()
        card_id,earned,flip_horizontal,flip_vertical=result
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




def matched_card_ids(guild, owner, card, rarity=None, sell_last=True, respect_locks=False):
    rows=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? ORDER BY id',(guild,owner)).fetchall()
    chosen=[r for r in rows if (not card or card.casefold() in item_info(r['country'])['name'].casefold() or card.casefold()==r['country'].casefold()) and (not rarity or rarity==r['rarity'])]
    if not sell_last:
        counts={}
        for row in rows:
            key=(row['country'],row['rarity'])
            counts[key]=counts.get(key,0)+1
        keep=set()
        for row in reversed(chosen):
            key=(row['country'],row['rarity'])
            if counts.get(key,0)>1:
                counts[key]-=1
            else:
                keep.add(row['id'])
        chosen=[r for r in chosen if r['id'] not in keep]
    if respect_locks:
        locks={r['card_id'] for r in db.execute('SELECT card_id FROM sell_locks WHERE guild=? AND owner=?',(guild,owner))}
        chosen=[r for r in chosen if r['id'] not in locks]
    return chosen


@bot.tree.command(name='collectionsearch',description='Search your collection by card name and optional rarity.')
@app_commands.guild_only()
@app_commands.choices(rarity=[app_commands.Choice(name=r.title(),value=r) for r in eco.RARITIES])
async def collectionsearch(interaction:discord.Interaction,card:str='',rarity:str=None,page:app_commands.Range[int,1,100000]=1):
    rows=sort_collection(matched_card_ids(interaction.guild_id,interaction.user.id,card,rarity), interaction.guild_id, interaction.user.id)
    portion=rows[(page-1)*20:page*20]
    text='\n'.join(f"{card_index(r['id'])} · {discord.utils.escape_markdown(item_info(r['country'])['name'])} · {variant_label(r['rarity'], *flip_flags(r), modifiers=card_modifiers(r))}" for r in portion) or 'No matching cards.'
    await interaction.response.send_message(embed=discord.Embed(title=f'Collection search ({len(rows)} found) · Page {page}',description=text))

@bot.tree.command(name='cardsearch',description='Find cards of a type and the players who own them.')
@app_commands.guild_only()
@app_commands.choices(rarity=[app_commands.Choice(name=r.title(),value=r) for r in eco.RARITIES])
async def cardsearch(interaction:discord.Interaction,card:str='',rarity:str=None,page:app_commands.Range[int,1,100000]=1):
    rows=db.execute('SELECT * FROM cards WHERE guild=? ORDER BY id',(interaction.guild_id,)).fetchall()
    chosen=[r for r in rows if (not card or card.casefold() in item_info(r['country'])['name'].casefold() or card.casefold()==r['country'].casefold()) and (not rarity or rarity==r['rarity'])]
    portion=chosen[(page-1)*15:page*15]
    text='\n'.join(f"{card_index(r['id'])} · {discord.utils.escape_markdown(item_info(r['country'])['name'])} ({variant_label(r['rarity'], *flip_flags(r), modifiers=card_modifiers(r))}) · <@{r['owner']}>" for r in portion) or 'No matching cards.'
    await interaction.response.send_message(embed=discord.Embed(title=f'All collections ({len(chosen)} found) · Page {page}',description=text))

@bot.tree.command(name='selllock',description='Toggle protection against /sellall and /sellalltype.')
@app_commands.guild_only()
async def selllock(interaction:discord.Interaction,card_id:int):
    row=db.execute('SELECT 1 FROM cards WHERE guild=? AND owner=? AND id=?',(interaction.guild_id,interaction.user.id,card_id)).fetchone()
    if not row:
        return await interaction.response.send_message('You do not own that card.')
    with db:
        if db.execute('SELECT 1 FROM sell_locks WHERE card_id=?',(card_id,)).fetchone():
            db.execute('DELETE FROM sell_locks WHERE card_id=?',(card_id,)); result='Unlocked'
        else:
            db.execute('INSERT INTO sell_locks VALUES(?,?,?)',(interaction.guild_id,interaction.user.id,card_id));result='Locked'
    await interaction.response.send_message(f'{result} card #{card_id}.')

class BulkTypeConfirm(discord.ui.View):
    def __init__(self,guild,owner,ids):
        super().__init__(timeout=60);self.guild=guild;self.owner=owner;self.ids=ids;self.finished=False
    @discord.ui.button(label='Confirm sale',style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        if interaction.user.id!=self.owner:
            return await interaction.response.send_message('This sale belongs to another player.',ephemeral=True)
        if self.finished: return await interaction.response.send_message('Already processed.',ephemeral=True)
        self.finished=True
        with db:
            rows=db.execute('SELECT * FROM cards WHERE guild=? AND owner=? AND id NOT IN (SELECT card_id FROM sell_locks)',(self.guild,self.owner)).fetchall()
            selling=[r for r in rows if r['id'] in self.ids]
            valid=len(selling)==len(self.ids)
            if valid:
                prices=sum(sale_price(item_info(r['country']),r['rarity'],r) for r in selling)
                total=prices+eco.sale_bonus(db,self.guild,self.owner,len(selling))
                db.executemany('DELETE FROM cards WHERE id=?',[(r['id'],) for r in selling])
                eco.recycle_ids(db,self.ids)
                eco.credit(db,self.guild,self.owner,total)
        self.stop()
        await interaction.response.edit_message(content=f'Sold {len(self.ids)} cards for {total:,} chudlets.' if valid else 'Sale cancelled because cards changed or were locked.',view=None)
    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        if interaction.user.id!=self.owner: return await interaction.response.send_message('Not your sale.',ephemeral=True)
        self.stop();await interaction.response.edit_message(content='Sale cancelled.',view=None)

@bot.tree.command(name='sellalltype',description='Sell matching cards, optionally retaining the last copy of each rarity.')
@app_commands.guild_only()
@app_commands.choices(rarity=[app_commands.Choice(name=r.title(),value=r) for r in eco.RARITIES])
async def sellalltype(interaction:discord.Interaction,card:str,rarity:str=None,sell_last:bool=False):
    rows=matched_card_ids(interaction.guild_id,interaction.user.id,card,rarity,sell_last,True)
    if not rows: return await interaction.response.send_message('No eligible unlocked cards found.')
    ids=[r['id'] for r in rows]
    total=sum(sale_price(item_info(r['country']),r['rarity'],r) for r in rows)+eco.sale_bonus(db,interaction.guild_id,interaction.user.id,len(rows))
    await interaction.response.send_message(f'Sell {len(ids)} matching cards for {total:,} chudlets? Locked cards are skipped. sell_last={sell_last}. Confirm within 60 seconds.',view=BulkTypeConfirm(interaction.guild_id,interaction.user.id,ids))

@bot.tree.command(name='ahsell',description='List your card for sale to other members for chudlets.')
@app_commands.guild_only()
async def ahsell(interaction:discord.Interaction,card_id:int,price:app_commands.Range[int,1,1000000000]):
    with db:
        row=db.execute('SELECT * FROM cards WHERE id=? AND guild=? AND owner=?',(card_id,interaction.guild_id,interaction.user.id)).fetchone()
        if row:
            db.execute('INSERT INTO auction_listings(card_id,guild,seller,item,rarity,flip_horizontal,flip_vertical,price,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(card_id,interaction.guild_id,interaction.user.id,row['country'],row['rarity'],int(bool(row['flip_horizontal'])) if 'flip_horizontal' in row.keys() else 0,int(bool(row['flip_vertical'])) if 'flip_vertical' in row.keys() else 0,price,time.time()))
            db.execute('UPDATE auction_listings SET modifiers=? WHERE card_id=?',(row['modifiers'],card_id))
            db.execute('DELETE FROM sell_locks WHERE card_id=?',(card_id,))
            db.execute('DELETE FROM cards WHERE id=?',(card_id,))
    await interaction.response.send_message(f'Listed card #{card_id} for {price:,} chudlets. Use /ah to find it.' if row else 'You do not own that card.')

class AuctionBuy(discord.ui.View):
    def __init__(self,listing):
        super().__init__(timeout=120);self.listing=listing
    @discord.ui.button(label='Buy card',style=discord.ButtonStyle.green)
    async def buy(self,interaction,button):
        listing=self.listing
        if interaction.guild_id!=listing['guild']:return await interaction.response.send_message('Wrong server.',ephemeral=True)
        if interaction.user.id==listing['seller']:return await interaction.response.send_message('You cannot buy your own listing.',ephemeral=True)
        with db:
            current=db.execute('SELECT * FROM auction_listings WHERE card_id=?',(listing['card_id'],)).fetchone()
            if current:
                eco.credit(db,interaction.guild_id,interaction.user.id,0)
                paid=db.execute('UPDATE wallets SET amount=amount-? WHERE guild=? AND owner=? AND amount>=?',(current['price'],interaction.guild_id,interaction.user.id,current['price'])).rowcount
                if paid:
                    eco.credit(db,interaction.guild_id,current['seller'],current['price'])
                    db.execute('INSERT INTO cards(id,guild,owner,country,rarity,flip_horizontal,flip_vertical) VALUES(?,?,?,?,?,?,?)',(current['card_id'],interaction.guild_id,interaction.user.id,current['item'],current['rarity'],current['flip_horizontal'],current['flip_vertical']))
                    db.execute('UPDATE cards SET modifiers=? WHERE id=?',(current['modifiers'],current['card_id']))
                    db.execute('DELETE FROM auction_listings WHERE card_id=?',(current['card_id'],))
                    db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)',(interaction.guild_id,interaction.user.id,current['item']))
                    unlock_reward=eco.rarity_unlock_bonus(db,interaction.guild_id,interaction.user.id,current['item'],current['rarity'])
                    eco.set_bonus(db,interaction.guild_id,interaction.user.id,current['item'])
                    eco.card_set_bonus(db,interaction.guild_id,interaction.user.id)
        if not current: return await interaction.response.send_message('This listing is no longer available.',ephemeral=True)
        if not paid: return await interaction.response.send_message('Insufficient chudlets.',ephemeral=True)
        self.stop();await interaction.response.edit_message(content=f'Purchased card #{current["card_id"]} for {current["price"]:,} chudlets!'+(f' +{unlock_reward} chudlets for a new card rarity.' if unlock_reward else ''),embed=None,view=None)
    @discord.ui.button(label='Cancel listing (seller)',style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        listing=self.listing
        if interaction.user.id!=listing['seller']:return await interaction.response.send_message('Only the seller can cancel.',ephemeral=True)
        with db:
            current=db.execute('SELECT * FROM auction_listings WHERE card_id=? AND seller=?',(listing['card_id'],interaction.user.id)).fetchone()
            if current:
                db.execute('INSERT INTO cards(id,guild,owner,country,rarity,flip_horizontal,flip_vertical) VALUES(?,?,?,?,?,?,?)',(current['card_id'],current['guild'],current['seller'],current['item'],current['rarity'],current['flip_horizontal'],current['flip_vertical']))
                db.execute('UPDATE cards SET modifiers=? WHERE id=?',(current['modifiers'],current['card_id']))
                db.execute('DELETE FROM auction_listings WHERE card_id=?',(current['card_id'],))
        self.stop();await interaction.response.edit_message(content='Listing cancelled; card returned.' if current else 'Listing no longer available.',embed=None,view=None)

@bot.tree.command(name='ah',description='Browse auction listings and buy cards.')
@app_commands.guild_only()
async def ah(interaction:discord.Interaction,page:app_commands.Range[int,1,100000]=1):
    rows=db.execute('SELECT * FROM auction_listings WHERE guild=? ORDER BY created_at,card_id LIMIT 10 OFFSET ?',(interaction.guild_id,(page-1)*10)).fetchall()
    if not rows:return await interaction.response.send_message('No listings on this page.')
    embeds=[]
    for row in rows:
        embed=discord.Embed(title=f"#{row['card_id']} · {item_info(row['item'])['name']}",description=f"{variant_label(row['rarity'], bool(row['flip_horizontal']), bool(row['flip_vertical']), card_modifiers(row))} · {row['price']:,} chudlets · Seller <@{row['seller']}>")
        embeds.append(embed)
    view=AuctionSelect(rows)
    await interaction.response.send_message(content=f'Auction market · Page {page}. Select a card to buy or cancel your listing.',embeds=embeds,view=view)

class AuctionSelect(discord.ui.View):
    def __init__(self,rows):
        super().__init__(timeout=120)
        options=[discord.SelectOption(label=f"#{r['card_id']} · {item_info(r['item'])['name'][:55]}",value=str(r['card_id'])) for r in rows]
        select=discord.ui.Select(placeholder='Choose a listing',options=options)
        async def selected(interaction):
            card_id=int(select.values[0]);row=db.execute('SELECT * FROM auction_listings WHERE guild=? AND card_id=?',(interaction.guild_id,card_id)).fetchone()
            if not row: return await interaction.response.send_message('Listing unavailable.',ephemeral=True)
            await interaction.response.send_message(f"Card #{card_id}: {item_info(row['item'])['name']} ({variant_label(row['rarity'], bool(row['flip_horizontal']), bool(row['flip_vertical']), card_modifiers(row))}) · {row['price']:,} chudlets",view=AuctionBuy(dict(row)),ephemeral=True)
        select.callback=selected;self.add_item(select)



@bot.tree.command(name='paidspawn', description='Spend 1,000 chudlets to spawn a random card.')
@app_commands.guild_only()
async def paidspawn(interaction: discord.Interaction):
    settings = db.execute('SELECT channel FROM settings WHERE guild=?', (interaction.guild_id,)).fetchone()
    if not settings:
        return await interaction.response.send_message('Run /setup first.', ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    async with spawn_lock:
        if db.execute('SELECT 1 FROM spawns WHERE guild=? AND expires>?', (interaction.guild_id, time.time())).fetchone():
            return await interaction.followup.send('A card is already waiting to be claimed. No charge.', ephemeral=True)
        with db:
            charged = db.execute('UPDATE wallets SET amount=amount-1000 WHERE guild=? AND owner=? AND amount>=1000', (interaction.guild_id,interaction.user.id)).rowcount
        if not charged:
            return await interaction.followup.send('You need 1,000 chudlets.', ephemeral=True)
        try:
            channel = bot.get_channel(settings['channel']) or await bot.fetch_channel(settings['channel'])
            success = await _spawn(interaction.guild_id, channel)
            if not success:
                with db: eco.credit(db, interaction.guild_id, interaction.user.id, 1000)
                return await interaction.followup.send('A spawn is already active. Refunded 1,000 chudlets.', ephemeral=True)
        except Exception:
            with db: eco.credit(db, interaction.guild_id, interaction.user.id, 1000)
            logging.exception('Paid spawn failed and was refunded')
            return await interaction.followup.send('Spawn failed. Refunded 1,000 chudlets.', ephemeral=True)
    await interaction.followup.send('Spent 1,000 chudlets. A random card has spawned.', ephemeral=True)


@bot.tree.command(name='paidspawnspam', description='Spend 10,000 chudlets for five minutes of spawnspam.')
@app_commands.guild_only()
async def paidspawnspam(interaction: discord.Interaction):
    settings = db.execute('SELECT channel FROM settings WHERE guild=?', (interaction.guild_id,)).fetchone()
    if not settings:
        return await interaction.response.send_message('Run /setup first.', ephemeral=True)
    async with spawn_lock:
        with db:
            charged = db.execute('UPDATE wallets SET amount=amount-10000 WHERE guild=? AND owner=? AND amount>=10000', (interaction.guild_id,interaction.user.id)).rowcount
            if charged:
                until = max(time.time(), (db.execute('SELECT until_time FROM spam_periods WHERE guild=?', (interaction.guild_id,)).fetchone() or {'until_time': 0})['until_time']) + 300
                db.execute('INSERT INTO spam_periods VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET until_time=excluded.until_time', (interaction.guild_id,until))
                db.execute('DELETE FROM rigged_spam WHERE guild=?', (interaction.guild_id,))
                if not db.execute('SELECT 1 FROM spawns WHERE guild=? AND expires>?', (interaction.guild_id,time.time())).fetchone():
                    db.execute('UPDATE settings SET next_spawn=? WHERE guild=?', (time.time(),interaction.guild_id))
    if not charged:
        return await interaction.response.send_message('You need 10,000 chudlets.', ephemeral=True)
    await interaction.response.send_message('Spent 10,000 chudlets. Five minutes of spawnspam added; the next card spawns after the current card is claimed.', ephemeral=False)

@bot.tree.command(name='spawnspam',description='Administrator: continuously spawn on claim for five minutes.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def spawnspam(interaction:discord.Interaction):
    settings=db.execute('SELECT channel FROM settings WHERE guild=?',(interaction.guild_id,)).fetchone()
    if not settings: return await interaction.response.send_message('Use /setup to configure the spawn channel first.',ephemeral=True)
    until=time.time()+300
    with db:
        db.execute('INSERT INTO spam_periods VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET until_time=excluded.until_time',(interaction.guild_id,until))
        db.execute('DELETE FROM rigged_spam WHERE guild=?',(interaction.guild_id,))
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?',(time.time(),interaction.guild_id))
    await interaction.response.send_message('Spawn spam enabled for 5 minutes. A new card will spawn after each claim; an ending announcement will be sent.')

@bot.tree.command(name='spawnspamrigged',description='Administrator: five minutes of spawns filtered by rarity, card or set.')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(rarity=[app_commands.Choice(name=r.replace('_',' ').title(),value=r) for r in eco.RARITIES])
async def spawnspamrigged(interaction:discord.Interaction,rarity:str=None,card:str=None,set:str=None,horizontal_flip:bool=None,vertical_flip:bool=None,admin:bool=False,cloudy:bool=False,spooky:bool=False,hungry:bool=False,festive:bool=False,bouncy:bool=False,hyper:bool=False,blood_moon:bool=False):
    if (admin or cloudy) and rarity is not None and rarity not in ADMIN_RARITIES:
        return await interaction.response.send_message('ADMIN only supports normal, holographic, glitch and inverted.', ephemeral=True)
    modifiers = (['admin'] if admin else []) + (['cloudy'] if cloudy else []) + (['bouncy'] if bouncy else []) + (['hyper'] if hyper else []) + (['blood_moon'] if blood_moon else [])
    modifiers += [key for key, enabled in (('spooky', spooky), ('hungry', hungry), ('festive', festive)) if enabled]

    settings=db.execute('SELECT channel FROM settings WHERE guild=?',(interaction.guild_id,)).fetchone()
    if not settings:
        return await interaction.response.send_message('Use /setup to configure the spawn channel first.',ephemeral=True)
    if rarity is not None and rarity not in eco.RARITIES:
        return await interaction.response.send_message('Invalid rarity.',ephemeral=True)
    if card is not None and card not in {item['id'] for item in ITEMS}:
        return await interaction.response.send_message('Choose an available card from autocomplete.',ephemeral=True)
    if set is not None and set not in eco.SET_DEFINITIONS:
        return await interaction.response.send_message('Choose a set from autocomplete.',ephemeral=True)
    if card is not None and set is not None and card not in eco.SET_DEFINITIONS[set]['members']:
        return await interaction.response.send_message('That card does not belong to the selected set.',ephemeral=True)
    cards=[item for item in ACTIVE_ITEMS if (card is None or item['id']==card) and (set is None or item['id'] in eco.SET_DEFINITIONS[set]['members'])]
    rarities=[rarity] if rarity else list(ADMIN_RARITIES if (admin or cloudy) else eco.RARITIES)
    forced_seasonal = frozenset(modifiers) & frozenset(SEASONAL_WINDOWS)
    eligible_cards = [item for item in cards if all(seasonal_image(item, key) for key in forced_seasonal)]
    if not any(item['weight']*eco.RARITIES[r]>0 for item in eligible_cards for r in rarities):
        reason = 'Set matching TALL/WIDE overlays in modifiers.json and card orientations in items.json.' if forced_seasonal else 'Check the rarity, card, and set filters.'
        return await interaction.response.send_message(f'No spawnable cards match these filters. {reason}',ephemeral=True)
    with db:
        db.execute('INSERT INTO spam_periods VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET until_time=excluded.until_time',(interaction.guild_id,time.time()+300))
        db.execute('INSERT INTO rigged_spam(guild,rarity,card,set_id,horizontal_flip,vertical_flip) VALUES(?,?,?,?,?,?) ON CONFLICT(guild) DO UPDATE SET rarity=excluded.rarity,card=excluded.card,set_id=excluded.set_id,horizontal_flip=excluded.horizontal_flip,vertical_flip=excluded.vertical_flip',(interaction.guild_id,rarity,card,set,None if horizontal_flip is None else int(horizontal_flip),None if vertical_flip is None else int(vertical_flip)))
        db.execute('UPDATE rigged_spam SET modifiers=? WHERE guild=?',(json.dumps(modifiers),interaction.guild_id))
        db.execute('UPDATE settings SET next_spawn=? WHERE guild=?',(time.time(),interaction.guild_id))
    description=', '.join(x for x in [f"rarity: {rarity}" if rarity else None,f"card: {item_info(card)['name']}" if card else None,f"set: {eco.SET_DEFINITIONS[set]['name']}" if set else None, f"horizontal flip: {horizontal_flip}" if horizontal_flip is not None else None, f"vertical flip: {vertical_flip}" if vertical_flip is not None else None] if x) or 'all cards and rarities'
    await interaction.response.send_message(f'Rigged spawn spam enabled for 5 minutes ({description}). A matching card spawns after each claim. An ending announcement will be sent.')

@spawnspamrigged.autocomplete('card')
async def rigged_card_autocomplete(interaction:discord.Interaction,current:str):
    matching=[item for item in ITEMS if current.casefold() in item['name'].casefold() or current.casefold() in item['id'].casefold()]
    return [app_commands.Choice(name=f"{item['name']} ({item['id']})"[:100],value=item['id']) for item in matching[:25]]

@spawnspamrigged.autocomplete('set')
async def rigged_set_autocomplete(interaction:discord.Interaction,current:str):
    return [app_commands.Choice(name=entry['name'][:100],value=key) for key,entry in eco.SET_DEFINITIONS.items() if current.casefold() in key.casefold() or current.casefold() in entry['name'].casefold()][:25]

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
