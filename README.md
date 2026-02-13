# Instagram → Discord Auto-Poster

This repository contains a GitHub Actions automation that checks the latest post from `instagram.com/HashtagUtd` and forwards it to a Discord channel using a webhook.

## Features

- Polls Instagram every 2 hours using GitHub Actions schedule.
- Supports manual run via **Run workflow** (`workflow_dispatch`).
- Avoids duplicate Discord posts by storing the latest posted shortcode in a cache-based state file.
- Optional `force_post` input to repost the latest item during manual testing.
- Optional `dry_run` input to test fetching without sending anything to Discord.
- Attempts to upload media directly to Discord (images/videos) so content can be viewed inside Discord when possible.
- Includes fallback media links when direct upload is not possible.
- Handles temporary Instagram rate limits (HTTP 429) gracefully by skipping the run and retrying on next schedule.
- Uses workflow concurrency + timeout to avoid overlapping runs.

## Required GitHub Secret

Create this repository secret:

- `DISCORD_WEBHOOK_URL` – Webhook URL for the destination Discord channel.

## Workflow

The automation lives in:

- `.github/workflows/instagram-to-discord.yml`

Triggers:

- **Scheduled**: every 2 hours (`0 */2 * * *`)
- **Manual**: run from GitHub Actions UI with options:
  - `force_post`: `true/false`
  - `dry_run`: `true/false`

## Local test command

Install dependencies:

```bash
pip install -r requirements.txt
```

Dry-run test (no Discord post):

```bash
python scripts/instagram_to_discord.py --dry-run
```

Force post test:

```bash
python scripts/instagram_to_discord.py --force-post
```

## Environment Variables

- `INSTAGRAM_USERNAME` (default: `HashtagUtd`)
- `DISCORD_WEBHOOK_URL` (required unless dry-run)
- `STATE_FILE` (default: `.cache/instagram_last_post.txt`)
- `FORCE_POST` (`true/false`)
- `DRY_RUN` (`true/false`)
- `MAX_MEDIA_FILES` (default: `4`)
- `MAX_DOWNLOAD_MB` (default: `20`)
- `LOG_LEVEL` (default: `INFO`)
- `SKIP_ON_FETCH_ERRORS` (default: `true`)
- `FETCH_TIMEOUT_SECONDS` (default: `90`)

## Notes

- GitHub Actions cannot natively subscribe to Instagram "new post" events, so this setup uses polling every 2 hours.
- Instagram can temporarily return HTTP 429 for anonymous scraping; this workflow now treats those fetch errors as transient and retries on the next scheduled run.
- The script uses a hard fetch timeout to avoid Instaloader waiting ~30 minutes inside a single run when rate limited.
- If Instagram changes response behavior or rate limits access, retries or authenticated scraping may be needed.
