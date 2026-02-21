# Instagram → Discord Auto-Poster

This project monitors only one Instagram account: **`https://www.instagram.com/HashtagUtd/`**.

When a new post or reel appears, it is sent to a Discord channel through a webhook.

## Features

- Tracks only **@HashtagUtd**.
- Scheduled/manual run in GitHub Actions.
- Provider fallback strategy:
  - `apify` first (`apify/instagram-scraper`, `resultsType=posts`, `resultsLimit=1`),
  - then `instaloader` fallback.
- Stable dedupe via latest shortcode persisted to `STATE_FILE` (default `.cache/instagram_last_post.txt`).
- `force_post=true` always posts latest item (or dry-run preview).
- `dry_run=true` fetches and logs what would be posted.
- Discord robustness:
  - retries on 429 (`Retry-After`) and 5xx,
  - no mass mentions (`allowed_mentions.parse = []`),
  - download size cap (`MAX_DOWNLOAD_MB`, default `8`).

## Required secrets

- `DISCORD_WEBHOOK_URL`
- `APIFY_API_TOKEN` (recommended; can be omitted if only using `instaloader` fallback)

## Python entrypoint

- `scripts/instagram_to_discord.py`

## Environment variables

- `INSTAGRAM_USERNAME` (default: `HashtagUtd`)
- `DISCORD_WEBHOOK_URL` (required unless `DRY_RUN=true`)
- `STATE_FILE` (default: `.cache/instagram_last_post.txt`)
- `FORCE_POST` (`true/false`)
- `DRY_RUN` (`true/false`)
- `APIFY_API_TOKEN`
- `PROVIDER_ORDER` (default: `apify,instaloader`)
- `SKIP_ON_FETCH_ERRORS` (default: `true`)
- `FETCH_TIMEOUT_SECONDS` (default: `90`)
- `MAX_MEDIA_FILES` (default: `4`)
- `MAX_DOWNLOAD_MB` (default: `8`)

## Local runbook

```bash
python -m pip install -U pip
pip install -r requirements.txt

# smoke test (no Discord post)
export INSTAGRAM_USERNAME="HashtagUtd"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export APIFY_API_TOKEN="..."
export STATE_FILE=".cache/instagram_last_post.txt"
export DRY_RUN="true"
python scripts/instagram_to_discord.py
```

## Production runbook (GitHub Actions)

1. Run `workflow_dispatch` with `dry_run=true` first.
2. Run again with `force_post=true` to verify Discord delivery.
3. Return to normal schedule with `force_post=false`.

## Typical failure cases

- Missing `DISCORD_WEBHOOK_URL`: script exits unless `DRY_RUN=true`.
- Missing/invalid `APIFY_API_TOKEN`: `apify` provider fails and script falls back to `instaloader` if configured.
- Private/blocked profile or temporary scraping limits: fetch may fail; with `SKIP_ON_FETCH_ERRORS=true`, the job logs warning and exits successfully.
- Discord 429/rate limits: webhook send retries up to 3 attempts.
