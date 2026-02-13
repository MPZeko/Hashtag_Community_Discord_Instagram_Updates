# Instagram → Discord Auto-Poster (Apify)

This project monitors only one Instagram account: **`https://www.instagram.com/HashtagUtd/`**.

When new posts appear, they are sent to a Discord channel through a webhook.

## Features

- Tracks only **@HashtagUtd**.
- Scheduled run every 2 hours via GitHub Actions.
- Manual run via **Run workflow** (`workflow_dispatch`).
- Manual options:
  - `force_latest=true` to post newest post immediately (even if already seen).
  - `dry_run=true` to test fetch without posting.
- Deduplication via `.state/ig_state.json` persisted by GitHub Actions cache.
- Attempts media-friendly Discord payloads:
  - image preview in embed when an image URL is available,
  - video URL included so users can open/play via Instagram when inline rendering is unavailable.

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
- `MAX_ITEMS` (default: `3`, range `1..50`)
- `STATE_PATH` (default: `.state/ig_state.json`)
- `FORCE_LATEST` (`1` = post newest regardless of state)
- `DRY_RUN` (`1` = do not send to Discord)

## Local run

```bash
python -m pip install -U pip
pip install -r requirements.txt

# test without posting
DRY_RUN=1 APIFY_API_TOKEN=xxx DISCORD_WEBHOOK_URL=xxx python instagram_to_discord.py

# manually post newest item
FORCE_LATEST=1 APIFY_API_TOKEN=xxx DISCORD_WEBHOOK_URL=xxx python instagram_to_discord.py
```

## Notes

- First normal run sends only the newest post to avoid flooding with historical posts.
- Later runs send only unseen posts, oldest → newest.
