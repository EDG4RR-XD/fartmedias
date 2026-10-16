"""Synchronous SQLite operations. Call mutations inside `with db:` without awaits."""
import json

RARITIES = {'normal': 79, 'silver': 10, 'gold': 5, 'emerald': 3, 'diamond': 2, 'obsidian': 1}
SHOP = {
    'free_ych': ('FREE YCH', 5000),
    'on_base_custom': ('FREE ON BASE CUSTOM', 10000),
    'off_base_custom': ('FREE OFF BASE CUSTOM', 30000),
}


def migrate(db):
    for table in ('cards', 'spawns'):
        columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        if 'rarity' not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN rarity TEXT NOT NULL DEFAULT 'normal'")
    db.executescript('''
    CREATE TABLE IF NOT EXISTS wallets(guild INTEGER, owner INTEGER, amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0), PRIMARY KEY(guild,owner));
    CREATE TABLE IF NOT EXISTS discoveries(guild INTEGER, owner INTEGER, item TEXT, PRIMARY KEY(guild,owner,item));
    CREATE TABLE IF NOT EXISTS milestones(guild INTEGER, owner INTEGER, item TEXT, level INTEGER DEFAULT 0, PRIMARY KEY(guild,owner,item));
    CREATE TABLE IF NOT EXISTS completed_sets(guild INTEGER, owner INTEGER, item TEXT, PRIMARY KEY(guild,owner,item));
    CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER, owner INTEGER, product TEXT, used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS item_extras(id TEXT PRIMARY KEY, definition TEXT);
    ''')
    # Old inventory establishes history, without a surprise retrospective payout.
    if not db.execute("SELECT 1 FROM metadata WHERE key='economy_initialized'").fetchone():
        with db:
            db.execute('INSERT OR IGNORE INTO discoveries SELECT DISTINCT guild,owner,country FROM cards')
            db.execute('INSERT OR IGNORE INTO milestones SELECT guild,owner,country,COUNT(*)/5 FROM cards GROUP BY guild,owner,country')
            db.execute("INSERT INTO metadata VALUES('economy_initialized','1')")


def balance(db, guild, owner):
    row = db.execute('SELECT amount FROM wallets WHERE guild=? AND owner=?', (guild,owner)).fetchone()
    return row['amount'] if row else 0


def credit(db, guild, owner, amount):
    db.execute('INSERT INTO wallets VALUES(?,?,?) ON CONFLICT(guild,owner) DO UPDATE SET amount=amount+excluded.amount', (guild,owner,amount))


def set_bonus(db, guild, owner, item):
    rarities = {r['rarity'] for r in db.execute('SELECT DISTINCT rarity FROM cards WHERE guild=? AND owner=? AND country=?', (guild,owner,item))}
    if set(RARITIES) <= rarities:
        fresh = db.execute('INSERT OR IGNORE INTO completed_sets VALUES(?,?,?)', (guild,owner,item)).rowcount
        if fresh:
            credit(db,guild,owner,5000)
            return 5000
    return 0


def award_claim(db, guild, owner, item, rarity):
    card_id = db.execute('INSERT INTO cards(guild,owner,country,rarity) VALUES(?,?,?,?)', (guild,owner,item,rarity)).lastrowid
    reward = 0
    if db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)', (guild,owner,item)).rowcount:
        reward += 1000
    count = db.execute('SELECT COUNT(*) FROM cards WHERE guild=? AND owner=? AND country=?', (guild,owner,item)).fetchone()[0]
    old = db.execute('SELECT level FROM milestones WHERE guild=? AND owner=? AND item=?', (guild,owner,item)).fetchone()
    previous = old['level'] if old else 0
    level = count // 5
    if count % 5 == 0 and level > previous:
        reward += 1000
        db.execute('INSERT INTO milestones VALUES(?,?,?,?) ON CONFLICT(guild,owner,item) DO UPDATE SET level=excluded.level', (guild,owner,item,level))
    credit(db,guild,owner,reward)
    reward += set_bonus(db,guild,owner,item)
    return card_id,reward


def exchange(db,guild,sender,target,give,want):
    a=db.execute('SELECT * FROM cards WHERE guild=? AND id=? AND owner=?',(guild,give,sender)).fetchone()
    b=db.execute('SELECT * FROM cards WHERE guild=? AND id=? AND owner=?',(guild,want,target)).fetchone()
    if sender==target or not a or not b:
        return None
    db.execute('UPDATE cards SET owner=? WHERE id=?',(target,give))
    db.execute('UPDATE cards SET owner=? WHERE id=?',(sender,want))
    bonuses=[]
    for owner,item in ((sender,b['country']),(target,a['country'])):
        # Trade discoveries prevent later selling/reclaiming as a "new" base card.
        db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)',(guild,owner,item))
        credit(db,guild,owner,100)
        bonuses.append(100+set_bonus(db,guild,owner,item))
    return bonuses


def sell(db,guild,owner,card_id,price):
    if price < 0:
        raise ValueError('Price cannot be negative')
    if not db.execute('DELETE FROM cards WHERE guild=? AND owner=? AND id=?',(guild,owner,card_id)).rowcount:
        return False
    credit(db,guild,owner,price)
    return True


def buy(db,guild,owner,product):
    price=SHOP[product][1]
    if not db.execute('UPDATE wallets SET amount=amount-? WHERE guild=? AND owner=? AND amount>=?',(price,guild,owner,price)).rowcount:
        return None
    return db.execute('INSERT INTO purchases(guild,owner,product) VALUES(?,?,?)',(guild,owner,product)).lastrowid


def redeem(db,guild,owner,purchase_id):
    row=db.execute('SELECT * FROM purchases WHERE id=? AND guild=? AND owner=? AND used=0',(purchase_id,guild,owner)).fetchone()
    if not row:
        return None
    db.execute('UPDATE purchases SET used=1 WHERE id=? AND used=0',(purchase_id,))
    return row['product']
