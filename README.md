## Latest update: /cardshop and character-family balancing

Update bot.py, economy.py and items.json from this archive in your existing GitHub repository, then deploy the latest commit. This release is based on your uploaded fartmedias-main (2).zip. It retains your card IDs, images, sets, drawing pool, rarity probabilities and shop art reward definitions. Do not change DATA_DIR or remove the existing volume. Download /backup before deployment. No requirements.txt or Dockerfile changes are needed.

/cardshop opens a PUBLIC menu with dropdown 1 for Card, dropdown 2 for Rarity, a Buy button, and Previous/Next card-page buttons. Your 29 cards span two dropdown pages because Discord limits each dropdown to 25 options. The Buy button stays disabled until both choices are selected. The preview shows the chosen variant image, sell price, purchase price and current balance. Each copy costs exactly five times its current effective sell value, including rarity-specific prices. Clicking Buy charges chudlets and gives one card copy. Other users can see the menu but only its owner can use its controls; others must open their own /cardshop. After a successful purchase the menu closes. Open /cardshop again to buy another copy. The menu expires after three minutes of inactivity or a bot restart.

Purchased cards appear immediately in /collection, /view, /itemtrack, /settrack, /raritytrack and /list. They count toward completion. Completing a named set pays the existing one-time 10,000-chudlet bonus; completing all rarities of a base card pays the existing one-time 5000-chudlet bonus. Those payouts occur inside the purchase transaction. A previously paid reward does not pay again. Purchases record the base card as discovered and do not award first-claim, five-copy claim or card-trade bonuses. They do not reset the consecutive-claim restriction or clear a bounty. The shop sells all cards currently defined in items.json in all currently configured rarities, including weight-0 rarities/cards; old archived definitions removed from items.json are not in the menu.

Payment, new card, discovery records, completion rewards and purchase receipt are committed atomically. Insufficient funds add no card. The same interaction cannot charge twice. A new card_purchases table stores transaction IDs, player/server, card ID, price, rarity and earned completion rewards and is included in database backups. If Discord cannot display the receipt after the transaction commits, /collection and /balance still show the saved result.

Balancing changes apply to ALL base designs/variants of each named character family:

- All four Seadweller entries: base and every rarity sell price ×3. Spawn weights unchanged.
- All four Thistle entries: base and every rarity sell price ×0.5; spawn weights 10 to 20.
- All five Tangelo entries: base and every rarity sell price ×2; each spawn weight 10 to 8.
- All five Deli/Green Delicious entries: spawn weights 10 to 20; sale prices unchanged.
- Gamble and Sophie entries: unchanged.

For example, normal Seadweller variants now sell for 150 and cost 750; normal Thistle sells for 25 and costs 125; normal Tangelo sells for 100 and costs 500. The base sell_value fallback for the first Seadweller entry was 10 in your file and is now 30; its explicit normal rarity override was 50 and is now 150. These multipliers were applied once to the supplied configuration, not on every bot startup. Prices change for existing owned cards too because sales use the current configuration. Weights are relative, so doubling a weight increases relative frequency rather than guaranteeing exactly twice the final normalized percentage.

Validation: eight card-shop tests plus sixteen existing tracking/bounty checks passed (24 focused checks). They cover menu selection/pagination/ownership, 5× pricing, insufficient funds, duplicate interactions, rollback, saved card/rarity ownership and one-time set/rarity completion payouts. All 29 definitions were checked against the original for the requested price/weight changes and preserved IDs/images/set memberships. Live Discord verification requires your deployed app.

## Latest update: /bountyset card rarity

Example: /bountyset card:Seadweller rarity:gold. Use autocomplete to select a card and choose its rarity. One active bounty is saved per user per server. Setting another bounty replaces the previous one. It costs no chudlets and creates no currency rewards. Cards and rarities with zero random spawn weight cannot be selected.

The selected EXACT card-and-rarity combination receives 1.5 times its usual random selection weight in this server. The base card's other rarities do not get that bonus. Multiple players targeting the same pair share a single 1.5 multiplier, rather than stacking. Different pairs can each be boosted. Like all weighted choices, results are normalized against the other weights; a 50% weight boost is not an additional 50 percentage points or a guaranteed appearance.

When the target appears, the bot explicitly pings every active bounty holder for that card and rarity. Other user/role/everyone mentions remain disabled by default. A matching spawn remains claimable for 10 minutes and is then removed from Discord; ordinary spawns remain for 5 minutes. There is still one active spawn per server, so a waiting 10-minute bounty spawn delays the next spawn rather than being replaced halfway through. The cleanup loop checks about every 5 seconds. If Discord denies message access/removal, the expired database entry is still removed and cannot be claimed.

Anyone can claim a bounty spawn. The user's bounty clears only when that user successfully CLAIMS the exact target; an expired target, another player's claim, gifts or trades do not clear it. It stays active and continues boosting until then or until replaced. A bounty does not bypass the consecutive-claim restriction. Bounties apply to matching /spawnrigged spawns for notification and lifetime as well, but the chosen rigged pair remains fixed.

Bounty state is saved in the bounties database table and included in /backup snapshots. It survives restart provided the database is on persistent storage. Matching players are notified in batches when necessary to respect message/mention limits.

To install, replace ONLY bot.py in your GitHub repository and deploy the latest commit. Keep the existing database, volume, DATA_DIR, economy.py and customized JSON files. No new dependencies or Dockerfile changes are needed. Download /backup before deploying. Previous tracking features and public replies remain enabled.

Seven new bounty tests and the nine tracking/claim tests passed (16 focused checks): exact-pair weights, non-stacking and server isolation, intentional user mentions, 10/5-minute lifetimes, replacing bounties, successful-owner-only clearing, claim restrictions, expiry cleanup and database persistence. Live Discord notification delivery depends on your server permissions and members' notification settings.

## Latest update: tracking, alternating claims and public replies

Update bot.py in GitHub with this release, then deploy the latest commit. Your economy.py, custom items.json, sets.json, draw_pool.json, rarity odds, prices and existing database are preserved. No additional dependency or Dockerfile change is required. Keep the existing persistent volume and DATA_DIR; download a backup before redeploying. If your bot has no persistent storage, a code deployment can still erase its local data.

/settrack set:ocean shows your owned and missing cards for that configured set, distinct-card completion (for example 3/5), counts for each owned card and total copies. Choose the set from autocomplete. Any rarity counts toward set ownership. Add page:2 for longer sets. This command only reads progress; use /sets to collect any completion rewards on already owned sets.

/raritytrack card:Seadweller shows the current variants you own, how many copies of each, missing variants, and the number of distinct owned variants. Choose a card from autocomplete. Retired variants are listed separately and do not count toward the current rarity target. This tracker gives no chudlets.

/itemtrack shows every known collectible card you own and are missing, with copy counts and a distinct-item completion total. Owned items are above missing items. Any rarity counts as owning the base card. Add page:2 etc. for more results. This tracks collectible cards, not purchased shop rewards; /inventory still lists shop purchases. Retired saved card definitions can appear in the tracker too. All trackers reflect this server and the user running the command at that moment; they do not auto-refresh old messages.

After someone successfully claims a spawn, they must wait for another player to successfully claim in the SAME server before claiming again. The exact rejection is:

Users can't claim two cards in a row!

Blocked attempts leave the spawn available and grant no cards or currency. Failed attempts, expired spawns, trades, gifts, sales and new spawns do not reset the last successful claimant. Another server has its own claimant. This applies to ordinary and manually rigged spawns. The last claimant is stored in a new claim_turns table and survives restart/backup/restore. The first successful claim after installing this release establishes the initial claimant; old ownership is not guessed as claim history.

All previously ephemeral command replies, errors, confirmations and followups are now PUBLIC in the channel. This includes balances, inventories, collection views, shop purchases, admin currency adjustments and /backup attachments. The backup ZIP can therefore be downloaded by channel viewers; use an admin-only channel for database exports if you want restricted access. All-server export still requires the bot application owner/team, and administrative commands still require Administrator permission. Public buttons and shop menus still only accept actions from the original player or designated trade recipient. Previously sent ephemeral messages do not change retroactively.

Run the focused checks with:

    python -m unittest test_tracking.py -v

Nine focused tests passed: tracker ownership/copy counts and server isolation, missing variants, retired variant display, invalid choices, autocomplete, public replies, exact claim error/no reward, concurrent claims, and persistence after database reopen. Live Discord checks still require your bot token/server. test_bot.py is retained from the uploaded project and may contain old demo names/rarity expectations; the focused tests use isolated test data.

The earlier README sections below describe previous releases. Their references to private/ephemeral replies are superseded by this update.

# Image collector bot with chudlets

## Latest update: currency transfers, card sets, drawing prompts, 10 variants

Upload bot.py, economy.py, Dockerfile, sets.json and draw_pool.json from this release. Keep your customized items.json. If you already have sets.json or draw_pool.json from this release, preserve your custom contents too. The supplied two new JSON files are empty lists so no fabricated card sets or OC prompts are enabled. Keep your volume and DATA_DIR unchanged. Run /backup and download it before deploying. If you still have no persistent storage, back up the currently running deployment before replacing it.

### /ctrade number user

Example: /ctrade number:500 user:@Alex transfers 500 of YOUR chudlets to Alex in this server. Any human player can use it; no admin permission is required. It cannot transfer to yourself or a bot, send zero/negative amounts, or overdraw your account. Transfers have no fee, do not create new currency, and do not award the card-trade bonus. A repeated Discord interaction cannot debit twice. Both sides and the transfer log are saved atomically. Amount limit: 1–1,000,000,000.

### Card sets, 10,000 chudlets per completed set

Edit sets.json to define your set IDs and display names:

```json
[
  {"id": "ocean", "name": "Ocean Set"},
  {"id": "halloween", "name": "Halloween Set"}
]
```

Then edit each relevant object in YOUR existing items.json. Add a sets field next to weight or sell_value, preserving the commas:

```json
"sets": ["ocean"]
```

To attach one card to two sets:

```json
"sets": ["ocean", "halloween"]
```

For example, add sets:["ocean"] to both your existing seadweller and mermaid entries to make the Ocean Set require those two base cards. Use the actual IDs of your own sets and cards. Every set tag must be defined in sets.json. A card without the sets field belongs to no sets. An empty set gives no reward. A retired card with weight 0 remains a requirement if still assigned; remove its set tag if you want it excluded.

Players must own at least one copy of EVERY assigned base card simultaneously. Any rarity counts. Claims and incoming card trades check completion automatically. /sets shows owned/missing counts and grants any previously unclaimed completion rewards, including sets added after cards were collected. Each set pays 10,000 chudlets once per player per server. Selling/trading away cards and rebuilding a set does not pay again. Multiple completed sets can pay together. Keep set IDs stable; changing an ID creates a new reward identity. Editing membership does not reset already paid rewards.

This is separate from the 5000-chudlet bonus for collecting every rarity of one base card.

### /draw, a separate random pool with no automatic payout

Edit draw_pool.json like this, replacing the sample links with your real direct HTTPS image URLs:

```json
[
  {"image_url": "https://your-image-host.example/oc-one.png"},
  {"image_url": "https://your-image-host.example/oc-two.png"}
]
```

Each entry is equally likely. Entries may repeat on later rolls. /draw posts the image with this exact text:

Draw this OC (Coloured, any) for 7500 Chudlets!

Using /draw never grants chudlets, awards no card, and charges nothing. A 10-second per-user cooldown limits repeated prompts. You approve completed artwork and award 7500 manually with /cgive. The empty default pool gives an explanatory message until you add OC links. Draw images are independent of collectible card images.

### Ten added variants and their odds

The new mutually exclusive percentages are:

normal 74%; silver 10%; gold 5%; emerald 3%; diamond 2%; obsidian 1%; bronze 2%; copper 1%; rose_quartz 0.5%; ruby 0.4%; sapphire 0.3%; amethyst 0.25%; pearl 0.2%; holographic 0.15%; rainbow 0.12%; glitch 0.08%.

They total 100%. These are defaults chosen for the new variants. You can edit RARITIES in economy.py to change the weights. The values are relative weights; when the sum is 100 they equal percentages.

The new keys are bronze, copper, rose_quartz, ruby, sapphire, amethyst, pearl, holographic, rainbow and glitch. Add them to each card's existing rarity_images object to supply separate artwork:

```json
"bronze": "https://your-image-host.example/card-bronze.png",
"copper": "https://your-image-host.example/card-copper.png",
"rose_quartz": "https://your-image-host.example/card-rose-quartz.png",
"ruby": "https://your-image-host.example/card-ruby.png",
"sapphire": "https://your-image-host.example/card-sapphire.png",
"amethyst": "https://your-image-host.example/card-amethyst.png",
"pearl": "https://your-image-host.example/card-pearl.png",
"holographic": "https://your-image-host.example/card-holographic.png",
"rainbow": "https://your-image-host.example/card-rainbow.png",
"glitch": "https://your-image-host.example/card-glitch.png"
```

This snippet is fields to merge inside rarity_images, not a complete replacement items.json. Keep your normal/silver/gold/emerald/diamond/obsidian links. Missing variant artwork falls back to image_url. Artwork is not generated by this update. Add optional new keys to rarity_sell_values to give them different sale prices; otherwise sell_value applies.

/list, /collection, /view, /sell, /spawnrigged and card trades recognize all 16 variants. The 5000-chudlet rarity completion bonus now requires all 16. Players already paid for the old six-variant set keep their reward and are not paid again for the same base card. Currency transfers and named sets use separate tables; your existing saved data is migrated without resetting it.

New local tests cover transfer conservation, overdraft rejection, duplicate transfer handling, server isolation, set claims/trades/recompletion, existing-card completion, empty and invalid sets, draw behavior with zero currency payout, rarity odds and image overrides. All 21 local tests passed. Live Discord verification still requires your deployed bot.

## New admin tools and downloadable data

Upload the updated bot.py and economy.py to your existing repository. Keep your own items.json, volume, DATA_DIR and Discord token. Dockerfile and requirements.txt are unchanged from the previous release. The existing database gains an audit table without removing saved data. Redeploy and allow slash commands to sync. If TEST_GUILD_ID is set, try these commands in that server.

IMPORTANT: If your current bot still has no persistent volume, deploying this update can erase its current local database before the new /backup command is available. Download a snapshot from the current deployment first, using the earlier terminal backup procedure. This update cannot recover a database that was already lost. /backup creates downloadable snapshots; it does not replace persistent storage.

All new commands require Discord Administrator permission in this server. Both command visibility defaults and runtime checks enforce this.

- /cgive member:@Player amount:1000 adds 1000 chudlets to that player's balance in this server.
- /ctake member:@Player amount:500 removes up to 500. If they have less, their balance becomes zero; it never becomes negative. Amounts must be whole numbers from 1 to 1,000,000,000. Bot accounts cannot receive adjustments. Adjustments record the admin, player, amount, resulting balance, server and time in the database. Retrying the same Discord interaction cannot apply an adjustment twice.
- /spawnrigged card:moon_cat rarity:gold spawns the selected card and rarity in your configured spawn channel. Select the card using autocomplete; normal is the default rarity. Cards with weight 0 can also be manually spawned. It refuses to replace an active spawn. Anyone can Claim it, and the usual rewards apply. The next scheduled spawn is five minutes later.
- /backup defaults to This server. It produces an ephemeral attachment visible to the requesting admin. Click the ZIP attachment to download. It contains flags.db, user-data.json and RESTORE.txt. It captures collections, chudlets, purchases/redemptions, rewards history, admin adjustments, server spawn settings and saved card definitions. It does not include the bot token, .env, downloaded artwork, or pending in-memory trade offers. Images are saved as URLs.
- /backup scope:All servers additionally checks that the requester owns the Discord application or belongs to its owning team. An ordinary server admin cannot download another server's users. Application ownership is checked through Discord, not through a guessed username. The requester must also have Administrator permission in the server where they invoke the command.

Backups are consistent SQLite snapshots, including committed WAL changes. They are created in temporary storage and attached privately. The command has a 60-second per-user cooldown. If the ZIP exceeds Discord's upload limit, it reports that limitation; use your host's file-transfer tools instead. Download promptly and keep a private copy. Data after the snapshot time is not included. Do not commit user-data ZIPs or flags.db to GitHub.

Restoring: pause the bot first, keep a copy of its current database, extract flags.db from the ZIP, and copy it into DATA_DIR as flags.db while the bot is stopped. Use the established volume restore workflow with the temporary sleep infinity start command. A single-server export replaces the destination with only that server's data; use All servers for a full multi-server restore. Restoring an older snapshot rolls balances and redemptions back to that point. Never overwrite an active SQLite database.

The tests now include permissions, capped and idempotent chudlet adjustments, chosen spawns, server isolation, ZIP contents, integrity checks, WAL snapshot restoration, and denial of full exports to non-owners. 21 local tests passed. Live Discord tests still require your token/server.

## Update your existing bot

1. Extract this ZIP.
2. In your existing GitHub repository choose Add file > Upload files.
3. Replace bot.py, Dockerfile, requirements.txt and README.md, and add economy.py. Keep these at the repository root. test_bot.py is optional to upload.
4. KEEP YOUR CUSTOM items.json. Add the new fields shown below to your existing entries. The items.json inside this ZIP is only a two-card demo. Replacing your customized file with it would change your spawn pool.
5. Commit changes. In Railway open your bot service, press Ctrl+K (Cmd+K on Mac), and choose Deploy Latest Commit if autodeploy has not started.
6. Keep your existing DISCORD_TOKEN, DATA_DIR=/data, TEST_GUILD_ID if set, and volume mounted at /data. Message Content Intent must remain enabled in the Discord Developer Portal. Run only one bot replica.
7. Check deployment logs. Old cards are automatically migrated as normal rarity. The flags.db filename stays unchanged. Existing collections and trades remain compatible; pending offers from before restart expire.

No chudlets are paid retrospectively for pre-update cards. Existing holdings seed first-card history and previous five-copy milestones. Your next spawn is scheduled no later than five minutes after this update, unless an older active spawn is still waiting to expire.

## Commands

/list [page]: shows all known base cards. Owned cards appear in the upper row/section, missing cards below it. Each owned card shows counts for all 16 rarities. Use page:2, page:3 etc. when needed. Each page shows up to three owned and three missing cards.
/collection [member] [page]: individual card IDs with rarity labels.
/view card_id: displays that card's rarity-specific image.
/balance: your chudlets.
/sell card_id: quotes that card's price and asks for confirmation. Selling removes that copy and credits chudlets. It does not transfer the card to another player.
/trade member give_id want_id: offer a one-for-one exchange. The other player must accept. Both get 100 chudlets on successful completion.
/shop: menu containing FREE YCH (5000), FREE ON BASE CUSTOM (10000), FREE OFF BASE CUSTOM (30000). Selecting a reward immediately buys one copy if you can afford it.
/inventory [page]: purchased items and their IDs, including redeemed entries.
/use-item item_id: consumes one purchased item and posts a receipt asking the player to ping @jonathan.sims with their claim. Use the purchase ID from /inventory, not a collectible card ID.
/receipt item_id: redisplays your saved redemption receipt without consuming another item.
/setup channel: choose the spawn channel, Manage Server required.
/spawn: manually test one spawn, Manage Server required; refuses while another is active.

The receipt stores a unique purchase number, server and owner. It does not automatically fulfill artwork or send a DM. The @jonathan.sims text prompts the player to select/ping that account themselves; no account ID is guessed. If posting a redeemed receipt fails, use /receipt to recover it.

## Claiming and chudlet rewards

Send the entire message Claim in the spawn channel. claim and CLAIM also work. First valid claim wins. Bots and webhooks cannot collect. Each spawn lasts five minutes. A replacement appears every five minutes, with a five-second scheduling check and possible network delays. Offline time is not backfilled.

- First discovery of a base card: 1000 chudlets. A new rarity of an already discovered base card is a duplicate for this particular bonus. Selling or trading it away does not reset discovery history. Acquiring a card through trade records it as discovered but awards only the trade reward, not the first-claim reward.
- Successful trade: 100 chudlets to each participant. Failed, expired or replayed acceptances do not pay. Repeated legitimate trades each pay, as configured by this rule.
- Five-copy milestone: 1000 when a claim brings current holdings of that base card to 5, 10, 15 etc. All rarities count toward the same total. Each milestone pays at most once per player and base card. Selling/trading copies away and returning to a previously paid milestone does not pay again. Trades never trigger this reward. If a trade brings you to five and the next claim is your sixth, no five-copy payout occurs.
- Full rarity set: 5000 once per player and base card when they own every one of the 16 configured rarities together. A claim or incoming trade can complete the set. Selling and rebuilding the set does not repeat the payout.

Rewards can stack. All balances, inventories, history and collections are per server. Selling cards never removes earned chudlets or prior discovery records. Keep volume backups enabled on your host.

## Rarity rolls

A base card is chosen using its weight. A separate rarity roll then chooses the 16 rarity weights listed at the top of this README. These are mutually exclusive outcomes totaling 100%. All 16 variants share one base card ID but each collected copy stores its own rarity.

## Customize cards, sale prices and images

Edit items.json in GitHub using the pencil button. Keep your existing IDs unchanged after people collect them. Here is a complete example for ONE card; replace every example URL with your actual direct HTTPS image link:

```json
[
  {
    "id": "moon_cat",
    "name": "Moon Cat",
    "image_url": "https://your-image-host.example/moon-cat-normal.png",
    "weight": 10,
    "sell_value": 100,
    "rarity_sell_values": {
      "silver": 200,
      "gold": 400,
      "emerald": 600,
      "diamond": 800,
      "obsidian": 1000
    },
    "rarity_images": {
      "normal": "https://your-image-host.example/moon-cat-normal.png",
      "silver": "https://your-image-host.example/moon-cat-silver.png",
      "gold": "https://your-image-host.example/moon-cat-gold.png",
      "emerald": "https://your-image-host.example/moon-cat-emerald.png",
      "diamond": "https://your-image-host.example/moon-cat-diamond.png",
      "obsidian": "https://your-image-host.example/moon-cat-obsidian.png"
    }
  }
]
```

id is permanent; name is displayed. image_url is the default/normal image. Upload images to a host such as File Garden and use URLs that display the image itself without a login. Discord attachment URLs can expire.

weight controls base-card spawn frequency relative to other entries. Set weight to 0 to retire a card. Its old copies remain viewable; /list includes retired cards too. At least one item must have a positive weight.

sell_value is your base sale price in chudlets, used for normal cards and rarities without a specific override. rarity_sell_values optionally overrides it per rarity. Prices are nonnegative integers. Example: sell_value:250 and a gold override of 1000 means a normal copy sells for 250 and a gold copy for 1000. Edit these values to choose your own prices. Old entries without prices default to 100.

rarity_images lets you provide up to 16 different images for the same card. If you omit a rarity's image, it uses image_url until you add artwork. The two demo cards supplied in the ZIP reuse their normal image in every slot because custom rarity artwork has not been supplied. Replace each slot with your variant image URL for distinct artwork. The rarity label and rarity tracking still work while images are shared.

To add more cards, add another object inside the outer square brackets, separated with a comma. No comma after the final object. Use double quotes and no comments. Keep IDs unique. Names and IDs must be at most 80 characters.

Commit changes and redeploy for JSON edits to load. Changing a display name/image updates existing cards too. Removing an entry keeps its last definition in the saved database for old cards; use weight 0 for an explicitly retired card.

## Customize the shop

In economy.py, SHOP contains product IDs, labels and prices. The supplied three entries match the requested prices. Editing prices affects future purchases. Keep product IDs unchanged so already purchased items remain redeemable. Do not remove product definitions that people have purchased.

To alter rewards, edit the relevant amounts in award_claim, exchange and set_bonus in economy.py. Back up your data before changing reward/migration logic.

## First setup or local run

Create an application at https://discord.com/developers/applications. Generate its bot token and keep it private. Enable Message Content Intent on Bot > Privileged Gateway Intents. Invite with bot and applications.commands scopes; allow View Channels, Send Messages, Embed Links, Read Message History.

For Railway, put bot.py, economy.py, items.json, Dockerfile and requirements.txt in the GitHub repository root. Create a Railway service from it, set DISCORD_TOKEN and DATA_DIR=/data, attach a volume at /data, deploy. No web domain or HTTP healthcheck is needed. Check pricing at https://railway.com/pricing. Run one replica. Disable sleep/serverless mode if enabled.

Optionally set TEST_GUILD_ID to your server ID for immediate test-server command registration. In Discord turn on Settings > Advanced > Developer Mode, right-click the server, Copy Server ID. Leave TEST_GUILD_ID in place while testing; global commands may take time to appear without it.

For a local test, install Python 3.12, open a terminal in this folder:

    python -m venv .venv

Activate on Windows:

    .venv\Scripts\activate

Activate on macOS/Linux:

    source .venv/bin/activate

Then:

    python -m pip install -r requirements.txt

Copy .env.example to .env, set DISCORD_TOKEN, keep local DATA_DIR=./data. Run:

    python bot.py

Closing the terminal stops the bot. Do not run locally alongside your hosted bot.

## Checks and troubleshooting

Run included tests with:

    python -m unittest test_bot.py -v

The suite checks migration, claim bonuses, milestone repetition, full rarity sets, sales, insufficient balances, shop redemption ownership/replays, trade rewards, concurrent claims, listing order, variant labels and spawn timing. These are local database/mock checks. A Discord token/server is required for live interaction testing.

Claim does nothing or error 4014: check Message Content Intent in the portal and restart. Check the channel and expiry. Blank image: open its URL in a private browser window and ensure it is an image. JSON error: check commas, double quotes and field types. Data missing: keep DATA_DIR and volume mount both at /data. New commands missing: check logs and TEST_GUILD_ID. If economy.py is missing in logs, upload it and the updated Dockerfile.
