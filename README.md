# Heroku Cloud Controller Telegram Bot

A Telegram control panel for managing your own Heroku apps through the official Heroku Platform API.

## Features

- Start panel styled like the supplied screenshot
- Separate `/start` and `/help`
- Owner + Support buttons
- Link/replace a Heroku API token
- Apps Manager
- App formation/dyno view
- Restart request
- Dyno scaling
- Config-var names view (values are masked)
- Log-session viewer
- Quick deploy from a public GitHub repository using Heroku App Setups
- Account unlink
- SQLite persistence
- Heroku/Telegram credentials are read from environment variables

## Important security note

This project intentionally does NOT collect Heroku passwords, recovery codes, or login OTPs.

The `/link` flow accepts a Heroku API token for the user's own account. For a production service used by multiple people, replace token entry with Heroku OAuth 2.0 so users authorize the bot without sending tokens to Telegram. Heroku recommends OAuth for third-party services.

## Deploy on Heroku

1. Create a Telegram bot with BotFather.
2. Create a Heroku app for this bot.
3. Add config vars:
   - `BOT_TOKEN`
   - `OWNER_ID`
   - `OWNER_URL`
   - `SUPPORT_URL`
4. Deploy this repository.
5. Enable the `worker` process.

## Local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="..."
export OWNER_ID="123456789"
python bot.py
```

## GitHub quick deploy

The bot accepts a public GitHub repository URL and submits it to Heroku's `app-setups` API. The repository must contain a valid Heroku deployment structure (for example a Procfile/runtime and application source as appropriate).

## API notes

The implementation uses `https://api.heroku.com` and the v3 Platform API headers. Heroku's current documentation recommends OAuth 2.0 for third-party integrations.
