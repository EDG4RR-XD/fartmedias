# Flag collector bot

Original Python starter inspired by flag-catching bots. Includes 16 countries, weighted timer spawns, button + name guessing, persistent collections, and consensual one-card trades. Collections are separate for each server. This is a starter, with no battles, image card generator, or event system.

## 1. Create your Discord bot

1. Open https://discord.com/developers/applications and choose New Application. Name it.
2. Open Bot. Use Reset Token to generate a token. Save it privately. Never send it in Discord, upload it to GitHub, or paste it into a chat.
3. This code uses default intents. Leave privileged intents off.
4. Under OAuth2 > URL Generator choose scopes `bot` and `applications.commands`.
5. Choose View Channels, Send Messages, Embed Links, and Read Message History. Administrator is unnecessary.
6. Open the generated URL. Install the bot in a server where you can manage apps.

## 2. Put it online using Railway

Railway is usage-billed. Check https://railway.com/pricing before deploying and choose a spending limit suitable for you. Do not assume a trial will run a bot indefinitely.

1. Extract this ZIP.
2. Create a private repository at https://github.com/new.
3. Choose Add file > Upload files. Upload bot.py, requirements.txt, Dockerfile, README.md and the supplied dotfiles if your file picker shows them. The Dockerfile must be at the repository root, alongside bot.py. Commit the files. Do not upload an actual .env file or the data folder.
4. Open https://railway.com and create a project using Deploy from GitHub repo. Authorize access to that repository and select it. Railway should build using the Dockerfile. The initial deployment may fail until the variables are added.
5. In the bot service's Variables, add:
   DISCORD_TOKEN = your bot token
   DATA_DIR = /data
6. For immediate server-only command registration, optionally add TEST_GUILD_ID = your Discord server ID. In Discord Settings > Advanced, turn on Developer Mode; right-click your server and Copy Server ID. Leave this variable in place while testing. Without it, commands register globally and may take time to appear.
7. Attach a Volume to the bot service. Set its mount path to /data. Both the volume mount and DATA_DIR must be /data. This preserves catches and configuration across deployments.
8. Apply the changes and deploy/redeploy. Run one replica. Disable serverless/sleep mode if enabled. Do not configure an HTTP healthcheck or generate a web domain: this process connects out to Discord.
9. Open deployment logs. Look for the Discord login/Gateway connection. The bot should appear online.
10. In Discord, run /setup and choose a normal text channel. You need Manage Server. A flag should appear within 30 seconds. Later flags appear every 10–20 minutes and expire after five minutes.
11. Click Catch, type the country name, and submit. Run /collection to see your card and ID. /spawn lets a server manager test another spawn once no active flag remains.

Keep the service running to keep the bot online. Stop any local copy before starting the hosted copy. Enable volume backups in your host dashboard before relying on the collection data.

## Commands

/setup channel: choose spawn channel, Manage Server required.
/spawn: manually test a spawn, Manage Server required.
/collection member: optional player, page: optional page number. Displays card IDs.
/trade member: other player, give_id: your card ID, want_id: their card ID.

Example: /trade member:@Alex give_id:12 want_id:27 offers your #12 for Alex's #27. Alex must click Accept within two minutes. Card ownership is checked again at acceptance. Offers expire on restart; saved cards remain. Old expired Catch buttons may stay visible but cannot award a card.

## Optional local test

Install Python 3.12 from https://python.org. Open a terminal in this extracted folder.

    python -m venv .venv

Activate on Windows:

    .venv\Scripts\activate

Activate on macOS/Linux:

    source .venv/bin/activate

Then:

    python -m pip install -r requirements.txt

Copy .env.example to .env. Set DISCORD_TOKEN, optionally TEST_GUILD_ID. For local use leave DATA_DIR=./data. Run:

    python bot.py

Closing the terminal or shutting down the computer stops a local bot.

## Customize

Edit FLAGS in bot.py. Each entry contains (country name, flag emoji, relative spawn weight, alternate accepted names). Higher weight means more common. Add countries using the same format. You can also use your own collectible names and custom Discord emoji strings. Keep existing country names after people collect them, because saved cards reference those names.

Change random.randint(600, 1200) to change seconds between spawns. Change the two occurrences of 300 for expiry and update the displayed expiry text too. Update setup's message if you change spawn timing.

## Troubleshooting

- Invalid token: reset the token in Discord Developer Portal, update the host variable, and redeploy.
- Commands missing: ensure applications.commands was selected when inviting. Check TEST_GUILD_ID and deployment logs. Global commands may take time to appear.
- Missing permissions: check channel permission overrides as well as the bot role. Allow all four listed permissions.
- Data disappeared: check DATA_DIR=/data and that a volume is attached at /data. Data previously written outside the volume will not move automatically.
- Flags look like letters: some devices render flag emoji as country codes. Custom images can be added later.
- Bot goes offline: check hosting billing, deployment status and logs.

## Validation

See the accompanying response for checks actually run. Live Discord interactions require your own application token and test server.

## References

https://discord.com/developers/docs/quick-start/getting-started
https://discordpy.readthedocs.io/en/stable/interactions/api.html
https://docs.railway.com/quick-start
https://docs.railway.com/volumes
https://docs.railway.com/pricing/plans
