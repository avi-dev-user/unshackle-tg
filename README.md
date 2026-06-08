# unshackle-tg

A self-hostable **Telegram frontend for the [unshackle](https://github.com/unshackle-dl/unshackle) downloader**. Drive unshackle from a phone with an inline button UI: paste a link or search, pick season/episode/tracks/quality, and the file is downloaded and sent back to you in Telegram.

This repository is the **framework** - the Telegram bot, the engine wiring, and two example services (`JSON`, `TEST`). You bring your own unshackle services, accounts, and CDM. It ships no commercial services and no keys (see [DISCLAIMER](DISCLAIMER.md)).

## Features

- **Inline, button-driven UX** - users only ever type a URL or a search query; everything else is a button.
- **On-demand downloads** over any unshackle service, with a season -> episode -> tracks -> quality -> delivery wizard.
- **Search** and a **subtitles-only** mode for services that support them.
- **Multi-user RBAC** - the bot serves only known, active users; per-user service allow/deny, category filters, DRM/login blocks, and concurrency limits.
- **Per-user accounts** - cookies and `user:pass` credentials (encrypted at rest), and per-user `.wvd`/`.prd` CDM devices for DRM.
- **Auto-monitors** - watch a series/podcast and auto-download new episodes on an interval or a fixed schedule.
- **Large uploads** - files are sent over MTProto (up to 2GB with a bot client, up to 4GB with an optional Premium user session), with automatic splitting beyond that.
- **i18n** - English by default with an in-bot language switch; translations live in `locales/` (Hebrew included).

## Architecture

One image runs two processes under `supervisord`:

```
Telegram  <--->  bot (aiohttp, Bot API)  --->  unshackle (REST API, `unshackle serve`)  --->  the internet
                      |                                    |
                 inline UI, RBAC,                    download + decrypt + mux
                 accounts, monitors,                 (ffmpeg / mp4decrypt / shaka)
                 MTProto uploader
```

The bot talks to the engine over its local REST API; see `src/engine.py`. The bot code is split into small modules (`menus`, `download`, `monitors_ui`, `admin`, `catalog_meta`, `state`, `uploader`, ...) with `src/bot.py` as the update router.

## Quick start

```bash
git clone https://github.com/avi-dev-user/unshackle-tg.git
cd unshackle-tg
cp .env.example .env        # fill in BOT_TOKEN, API_ID/API_HASH, ADMIN_IDS, ENCRYPTION_KEY
docker compose up -d --build
```

- `BOT_TOKEN` - from [@BotFather](https://t.me/BotFather).
- `API_ID` / `API_HASH` - from [my.telegram.org](https://my.telegram.org); needed for the MTProto uploader (files > 50MB).
- `ADMIN_IDS` - comma-separated Telegram user IDs that are admins.
- `ENCRYPTION_KEY` - a Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) used to encrypt stored credentials.
- For uploads up to 4GB, generate a Premium session with `python gen_session.py` and set `PREMIUM_SESSION`.

See [.env.example](.env.example) for the full list, including the optional catalog-routing settings.

## Adding a service

Services are standard unshackle services. To add one:

1. Drop the service directory under `services/` (or mount your own directory).
2. List its directory in `deploy/unshackle.yaml` under `directories.services` (it accepts a list, so you can keep several directories side by side).
3. Rebuild. The bot reads the live service list from the engine - no bot code changes needed.

Optionally, route and categorise it without touching code via the env settings in `.env.example`: `CATEGORY_SEEDS` (which tab it shows under), `DOMAIN_SERVICES` (fast URL -> service routing), `FREE_SERVICES`, `CATCHALL_SERVICE` (e.g. a yt-dlp service for any other link), and `FEED_SERVICE` (podcast/RSS).

## Adding a language

Translations are flat `KEY -> text` JSON files in `locales/`, auto-loaded at startup. To add a language **no code change is needed**:

1. Copy `locales/en.json` to `locales/<code>.json` (e.g. `locales/de.json`).
2. Translate the values (keep the `{placeholders}` intact).
3. Restart. The language appears in the in-bot 🌐 Language switch automatically.

English (`en.json`) is the source and the fallback for any missing key.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -q
```

The suite covers the pure logic and the persisted stores (catalog parsing, i18n locale
integrity, RBAC, per-user account isolation, the path-traversal guard, atomic saves). CI runs
them on every push (`.github/workflows/ci.yml`).

## License

[GPL-3.0](LICENSE). See also the [DISCLAIMER](DISCLAIMER.md).
