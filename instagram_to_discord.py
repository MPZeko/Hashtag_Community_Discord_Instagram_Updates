#!/usr/bin/env python3
"""Instagram -> Discord for @HashtagUtd via Apify posts actor.

- Uses Apify actor: apidojo/instagram-scraper
- Tracks only: https://www.instagram.com/HashtagUtd/
- Dedupes with local state file (cached by GitHub Actions)
- Posts new items to Discord, including media preview hints when available
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from apify_client import ApifyClient

PROFILE_URL = "https://www.instagram.com/HashtagUtd/"
ACTOR_ID = "apidojo/instagram-scraper"


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(state_path)


def item_key(item: dict[str, Any]) -> str:
    for key in ("id", "shortCode", "shortcode", "code", "pk"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()

    fallback_url = item.get("url") or item.get("postUrl") or item.get("displayUrl")
    if isinstance(fallback_url, str) and fallback_url.strip():
        return fallback_url.strip()

    return str(hash(json.dumps(item, sort_keys=True, default=str)))


def item_url(item: dict[str, Any]) -> str:
    for key in ("url", "postUrl", "permalink"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return PROFILE_URL


def item_caption(item: dict[str, Any]) -> str:
    for key in ("caption", "text", "title"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_media(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (image_url, video_url) using defensive field matching."""
    image_url = None
    video_url = None

    for key in ("displayUrl", "imageUrl", "thumbnailUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            image_url = value.strip()
            break

    for key in ("videoUrl", "video_url"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            video_url = value.strip()
            break

    sidecar = item.get("childPosts")
    if isinstance(sidecar, list):
        for child in sidecar:
            if not isinstance(child, dict):
                continue
            if not image_url:
                value = child.get("displayUrl") or child.get("imageUrl")
                if isinstance(value, str) and value.strip():
                    image_url = value.strip()
            if not video_url:
                value = child.get("videoUrl")
                if isinstance(value, str) and value.strip():
                    video_url = value.strip()
            if image_url and video_url:
                break

    return image_url, video_url


def fetch_items_apify(token: str, max_items: int) -> list[dict[str, Any]]:
    client = ApifyClient(token)
    run_input = {
        "startUrls": [PROFILE_URL],
        "maxItems": max_items,
    }

    print(f"[apify] actor={ACTOR_ID} profile={PROFILE_URL} maxItems={max_items}")
    run = client.actor(ACTOR_ID).call(run_input=run_input)

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Apify run did not return defaultDatasetId")

    items = client.dataset(dataset_id).list_items().items
    if not isinstance(items, list):
        raise RuntimeError("Unexpected dataset items type")

    return [item for item in items if isinstance(item, dict)]


def diff_new_items(items_newest_first: list[dict[str, Any]], last_seen_key: str | None) -> tuple[list[dict[str, Any]], str | None]:
    if not items_newest_first:
        return [], last_seen_key

    keys = [item_key(item) for item in items_newest_first]
    newest_key = keys[0]

    if not last_seen_key:
        return [items_newest_first[0]], newest_key

    new_chunk: list[dict[str, Any]] = []
    for item, key in zip(items_newest_first, keys):
        if key == last_seen_key:
            break
        new_chunk.append(item)

    if not new_chunk:
        return [], newest_key

    return list(reversed(new_chunk)), newest_key


def build_payload(item: dict[str, Any]) -> dict[str, Any]:
    url = item_url(item)
    caption = item_caption(item)
    if len(caption) > 1500:
        caption = caption[:1497] + "..."

    image_url, video_url = extract_media(item)

    content_lines = [f"📸 New Instagram post from **@HashtagUtd**", f"Post: {url}"]
    if video_url:
        # If Discord cannot render the video inline, users can still click through.
        content_lines.append(f"Video: {video_url}")

    embed: dict[str, Any] = {
        "title": "New post from @HashtagUtd",
        "url": url,
        "description": caption or "No caption provided.",
    }

    if image_url:
        embed["image"] = {"url": image_url}

    if video_url:
        embed["video"] = {"url": video_url}

    return {
        "content": "\n".join(content_lines),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }


def post_to_discord(webhook_url: str, item: dict[str, Any], dry_run: bool = False) -> None:
    payload = build_payload(item)
    if dry_run:
        print("[dry-run] would post:", json.dumps(payload, ensure_ascii=False))
        return

    response = requests.post(webhook_url, json=payload, timeout=20)
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text[:300]}")


def main() -> int:
    token = env("APIFY_API_TOKEN")
    webhook = env("DISCORD_WEBHOOK_URL")
    max_items_str = env("MAX_ITEMS", "3")
    state_path = Path(env("STATE_PATH", ".state/ig_state.json") or ".state/ig_state.json")
    dry_run = env("DRY_RUN", "0") == "1"
    force_latest = env("FORCE_LATEST", "0") == "1"

    if not token:
        print("ERROR: APIFY_API_TOKEN is missing", file=sys.stderr)
        return 1
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL is missing", file=sys.stderr)
        return 1

    try:
        max_items = int(max_items_str or "3")
        if max_items < 1 or max_items > 50:
            raise ValueError
    except ValueError:
        print("ERROR: MAX_ITEMS must be an integer between 1 and 50", file=sys.stderr)
        return 1

    state = load_state(state_path)
    last_seen_key = state.get("last_seen_key") if isinstance(state.get("last_seen_key"), str) else None

    try:
        items = fetch_items_apify(token, max_items)
    except Exception as err:
        print(f"ERROR: apify fetch failed: {err}", file=sys.stderr)
        return 1

    if not items:
        print("[info] no items returned")
        return 0

    if force_latest:
        newest = items[0]
        newest_key = item_key(newest)
        print("[info] FORCE_LATEST enabled -> posting newest item")
        post_to_discord(webhook, newest, dry_run=dry_run)
        save_state(state_path, {"last_seen_key": newest_key, "updated_at": int(time.time())})
        return 0

    new_items, new_last_seen_key = diff_new_items(items, last_seen_key)

    if not new_items:
        print("[info] no new posts")
        save_state(state_path, {"last_seen_key": new_last_seen_key, "updated_at": int(time.time())})
        return 0

    print(f"[info] new posts to send: {len(new_items)}")
    for item in new_items:
        post_to_discord(webhook, item, dry_run=dry_run)

    save_state(state_path, {"last_seen_key": new_last_seen_key, "updated_at": int(time.time())})
    print("[info] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
