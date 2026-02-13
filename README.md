# Instagram → Discord Auto-Poster (Apify)

This project checks new posts from `https://www.instagram.com/hashtagutd/` and sends only new posts to a Discord channel webhook.

It uses the Apify actor `apidojo/instagram-scraper` with a small state file to deduplicate posts between runs.

## Features

- Scheduled run every 2 hours via GitHub Actions.
- Manual run via **Run workflow** (`workflow_dispatch`).
- Deduplication via `.state/ig_state.json` persisted by GitHub Actions cache.
- Posts only new items to Discord.
- Defensive parsing for actor output field differences (`id`, `shortCode`, `url`, etc.).
- `DRY_RUN=1` support for manual testing without sending messages.

## Required secrets

- `APIFY_API_TOKEN`
- `DISCORD_WEBHOOK_URL`

## Workflow file

- `.github/workflows/instagram_to_discord.yml`

## Python entrypoint

- `instagram_to_discord.py`

## Environment variables

- `APIFY_API_TOKEN` (required)
- `DISCORD_WEBHOOK_URL` (required)
- `IG_PROFILE_URL` (default: `https://www.instagram.com/hashtagutd/`)
- `APIFY_ACTOR_ID` (default: `apidojo/instagram-scraper`)
- `MAX_ITEMS` (default: `3`, range `1..50`)
- `STATE_PATH` (default: `.state/ig_state.json`)
- `DRY_RUN` (`1` = do not send to Discord)

## Local run

```bash
python -m pip install -U pip
pip install -r requirements.txt

# dry run (no Discord post)
DRY_RUN=1 APIFY_API_TOKEN=xxx DISCORD_WEBHOOK_URL=xxx python instagram_to_discord.py
```

## Notes

- First run sends only the newest post to avoid flooding Discord with historical posts.
- Later runs send only unseen posts, oldest → newest.
