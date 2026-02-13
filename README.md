# Instagram → Discord Auto-Poster (Apify)

This project monitors only one Instagram account: **`https://www.instagram.com/HashtagUtd/`**.

When a new post appears, it is sent to a Discord channel through webhook.

## Features

- Tracks only **@HashtagUtd**.
- Scheduled run every 2 hours via GitHub Actions.
- Manual run via **Run workflow** (`workflow_dispatch`).
- Manual options:
  - `force_latest=true` to post newest post immediately (even if already seen).
  - `dry_run=true` to test fetch without posting.
- Deduplication via `.state/ig_state.json` persisted by GitHub Actions cache.
- Posts **only when latest post key changes**.
- Media behavior:
  - image preview in embed when available,
  - video URL included as fallback link to Instagram when inline playback is unavailable.
- If a post has no caption, the bot simply omits caption text (it no longer writes `No caption provided`).

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

- Normal mode fetches a single latest post candidate (`maxItems=1`) to keep API costs down.
- Bot posts only if detected latest key differs from stored `last_seen_key`.
