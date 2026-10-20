"""Synchronous SQLite operations. Call mutations inside `with db:` without awaits."""
import json

RARITIES = {
    'normal': 50,
    'silver': 11,
    'gold': 7,
    'emerald': 6,
    'diamond': 5.5,
    'obsidian': 5,
    'copper': 4.5,
    'ruby': 4,
    'sapphire': 3.5,
    'holographic': 2,
    'inverted': 1.5,
    'glitch': 1,
}
SET_DEFINITIONS = {}
SHOP = {
    'free_ych': ('FREE YCH', 15000),
    'on_base_custom': ('FREE ON BASE CUSTOM', 20000),
    'off_base_custom': ('FREE OFF BASE CUSTOM', 40000),
}


def migrate(db):
    for table in ('cards', 'spawns'):
        columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        if 'rarity' not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN rarity TEXT NOT NULL DEFAULT 'normal'")
    db.executescript('''
    CREATE TABLE IF NOT EXISTS card_purchases(request TEXT PRIMARY KEY, guild INTEGER, owner INTEGER, card_id INTEGER, item TEXT, rarity TEXT, price INTEGER, earned INTEGER, created_at REAL);
    CREATE TABLE IF NOT EXISTS set_definitions(id TEXT PRIMARY KEY, name TEXT, members TEXT);
    CREATE TABLE IF NOT EXISTS completed_card_sets(guild INTEGER, owner INTEGER, set_id TEXT, PRIMARY KEY(guild,owner,set_id));
    CREATE TABLE IF NOT EXISTS currency_transfers(request TEXT PRIMARY KEY, guild INTEGER, sender INTEGER, recipient INTEGER, amount INTEGER, created_at REAL);
    CREATE TABLE IF NOT EXISTS wallet_adjustments(request TEXT PRIMARY KEY, guild INTEGER, actor INTEGER, owner INTEGER, delta INTEGER, balance INTEGER, created_at REAL);
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
    reward += card_set_bonus(db,guild,owner)
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
        bonuses.append(100+set_bonus(db,guild,owner,item)+card_set_bonus(db,guild,owner))
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


def adjust_balance(db,guild,actor,owner,delta,request):
    import time
    old=db.execute('SELECT delta,balance FROM wallet_adjustments WHERE request=?',(str(request),)).fetchone()
    if old:
        return old['delta'],old['balance']
    current=balance(db,guild,owner)
    actual=max(delta,-current)
    if current+actual > 9000000000000000000:
        raise ValueError('Balance limit exceeded')
    final=current+actual
    db.execute('INSERT INTO wallets VALUES(?,?,?) ON CONFLICT(guild,owner) DO UPDATE SET amount=excluded.amount',(guild,owner,final))
    db.execute('INSERT INTO wallet_adjustments VALUES(?,?,?,?,?,?,?)',(str(request),guild,actor,owner,actual,final,time.time()))
    return actual,final


def transfer(db,guild,sender,recipient,amount,request):
    import time
    if isinstance(amount,bool) or not isinstance(amount,int) or amount <= 0 or sender == recipient:
        return False
    previous=db.execute('SELECT * FROM currency_transfers WHERE request=?',(str(request),)).fetchone()
    if previous:
        return (previous['guild'],previous['sender'],previous['recipient'],previous['amount']) == (guild,sender,recipient,amount)
    if balance(db,guild,recipient)+amount > 9000000000000000000:
        return False
    if not db.execute('UPDATE wallets SET amount=amount-? WHERE guild=? AND owner=? AND amount>=?',(amount,guild,sender,amount)).rowcount:
        return False
    credit(db,guild,recipient,amount)
    db.execute('INSERT INTO currency_transfers VALUES(?,?,?,?,?,?)',(str(request),guild,sender,recipient,amount,time.time()))
    return True


def configure_sets(db,definitions,items):
    global SET_DEFINITIONS
    if not isinstance(definitions,list):
        raise ValueError('sets.json must contain a list.')
    configured={}
    for entry in definitions:
        if not isinstance(entry,dict) or any(not isinstance(entry.get(k),str) or not entry[k].strip() or len(entry[k])>80 for k in ('id','name')):
            raise ValueError('Every set needs id and name strings up to 80 characters.')
        if entry['id'] in configured:
            raise ValueError('Duplicate set ID.')
        configured[entry['id']]={'name':entry['name'],'members':set()}
    for item in items:
        tags=item.get('sets',[])
        if not isinstance(tags,list) or any(not isinstance(tag,str) or tag not in configured for tag in tags):
            raise ValueError('Each item sets field must be a list of IDs defined in sets.json.')
        for tag in tags:
            configured[tag]['members'].add(item['id'])
    SET_DEFINITIONS=configured
    with db:
        # Persist the current definitions for inspection in exports; history is separate.
        db.execute('DELETE FROM set_definitions')
        db.executemany('INSERT INTO set_definitions VALUES(?,?,?)',[(key,value['name'],json.dumps(sorted(value['members']))) for key,value in configured.items()])


def card_set_bonus(db,guild,owner):
    owned={r['country'] for r in db.execute('SELECT DISTINCT country FROM cards WHERE guild=? AND owner=?',(guild,owner))}
    earned=0
    for key,definition in SET_DEFINITIONS.items():
        if definition['members'] and definition['members'] <= owned:
            if db.execute('INSERT OR IGNORE INTO completed_card_sets VALUES(?,?,?)',(guild,owner,key)).rowcount:
                earned += 10000
    if earned:
        credit(db,guild,owner,earned)
    return earned


def buy_card(db,guild,owner,item,rarity,price,request):
    import time
    if isinstance(price,bool) or not isinstance(price,int) or price<0 or rarity not in RARITIES:
        raise ValueError('Invalid purchase price or rarity')
    previous=db.execute('SELECT * FROM card_purchases WHERE request=?',(str(request),)).fetchone()
    if previous:
        if (previous['guild'],previous['owner'],previous['item'],previous['rarity'],previous['price'])!=(guild,owner,item,rarity,price):
            return None
        return previous['card_id'],previous['earned']
    credit(db,guild,owner,0)
    if not db.execute('UPDATE wallets SET amount=amount-? WHERE guild=? AND owner=? AND amount>=?',(price,guild,owner,price)).rowcount:
        return None
    card_id=db.execute('INSERT INTO cards(guild,owner,country,rarity) VALUES(?,?,?,?)',(guild,owner,item,rarity)).lastrowid
    db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)',(guild,owner,item))
    earned=set_bonus(db,guild,owner,item)+card_set_bonus(db,guild,owner)
    db.execute('INSERT INTO card_purchases VALUES(?,?,?,?,?,?,?,?,?)',(str(request),guild,owner,card_id,item,rarity,price,earned,time.time()))
    return card_id,earned
