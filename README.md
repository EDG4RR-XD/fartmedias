# Image collector bot with chudlets

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

The tests now include permissions, capped and idempotent chudlet adjustments, chosen spawns, server isolation, ZIP contents, integrity checks, WAL snapshot restoration, and denial of full exports to non-owners. 15 local tests passed. Live Discord tests still require your token/server.

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

/list [page]: shows all known base cards. Owned cards appear in the upper row/section, missing cards below it. Each owned card shows counts for all six rarities. Use page:2, page:3 etc. when needed. Each page shows up to three owned and three missing cards.
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
- Full rarity set: 5000 once per player and base card when they own normal, silver, gold, emerald, diamond and obsidian together. A claim or incoming trade can complete the set. Selling and rebuilding the set does not repeat the payout.

Rewards can stack. All balances, inventories, history and collections are per server. Selling cards never removes earned chudlets or prior discovery records. Keep volume backups enabled on your host.

## Rarity rolls

A base card is chosen using its weight. A separate rarity roll then chooses normal 79%, silver 10%, gold 5%, emerald 3%, diamond 2%, obsidian 1%. These are mutually exclusive outcomes totaling 100%. Variants share one base card ID but each collected copy stores its own rarity.

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

rarity_images lets you provide six different images for the same card. If you omit a rarity's image, it uses image_url until you add artwork. The two demo cards supplied in the ZIP reuse their normal image in every slot because custom rarity artwork has not been supplied. Replace each slot with your variant image URL for distinct artwork. The rarity label and rarity tracking still work while images are shared.

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
