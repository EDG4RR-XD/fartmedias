# Fartmedias Discord card bot

Collect character cards, trade copies, earn and spend chudlets, track sets, and request drawing prompts. Collections, balances, profiles, bounties, and rewards are scoped to each Discord server. Card IDs identify individual copies and are allocated across the saved database.

This guide describes the current code. It replaces the older accumulated release notes.

## Setup and running

1. Create a Discord bot, enable Message Content Intent in the Developer Portal, and invite it with the `bot` and `applications.commands` scopes.
2. Install Python 3.12 or use the supplied Dockerfile.
3. Install dependencies with `pip install -r requirements.txt`.
4. Set `DISCORD_TOKEN` in your hosting environment or a local `.env` file. Keep the token private.
5. Optionally set `TEST_GUILD_ID` to sync slash commands to one test server. Without it, commands sync globally.
6. Set `DATA_DIR` to a persistent directory. The default is `./data`; the database is `DATA_DIR/flags.db`.
7. Start with `python bot.py`.
8. Run `/setup channel:#your-channel` to enable automatic spawning.

The spawn channel needs View Channel, Send Messages, Embed Links, and Read Message History. Give the bot Attach Files for transformed card images and backups, and permission to remove its spawn messages where needed.

For Railway, keep the existing persistent volume and point `DATA_DIR` at its mount path, for example `/data`. Update source files in your connected repository. The Dockerfile packages the configuration, gradient, and `overlays` directory. Local overlay art must be included in that directory to be copied by this Dockerfile.

Use a separate bot token, test server, and data directory for development. Schema updates happen automatically at startup; keep the existing database when updating.

## Spawning and claiming

`/setup channel` requires Manage Server. It saves the spawn channel and schedules the first spawn within roughly five seconds.

`/spawn` requires Manage Server and requests a random spawn in the configured channel. It does not replace an active spawn.

Type `Claim` in the spawn's channel to collect it. One spawn can be active per server. The normal schedule uses five-minute intervals, and ordinary spawns remain claimable for five minutes. The background loop checks about every five seconds, so timing is approximate. Claims during spawn spam make the next spawn eligible on the next loop pass.

Players can claim any number of cards consecutively in the same server. Chaining bonuses apply when successive claims match the same configured set.

Claim rewards:

- First discovery of a base card: 1,000 chudlets.
- First acquisition of each base-card/rarity combination: 150 chudlets, once per player per server.
- Each newly reached five-copy milestone for that base card: 1,000 chudlets. Previously rewarded milestones do not pay again.
- Owning all current base rarities of one card: 5,000 chudlets once per base card.
- Completing a configured named set: 10,000 chudlets once per set.

Milestones use currently held copies when a card is claimed. Discoveries and paid reward history are saved independently of current ownership.

## Cards, searches, and tracking

- `/collection [member] [page]`: list individual card copies and IDs; defaults to your collection.
- `/collectionlocked [member] [page]`: show only locked copies.
- `/collectionunlocked [member] [page]`: show only unlocked copies.
- `/view card_id`: show a card's image, owner, rarity, modifiers, and artist credit.
- `/collectionsearch [card] [rarity] [page]`: search your collection by card name or exact item ID, optionally filtering rarity.
- `/cardsearch [card] [rarity] [page]`: search collections in this server and show owners.
- `/list [page]`: show owned base cards above unowned cards, with rarity copy counts.
- `/itemtrack [page]`: show owned and missing collectible base cards with copy counts.
- `/raritytrack card`: show owned, missing, and retired rarities for one base card.
- `/settrack set [page]`: show owned and missing members of a named set.
- `/sets [page]`: show named-set progress and collect any unpaid completion bonuses.

Trackers use current ownership. Any rarity counts toward owning a base card or named-set member. Modifiers do not add required rarities. Tracking commands themselves do not award currency, except `/sets`, which checks unpaid set rewards.

Locked cards display a lock beside their IDs in collection/search indexing. Favourites display a heart. `/list` marks rarity groups containing locked or favourite copies. Holographic, Inverted, Glitch, and flipped variants display a sparkle in variant listings and spawns.

## Trading and gifting

`/trade member give_id want_id` offers one owned copy for another player's copy. The recipient must accept within two minutes. Ownership is checked again when accepted. Each participant receives a 100-chudlet trade bonus, plus any newly completed set rewards. Incoming traded cards are recorded as discovered.

`/givecard card_id user` transfers one card to another human player. It checks completion rewards for the recipient and records discovery. It does not give the 100-chudlet trade bonus.

Rarities, flips, ADMIN, and seasonal overlays stay with the card during transfers. Favourites and featured-profile selections clear when the selected copy changes ownership or is deleted.

## Card rarity unlock rewards

The first time a player acquires a particular base card in a particular base rarity, they earn 150 chudlets. Gold Seadweller and Gold Thistle are separate unlocks. Claims, card-shop purchases, incoming trades, gifts, and auction purchases qualify. The reward is recorded permanently per player per server. Selling, gifting away, or auctioning the last copy does not reset it. Buying or claiming that combination again gives no additional unlock reward. Modifiers, including flips and ADMIN, do not create separate rarity unlocks. Cancelling your own auction listing gives no reward.

Existing owned cards and auction escrow, saved card-purchase history, and recorded completed rarity sets seed the unlock history when this feature is first installed. These known combinations receive no retrospective payout. Old sold or transferred combinations without surviving rarity-specific records cannot be reconstructed from the previous database. Future unlock history survives restarts and is included in database backups.

## Selling and card ID reuse

- `/sell card_id`: sell a single copy after confirmation.
- `/sellall`: sell all unlocked copies after confirmation, including last copies.
- `/sellalltype card [rarity] [sell_last]`: sell matching unlocked cards. `sell_last` defaults to `False`, retaining a copy of each matching base-card/rarity pair. Set it to `True` to allow selling those last copies.
- `/selllock card_id`: toggle protection against `/sellall` and `/sellalltype`.

Sell locks apply to bulk sales. A locked card can still be sold individually, traded, gifted, or auctioned.

A card's NPC sell value is its configured rarity price, or `sell_value` fallback, plus **1,500 chudlets per modifier**. Horizontal flip, vertical flip, ADMIN, SPOOKY, HUNGRY, and FESTIVE each count separately. Base rarity is not a modifier.

Example: a base sell value of 150 with ADMIN and both flips sells for 4,650 chudlets before the Mass Credit bonus.

The Mass Credit sale bonus is `floor(Mass Credit / 10)` per card sold. It is added after modifier bonuses. Bulk sales apply both bonuses to each eligible copy. Single-sale confirmations last 60 seconds; bulk confirmations also expire after 60 seconds.

New claims and card-shop purchases fill the lowest vacant positive historical card ID before allocating a higher ID. This also fills older gaps. Auction escrow reserves its card IDs. An ID reused after a sale belongs to the newly generated copy, which can be a different card and rarity. Existing copies keep their IDs.

## Player market

`/ahsell card_id price` lists an owned copy at a player-chosen price. The card leaves the collection and stays in auction escrow with its ID and modifiers reserved.

`/ah [page]` shows listings. Select a listing to buy it or, as its seller, cancel and return the card. A purchase pays the seller, transfers the card, and checks the buyer's completion rewards. A seller cannot buy their own listing.

Auction prices are set manually; the 1,500-per-modifier NPC sale bonus does not change a listing's price. Auction listings are saved across restarts; interactive menus must be reopened afterward.

## Currency and shops

- `/balance`: show your chudlet balance.
- `/ctrade number user`: transfer your chudlets to another human player without a fee. Transfers cannot overdraw your balance.
- `/cardshop`: open the collectible-card shop. Choose a card and rarity, then Buy. Card choices paginate when needed. Only the menu owner can operate it; it expires after three minutes.
- `/shop`: buy configured art rewards.
- `/inventory [page]`: list purchased art rewards and redemption IDs.
- `/use-item item_id`: redeem a purchased art reward and create a receipt.
- `/receipt item_id`: display your saved redemption receipt again.

Card-shop copies cost five times the configured base-rarity sell value. They always have no modifiers. Modifier sale bonuses do not raise shop prices. Purchases count toward collection and completion tracking and may award unpaid completion bonuses. They can award the 150-chudlet card/rarity unlock bonus. They do not grant claim milestones, first-base-card claim rewards, or trade bonuses, and do not clear bounties or alter claim turns.

Current art rewards:

- FREE YCH: 15,000 chudlets.
- FREE ON BASE CUSTOM: 20,000 chudlets.
- FREE OFF BASE CUSTOM: 40,000 chudlets.
- FREE CHARACTER: 100,000 chudlets.
- HEADSHOT COMMISSION: 100,000 chudlets.
- FULLBODY COMMISSION: 250,000 chudlets.

Reward products and prices are configured in `SHOP` in `economy.py`. Card-shop payment receipts prevent the same interaction from charging twice. Art reward redemption is recorded so an item cannot be redeemed repeatedly.

## Drawing prompts and Mass Credit

`/draw` picks an image from `draw_pool.json` and asks for a coloured drawing for 7,500 chudlets. There is a ten-second per-user cooldown.

`/mass` picks from the same pool and asks for at least ten doodles at any quality, paying 10 chudlets per doodle after approval. Submit the completed ZIP to the owner. There is a ten-second per-user cooldown.

`/massown` asks for at least ten doodles of your own OC at any quality, paying 1 chudlet per doodle after approval. Submit a screenshot to the owner.

These commands post instructions only. Artwork approval and payments are manual. Administrators use `/cgive` for chudlets and `/mcgive` for Mass Credit. Mass Credit is a separate saved balance; every ten credits adds one chudlet per sold card.

## Profiles and favourites

`/profile [user]` shows chudlets, Mass Credit, rarest owned card, and enabled customizations.

`/profilecustomize [accentcolour] [footerimage] [favoritecard] [setcompletion] [raritycompletion]` changes any supplied options; omitted options retain their saved values.

- `accentcolour`: six-digit hex colour, for example `#64D545`. Use `reset` to clear it.
- `footerimage`: HTTP(S) image URL shown as the profile embed image. Use `reset` to clear it.
- `favoritecard`: an owned card ID to feature. Use `0` to clear it.
- `setcompletion`: show or hide currently completed named sets.
- `raritycompletion`: show or hide cards for which you currently own every base rarity.

`/favorite card_id` toggles a cosmetic heart on an owned card. It does not lock the card or change its price. Profile settings and favourites are server-specific.

## Bounties

`/bountyset card rarity` saves one active bounty per player per server. A new bounty replaces the previous one. Choose a spawnable card and rarity; zero-weight targets are rejected.

The exact pair gets 1.5 times its usual selection weight in that server. Multiple players targeting the same pair share one boost. Other rarities of that card are unaffected. Selection remains weighted, so a bounty does not guarantee a spawn.

When the pair appears, its bounty holders are pinged and it remains claimable for ten minutes. Anyone may claim it. A player's bounty clears only when that player successfully claims the matching pair. Gifts, trades, purchases, another player's claim, and expiry leave their bounty active. Claim-turn restrictions still apply. Matching rigged spawns receive the notification and lifetime behavior too.

## Rarities and modifiers

Current base rarity weights in `economy.py`:

```text
normal        50
silver        11
gold           7
emerald        6
diamond        5.5
obsidian       5
copper         4.5
ruby           4
sapphire       3.5
holographic    2
inverted       1.5
glitch         1
```

These are relative weights, not percentages. The current total is 101. Bounties and filters affect the final selection pool. Base rarity art uses `rarity_images` and falls back to `image_url`; the bot does not generate the base rarity artwork.

### Flips

Horizontal flip mirrors left and right, reflecting across the vertical Y-axis. Its random chance is 1% per spawn.

Vertical flip mirrors top and bottom, reflecting across the horizontal X-axis. Its random chance is 0.1% per spawn.

Rolls are independent and apply to every rarity. Both together have a 0.001% chance and rotate the final image 180 degrees. The bot performs the transforms; no separately flipped art is needed.

Rigged commands accept `horizontal_flip` and `vertical_flip`: `True` forces that flip, `False` prevents it, and omission keeps the normal random chance.

### ADMIN

ADMIN can be created only through `/spawnrigged` or `/spawnspamrigged`, using `admin:True`. It is never randomly rolled or sold by `/cardshop`.

It supports only `normal`, `holographic`, `glitch` (Glitched), and `inverted`. Other explicit rarity combinations are rejected. ADMIN spam without a selected rarity restricts the pool to those four.

The supplied `admin_gradient.png` is stretched to the rarity image and applied at full-opacity Multiply. ADMIN recolours existing pixels and preserves the source image's dimensions and alpha. It adds no opaque pixels outside the original artwork.

### Seasonal modifiers and card orientation

Set `"orientation": "TALL"` or `"orientation": "WIDE"` on each card object in `items.json`. The supplied entries have an empty orientation placeholder because their dimensions were not available for classification. Fill these before enabling seasonal artwork. Missing/empty orientation skips random seasonal modifiers and prevents forcing one; the bot never substitutes artwork for the opposite orientation.

There are three seasonal overlay modifiers, all compatible with every base rarity:

- SPOOKY: October 21 through October 31, inclusive, 10% per spawn.
- HUNGRY: November 16 through November 26, inclusive, 10% per spawn.
- FESTIVE: December 15 through December 25, inclusive, 10% per spawn.

Windows recur annually in `America/New_York` time. Set `MODIFIER_TIMEZONE` to another IANA timezone if needed. Random rolls apply to ordinary, test, and spam spawns, including rigged spam, only during the matching window. Each requires a configured image for the card's orientation. No random seasonal roll occurs outside its window.

Administrators can force any of the three year-round with `/spawnrigged spooky:True`, `hungry:True`, or `festive:True`. These Boolean options can stack and are not available on `/spawnspamrigged`. Omitted/False seasonal options do not force the modifier; normal in-season random rolls still apply.

Configure two image sources per modifier in `modifiers.json`:

```json
{
  "spooky": {
    "name": "SPOOKY",
    "TALL": "overlays/spooky-tall.png",
    "WIDE": "overlays/spooky-wide.png"
  },
  "hungry": {
    "name": "HUNGRY",
    "TALL": "overlays/hungry-tall.png",
    "WIDE": "overlays/hungry-wide.png"
  },
  "festive": {
    "name": "FESTIVE",
    "TALL": "overlays/festive-tall.png",
    "WIDE": "overlays/festive-wide.png"
  }
}
```

Use local paths relative to the bot folder or public HTTPS URLs. Put local assets in `overlays` for the Dockerfile to include them. The supplied sources are empty until you add your own art. Transparent PNGs allow the card to remain visible underneath. Restart after editing assets or configuration.

Artwork selection follows the card's explicit orientation even if a particular rarity image has different dimensions. Overlays are centered at native size; the canvas grows if needed, so these modifiers can add visible pixels. Prepare artwork at the intended card resolution.

Layer order: base rarity image, ADMIN Multiply, horizontal/vertical flips, SPOOKY, HUNGRY, FESTIVE. Seasonal art stays upright when the underlying card is flipped. Each seasonal modifier adds 1,500 chudlets to NPC sale value. Saved seasonal modifiers survive claims, transfers, auctions, restarts and backups; the calendar restricts creation, not later viewing.

The former tall/wide modifier slots and command options are removed. Startup removes those obsolete saved modifier keys from cards, spawns, listings, and rigged sessions while retaining the cards and all other modifiers. They no longer add sale bonuses. TALL and WIDE now select seasonal artwork through each card's orientation.

## Administrator commands

These commands require Administrator permission:

- `/cgive member amount`: add chudlets.
- `/ctake member amount`: remove chudlets, stopping at zero.
- `/mcgive member amount`: add Mass Credit.
- `/mctake member amount`: remove Mass Credit, stopping at zero.
- `/spawnrigged card [rarity] [horizontal_flip] [vertical_flip] [admin] [spooky] [hungry] [festive]`: spawn a selected card, defaulting to normal rarity. Explicitly selected archived/zero-weight cards can be spawned. An existing active spawn is not replaced.
- `/spawnspam`: enable five minutes of random spawns that become eligible immediately after each successful claim. Clears rigged session filters.
- `/spawnspamrigged [rarity] [card] [set] [horizontal_flip] [vertical_flip] [admin]`: enable five minutes of filtered spawn spam. Supplied filters must all match. Cards require positive spawn weight. Re-running it replaces the filters and restarts the five-minute timer.
- `/backup [scope]`: download a consistent saved-data snapshot.

Example ADMIN spawn:

```text
/spawnrigged card:Sea rarity:normal admin:True vertical_flip:True
```

Select the actual card from autocomplete. Spawn-spam sessions and rigged filters persist through restarts. Their expiry announcement is sent when the bot next processes the expired session. An already waiting spawn remains subject to its own expiry and claim rules.

Currency amounts are positive whole numbers up to 1,000,000,000. Currency adjustments are recorded and protected against duplicate processing of the same interaction.

## Configuration files

### items.json

A nonempty list of collectible definitions. Keep item IDs stable once cards are saved. Each entry supports:

```json
{
  "id": "example_card",
  "name": "Example Card",
  "image_url": "https://your-host.example/card.png",
  "ARTIST:": "Artist Name",
  "orientation": "TALL",
  "weight": 10,
  "sell_value": 100,
  "rarity_images": {
    "holographic": "https://your-host.example/card-holographic.png"
  },
  "rarity_sell_values": {
    "holographic": 3000
  },
  "sets": ["example_set"]
}
```

`ARTIST:` includes the colon in its key. All supplied definitions have this field, initially `Unknown`. Set the artist's name to show `art by Artist Name` on spawns and `/view`.

IDs and names are limited to 80 characters. Artwork URLs must be public HTTPS links. Weights must be finite and nonnegative; zero excludes the item from the regular random pool. Prices must be nonnegative integers. Rarity keys must match the current names in `economy.py`.

Current configuration controls prices for existing copies too. Archived definitions saved in the database can keep retired cards viewable. Restart after editing configuration.

### sets.json

Define set IDs and display names, then reference those IDs in each item's `sets` list:

```json
[
  {"id": "example_set", "name": "Example Set"}
]
```

Each named set requires all assigned base cards simultaneously, in any rarity. An empty set pays nothing. Keep set IDs stable because paid completion rewards are tracked by ID. A weight-zero card still assigned to a set remains a requirement.

### draw_pool.json

Shared by `/draw` and `/mass`. Entries are equally likely:

```json
[
  {"image_url": "https://your-host.example/oc.png"}
]
```

An empty list disables image prompts until you add art. This pool is separate from collectible card definitions.

### Other files

- `modifiers.json`: TALL/WIDE image paths or URLs for SPOOKY, HUNGRY, and FESTIVE. Seasonal chances and dates are defined in bot.py.
- `admin_gradient.png`: ADMIN Multiply gradient.
- `overlays/`: local custom overlay assets included by Docker.
- `economy.py`: base rarity weights, flip chances, art reward shop, saved-data operations, and reward logic.
- `bot.py`: Discord commands, spawning, rendering, and sale-price calculation.
- `requirements.txt`: Python dependencies.
- `Dockerfile`: container build and startup.

## Backups and restoration

`/backup` defaults to this server and requires Administrator permission. `scope:all` additionally requires ownership of the Discord application or membership in its owning team. The command has a 60-second per-user cooldown.

The ZIP contains `flags.db`, `user-data.json`, and `RESTORE.txt`. The database is the restorable state; JSON is readable inspection data. Snapshots include committed saved collections, balances, modifiers, auction listings, purchases, reward history, profiles, bounties, session settings, and archived card definitions.

Backup attachments are publicly visible in the channel where the command runs. Use a restricted channel when appropriate. Some validation errors elsewhere in the bot are private; do not assume all replies are private or all are public.

Backups exclude the token, `.env`, source/configuration files, downloaded image assets, and pending in-memory trade offers. Save project files and custom overlay art separately. A backup does not replace persistent storage.

To restore:

1. Download and retain a safety copy of the ZIP.
2. Stop the bot.
3. Back up the current database and any `flags.db-wal` / `flags.db-shm` files.
4. Extract the snapshot's `flags.db` into `DATA_DIR`, replacing the old database while the bot is stopped.
5. Archive or remove the old WAL/SHM sidecar files so they are not reused with the restored database.
6. Start the bot; schema migrations run automatically.

Use an all-server snapshot when replacing a multi-server database. Replacing it with a server-only snapshot removes other servers' records. Restore into the persistent volume on a container host.

## Validation

Run focused modifier, price, and flip checks with:

```bash
python -m unittest test_seasonal_modifiers test_modifiers test_flips -q
```

Nine focused checks passed for seasonal date boundaries and 10% rolls, orientation selection, final layer order, stacking, year-round rigged overrides, ADMIN rendering, artist credits and flips.

The repository also contains older tests. The original uploaded project and the modifier release both reported 10 failures and 15 errors across the pre-existing 59-test discovery suite, including outdated demo names, schema fixtures, and rarity expectations. Those legacy tests are not a clean release gate. Live Discord integration still needs a configured bot and server.


## Favourite thumbnail, BOUNCY and collection sorting

The owned card selected using `/profilecustomize favoritecard:<id>` appears in
`/profile` as the thumbnail, including its rarity image, flips and modifiers.
The footer image remains the large profile image. Selling or transferring the
featured card clears it as before.

BOUNCY renders the complete card as a transparent looping GIF at its original
resolution, adding 8% vertical travel without cropping. A cycle lasts one second.
It has an independent 2% spawn chance and works with every rarity and modifier.
Use `bouncy:true` with `/spawnrigged` or `/spawnspamrigged` to force it.

`/sort order:<autocomplete option>` saves a per-user, per-server collection order.
Use `/sortindex` to see all 27 numbered options: ascending and descending for
Card ID, Card name, Rarity, Sale value, Artist, Orientation, Copies of card type,
Copies of exact variant, Modifier count, Modifier names, Flip count, Locked,
and Favourite, plus saved Manual order.
Item ID sorting overlaps card name grouping; individual modifier/flip flags and
seasonal counts were removed in favour of modifier names/count and flip count.
Old saved modes that were removed fall back to Card ID ascending.

`/manualsort id:123` puts the first ID first. Repeat with each next ID.
Use `reset:true` with the first ID to begin a fresh order. Repeated IDs are
rejected. Unlisted and newly acquired cards follow in ascending ID order.
`/manualsortbefore id:123 target:456` moves 123 immediately before 456;
`/manualsortafter id:123 target:456` moves it immediately after 456.
Both cards must belong to you in this server. Moving a card starts from your
current displayed order and saves it as manual. `/sort order:manual` restores it
if you switched to another sort.

HYPER spins the complete rendered card clockwise once every two seconds as a
transparent looping GIF. The canvas accommodates every angle without shrinking
or cropping. Combined BOUNCY keeps its one-second bounce. HYPER has an independent
2% spawn chance on every rarity; force it with `hyper:true` on `/spawnrigged` or
`/spawnspamrigged`. Existing modifier compatibility rules still apply.

Descending places enabled flags first. Rarity descending places rarer cards
first. Card ID ascending is the default and breaks ties. Card IDs track their
numeric order; recycled IDs do not represent acquisition dates. Copy counts use
the entire owned collection. Sorting applies before pagination in `/collection`,
`/collectionlocked`, `/collectionunlocked`, and `/collectionsearch`, including
when another user views your collection. It changes display order only.

Restart the bot to apply database migrations and synchronize slash commands.

## Chaining, paid spawns and BLOOD MOON

- Consecutive claims by the same user from the same configured set build a server-wide chain. The first claim starts at x1. The second earns an additional 100 chudlets, the third 200, and so on. Other users' claims start their own chains. Cards outside a configured set reset the chain. Consecutive claims by the same user are permitted when they continue their set chain.
- Each chain level after x1 increases the weight of rarities above normal by 15%, capped at 3x their original weight. These boosted odds apply to the following random spawn in that server, and do not override forced rarities. Chain streaks survive bot restarts.
- `/paidspawn`: 1,000 chudlets for one random card in the configured spawn channel. The command refuses to charge if a card is already active and refunds failures.
- `/paidspawnspam`: 10,000 chudlets for five minutes of spawnspam. Buying during an existing session extends its end by five minutes; it resets rigged spam to random spawning.
- BLOOD MOON: 1% roll on every spawn, increased to 20% on October 31 in `MODIFIER_TIMEZONE` (defaults to America/New_York). Uses the bundled `blood_moon.png` via a full-card multiply blend. Stacks with other modifiers and flips. Administrators can force it with `blood_moon:true` in `/spawnrigged` or `/spawnspamrigged`.
