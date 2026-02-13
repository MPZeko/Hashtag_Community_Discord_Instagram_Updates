#!/usr/bin/env python3
"""Instagram -> Discord for @HashtagUtd via Apify posts actor.

Key behavior:
- Monitors only https://www.instagram.com/HashtagUtd/
- Fetches only the newest post candidate to reduce API usage
- Posts to Discord only when a *new* post is detected
- Persists last seen post key in a state file
"""

from __future__ import annotations

import hashlib
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


def _looks_like_post_url(value: str) -> bool:
    return "/p/" in value or "/reel/" in value or "/tv/" in value


def item_url(item: dict[str, Any]) -> str:
    for key in ("url", "postUrl", "permalink", "link"):
        value = item.get(key)
        if isinstance(value, str) and value.strip() and _looks_like_post_url(value):
            return value.strip()

    for code_key in ("shortCode", "shortcode", "code"):
        code = item.get(code_key)
        if isinstance(code, str) and code.strip():
            return f"https://www.instagram.com/p/{code.strip()}/"

    return PROFILE_URL


def item_caption(item: dict[str, Any]) -> str:
    for key in ("caption", "text", "title", "captionText"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def item_key(item: dict[str, Any]) -> str:
    for key in ("id", "shortCode", "shortcode", "code", "pk"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()

    url = item_url(item)
    if url != PROFILE_URL:
        return url

    # Deterministic hash fallback (Python hash() is randomized per process).
    encoded = json.dumps(item, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def extract_media(item: dict[str, Any]) -> tuple[str | None, str | None]:
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


def _flatten_post_candidates(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize actor output to a flat list of post-like dictionaries."""
    candidates: list[dict[str, Any]] = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        # Some actor modes return profile objects with embedded posts.
        for embedded_key in ("latestPosts", "posts", "items"):
            embedded = item.get(embedded_key)
            if isinstance(embedded, list):
                candidates.extend(x for x in embedded if isinstance(x, dict))

        candidates.append(item)

    # Keep only entries that look like posts (post URL or shortcode-ish keys).
    filtered: list[dict[str, Any]] = []
    for item in candidates:
        url = item_url(item)
        has_code = any(isinstance(item.get(k), str) and item.get(k, "").strip() for k in ("shortCode", "shortcode", "code"))
        if _looks_like_post_url(url) or has_code:
            filtered.append(item)

    # De-duplicate by stable key while preserving order.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in filtered:
        key = item_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def fetch_latest_post_item(token: str) -> dict[str, Any] | None:
    """Fetch at most one latest post candidate from Apify."""
    client = ApifyClient(token)
    run_input = {
        "startUrls": [PROFILE_URL],
        "maxItems": 1,
    }

    print(f"[apify] actor={ACTOR_ID} profile={PROFILE_URL} maxItems=1")
    run = client.actor(ACTOR_ID).call(run_input=run_input)

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Apify run did not return defaultDatasetId")

    items = client.dataset(dataset_id).list_items().items
    if not isinstance(items, list):
        raise RuntimeError("Unexpected dataset items type")

    normalized = _flatten_post_candidates([x for x in items if isinstance(x, dict)])
    if not normalized:
        return None
    return normalized[0]


def build_payload(item: dict[str, Any]) -> dict[str, Any]:
    url = item_url(item)
    caption = item_caption(item)
    if len(caption) > 1500:
        caption = caption[:1497] + "..."

    image_url, video_url = extract_media(item)

    content_lines = [f"📸 New Instagram post from **@HashtagUtd**", f"Post: {url}"]
    if video_url:
        content_lines.append(f"Video: {video_url}")

    embed: dict[str, Any] = {
        "title": "New post from @HashtagUtd",
        "url": url,
    }

    # Only set description when we actually have text.
    if caption:
        embed["description"] = caption

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
    state_path = Path(env("STATE_PATH", ".state/ig_state.json") or ".state/ig_state.json")
    dry_run = env("DRY_RUN", "0") == "1"
    force_latest = env("FORCE_LATEST", "0") == "1"

    if not token:
        print("ERROR: APIFY_API_TOKEN is missing", file=sys.stderr)
        return 1
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL is missing", file=sys.stderr)
        return 1

    state = load_state(state_path)
    last_seen_key = state.get("last_seen_key") if isinstance(state.get("last_seen_key"), str) else None

    try:
        latest_item = fetch_latest_post_item(token)
    except Exception as err:
        print(f"ERROR: apify fetch failed: {err}", file=sys.stderr)
        return 1

    if not latest_item:
        print("[info] no post items returned")
        return 0

    latest_key = item_key(latest_item)

    if not force_latest and last_seen_key == latest_key:
        print("[info] no new posts")
        return 0

    if force_latest:
        print("[info] FORCE_LATEST enabled -> posting newest item")
    else:
        print("[info] new post detected -> posting")

    post_to_discord(webhook, latest_item, dry_run=dry_run)

    # Update state only after successful post (or dry-run post simulation).
    save_state(state_path, {"last_seen_key": latest_key, "updated_at": int(time.time())})
    print("[info] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
