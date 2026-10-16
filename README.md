# Image collector bot

Images appear in the configured channel every 10–20 minutes. The first human player to send the message Claim in that channel gets the collectible. claim and CLAIM work too. Images expire after five minutes. Collections, card IDs and accepted trades save to SQLite. Collections are per server. Duplicate copies of an item can be collected.

## Updating your existing GitHub/Railway bot

1. Extract this ZIP.
2. Open your existing GitHub repository, choose Add file > Upload files.
3. Upload bot.py, items.json, Dockerfile, requirements.txt and README.md from inside the extracted folder. Keep these files at the repository root. This replaces the previous files with matching names. Do not upload .env or your data directory.
4. Click Commit changes.
5. Go to https://discord.com/developers/applications, select your app, open Bot, scroll to Privileged Gateway Intents, enable Message Content Intent and save. This version reads the word Claim from chat. If Discord asks you to apply for access, follow its portal instructions.
6. In Railway, keep DISCORD_TOKEN as your existing token, DATA_DIR=/data, and your existing volume mounted at /data. Keep any TEST_GUILD_ID variable you already set.
7. Wait for GitHub autodeploy, or deploy the latest commit manually. Look at deployment logs for a successful Discord connection. Only run one replica and stop any local copy.
8. Run /setup with your chosen channel if needed. Existing saved settings are reused. Use /spawn to test when no active image is waiting.
9. Type Claim in the same channel as the spawned image. /collection lists your card IDs. /view card_id:1 displays card #1. /trade still works.

The existing data/flags.db filename and original database columns are deliberately retained for compatibility. Keep your volume and DATA_DIR unchanged. Old flag cards stay in collections; old items without configured images display a legacy notice in /view. Old active spawns remain claimable until they expire. Removing an item from items.json after it has been loaded keeps its stored definition for old cards.

## Add your own claimable images

The supplied items.json contains two demo image entries, Canada and Japan. Replace them with your own items if desired.

1. Upload an image to an image host that provides a public, direct HTTPS image link. For example, upload your file to File Garden and copy the file URL. Opening that URL in a private browser window should display the image itself without requiring login. A webpage containing an image is not the same as a direct image link. Discord attachment URLs can expire, so use a persistent image host.
2. In GitHub click items.json, then the pencil button to edit it.
3. Each object inside the outer square brackets defines one item. Example:

```json
[
  {
    "id": "moon_cat",
    "name": "Moon Cat",
    "image_url": "https://your-image-host.example/moon-cat.png",
    "weight": 10
  },
  {
    "id": "sun_dog",
    "name": "Sun Dog",
    "image_url": "https://your-image-host.example/sun-dog.png",
    "weight": 2
  }
]
```

Replace the example URLs with your actual image links before deploying.

- id: unique permanent internal identifier. Use something like moon_cat. Keep this unchanged after anyone claims it. Existing cards reference this ID.
- name: display name. You may rename it; existing cards update to the new display name too.
- image_url: public direct HTTPS image link. You may replace this; existing cards then display the replacement image too.
- weight: relative spawn likelihood. A weight of 10 appears about five times as often as a weight of 2. With only those two items, their probabilities are 10/12 and 2/12. Set weight to 0 to retire an item while preserving its definition.

4. To add a third item, put a comma after the second item's closing brace, then add another object before the final ]. Do not put a comma after the final object. Use double quotes, with no comments.
5. Click Commit changes. Railway must redeploy/restart for the edited item list to load. Existing collections remain in your volume.
6. Test using /spawn. Selection is random, so a newly added item may take several spawns to appear. To test only one item, temporarily set the others' weights to 0, then restore them.

You can have one item or many. At least one must have weight above 0. The bot checks required fields, duplicate IDs and invalid weights on startup. It cannot guarantee that an external host will keep serving your image.

## More customization

Open bot.py in GitHub and edit these constants near the top:

    SPAWN_MIN_SECONDS = 600
    SPAWN_MAX_SECONDS = 1200
    CLAIM_SECONDS = 300

600 and 1200 mean 10–20 minutes between spawns. 300 is a five-minute claim window. Keep the minimum at or below the maximum. The background loop checks every 30 seconds. If you change the timings, update the /setup confirmation text too.

Change the spawn embed color by editing color=0x79B86D in bot.py. A hex color like #FF5519 becomes 0xFF5519 in Python. Edit the Embed title/description strings to change the messages. To change bot name/avatar, use the Discord Developer Portal.

## First-time hosting

Create a Discord application at https://discord.com/developers/applications and generate its bot token. Keep it private. Enable Message Content Intent as above. Invite using OAuth2 > URL Generator, selecting bot and applications.commands, with View Channels, Send Messages, Embed Links and Read Message History permissions.

Upload the files to a private GitHub repository. In Railway create a project from that repository. It builds with the included Dockerfile. Add DISCORD_TOKEN and DATA_DIR=/data in service Variables, attach a volume at /data, then deploy. Run one replica. Disable sleeping/serverless mode if enabled. This bot needs no web domain or HTTP healthcheck. Check Railway's pricing before running it: https://railway.com/pricing.

For immediate development command registration, optionally set TEST_GUILD_ID to your server ID. Turn on Discord Settings > Advanced > Developer Mode, right-click your server, and copy its ID. Leave that variable in place while testing. Global commands may take time to appear if TEST_GUILD_ID is absent.

## Optional local run

Install Python 3.12, open a terminal in this folder, then run:

    python -m venv .venv

Windows activation:

    .venv\Scripts\activate

macOS/Linux activation:

    source .venv/bin/activate

Then:

    python -m pip install -r requirements.txt

Copy .env.example to .env, set DISCORD_TOKEN, leave DATA_DIR=./data, and run:

    python bot.py

Closing the terminal stops the local bot. Do not run it at the same time as your hosted instance.

## Troubleshooting

- Claim does nothing: enable Message Content Intent in the Developer Portal, save, and restart/redeploy. Send Claim as its entire message in the image's channel before expiry. Bot/webhook messages are ignored.
- PrivilegedIntentsRequired or close code 4014: Message Content Intent is not enabled/authorized in the portal.
- Image is blank: check the image link in a private browser window. It must serve a supported image such as PNG or JPEG. The bot does not validate remote image bytes on startup.
- JSON error on startup: check double quotes, missing commas, trailing commas and duplicate IDs.
- Commands missing: check application invite scopes, server ID and deployment logs.
- Data missing: check the same volume remains mounted at /data and DATA_DIR is /data. Do not delete the volume. Enable host volume backups.
- After restart, pending trade offers expire. Saved cards remain.

## Validation

Local mocked tests cover claim parsing, wrong-channel/expired/bot rejection, concurrent claims, spawn images, view fallback and trade ownership. A real Discord server/token is required for live verification.

References:
https://discordpy.readthedocs.io/en/stable/intents.html
https://discordpy.readthedocs.io/en/stable/api.html
https://docs.railway.com/volumes
