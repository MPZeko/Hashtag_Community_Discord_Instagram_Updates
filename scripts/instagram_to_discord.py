#!/usr/bin/env python3
"""Fetch the latest Instagram post from a profile and publish it to Discord.

Provider strategy:
1) Apify API (if APIFY_API_TOKEN is configured)
2) Instaloader fallback (best effort)

State:
- Persists last seen shortcode to STATE_FILE (default: .cache/instagram_last_post.txt)
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import multiprocessing as mp
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests

# Instaloader is optional at runtime (fallback). Keep import at top so dependency failures are obvious.
import instaloader
from apify_client import ApifyClient


@dataclass
class MediaItem:
    url: str
    is_video: bool


@dataclass
class InstagramPost:
    shortcode: str
    username: str
    caption: str
    created_at: datetime
    permalink: str
    media_items: list[MediaItem]


def fetch_latest_post_via_apify(instagram_username: str, apify_token: str) -> InstagramPost:
    """Fetch latest post via Apify Instagram scraper actor.

    Important: Use resultsType=posts + resultsLimit=1 to reliably get actual post items.
    """
    client = ApifyClient(apify_token)

    run_input = {
        "usernames": [instagram_username],
        "resultsLimit": 1,
        "resultsType": "posts",
    }

    logging.info("[apify] actor=apify/instagram-scraper usernames=%s resultsLimit=1 resultsType=posts", instagram_username)
    run = client.actor("apify/instagram-scraper").call(run_input=run_input)

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Apify run missing defaultDatasetId")

    items = list(client.dataset(dataset_id).iterate_items())
    if not items:
        raise RuntimeError("Apify returned no posts (dataset empty).")

    item = items[0]
    shortcode = (item.get("shortCode") or item.get("shortcode") or item.get("code") or item.get("id") or "").strip()
    if not shortcode:
        raise RuntimeError("Apify item missing shortcode/id fields.")

    caption = (item.get("caption") or item.get("text") or "").strip()

    timestamp = item.get("timestamp")
    if isinstance(timestamp, (int, float)):
        created_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    media: list[MediaItem] = []
    if item.get("type") == "Video" and item.get("videoUrl"):
        media.append(MediaItem(url=item["videoUrl"], is_video=True))
    elif item.get("displayUrl"):
        media.append(MediaItem(url=item["displayUrl"], is_video=False))

    for child in item.get("childPosts") or []:
        if not isinstance(child, dict):
            continue
        url = child.get("videoUrl") or child.get("displayUrl")
        if url:
            media.append(MediaItem(url=url, is_video=bool(child.get("videoUrl"))))

    permalink = item.get("url") or f"https://www.instagram.com/p/{shortcode}/"

    return InstagramPost(
        shortcode=shortcode,
        username=instagram_username,
        caption=caption,
        created_at=created_at,
        permalink=permalink,
        media_items=media,
    )


def fetch_latest_post_via_instaloader(instagram_username: str) -> InstagramPost:
    """Fallback scraper (best effort). Can hit rate limits; run with timeout wrapper."""
    loader = instaloader.Instaloader(
        sleep=False,
        quiet=True,
        download_comments=False,
        download_geotags=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1,
    )
    profile = instaloader.Profile.from_username(loader.context, instagram_username)
    latest = next(profile.get_posts(), None)
    if latest is None:
        raise RuntimeError(f"No posts found for profile '{instagram_username}'.")

    media: list[MediaItem] = []
    if latest.typename == "GraphSidecar":
        for node in latest.get_sidecar_nodes():
            media.append(MediaItem(url=node.video_url or node.display_url, is_video=node.is_video))
    else:
        media.append(MediaItem(url=latest.video_url or latest.url, is_video=latest.is_video))

    return InstagramPost(
        shortcode=latest.shortcode,
        username=instagram_username,
        caption=(latest.caption or "").strip(),
        created_at=latest.date_utc.replace(tzinfo=timezone.utc),
        permalink=f"https://www.instagram.com/p/{latest.shortcode}/",
        media_items=media,
    )


def _worker(provider: str, username: str, apify_token: str | None, queue: mp.Queue) -> None:
    try:
        if provider == "apify":
            if not apify_token:
                raise RuntimeError("APIFY_API_TOKEN is missing.")
            post = fetch_latest_post_via_apify(username, apify_token)
        else:
            post = fetch_latest_post_via_instaloader(username)

        payload = asdict(post)
        payload["created_at"] = post.created_at.isoformat()
        queue.put(("ok", payload))
    except Exception as err:
        queue.put(("error", str(err)))


def _run_provider_with_timeout(provider: str, username: str, apify_token: str | None, timeout_s: int) -> InstagramPost:
    """Run provider in subprocess to avoid hangs."""
    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    p = ctx.Process(target=_worker, args=(provider, username, apify_token, queue))
    p.start()
    p.join(timeout=timeout_s)

    if p.is_alive():
        p.terminate()
        p.join(timeout=5)
        raise TimeoutError(f"Provider '{provider}' exceeded timeout ({timeout_s}s).")

    if queue.empty():
        raise RuntimeError(f"Provider '{provider}' exited without payload (exit_code={p.exitcode}).")

    status, data = queue.get()
    if status != "ok":
        raise RuntimeError(data)

    return InstagramPost(
        shortcode=data["shortcode"],
        username=data["username"],
        caption=data["caption"],
        created_at=datetime.fromisoformat(data["created_at"]),
        permalink=data["permalink"],
        media_items=[MediaItem(**item) for item in data["media_items"]],
    )


class InstagramToDiscordBridge:
    def __init__(
        self,
        instagram_username: str,
        discord_webhook_url: str,
        state_file: Path,
        force_post: bool,
        dry_run: bool,
        max_media_files: int,
        max_download_mb: int,
        skip_on_fetch_errors: bool,
        fetch_timeout_seconds: int,
        apify_api_token: str | None,
        provider_order: list[str],
    ) -> None:
        self.instagram_username = instagram_username
        self.discord_webhook_url = discord_webhook_url
        self.state_file = state_file
        self.force_post = force_post
        self.dry_run = dry_run
        self.max_media_files = max_media_files
        self.max_download_bytes = max_download_mb * 1024 * 1024
        self.skip_on_fetch_errors = skip_on_fetch_errors
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.apify_api_token = apify_api_token
        self.provider_order = provider_order

    def _fetch_latest(self) -> InstagramPost:
        errors: list[str] = []
        for provider in self.provider_order:
            if provider == "apify" and not self.apify_api_token:
                errors.append("apify skipped (missing APIFY_API_TOKEN)")
                continue
            try:
                logging.info("Trying provider: %s", provider)
                return _run_provider_with_timeout(
                    provider,
                    self.instagram_username,
                    self.apify_api_token,
                    self.fetch_timeout_seconds,
                )
            except Exception as err:
                errors.append(f"{provider}: {err}")
                logging.warning("Provider %s failed: %s", provider, err)

        raise RuntimeError("All providers failed. " + " | ".join(errors))

    def run(self) -> int:
        try:
            latest = self._fetch_latest()
        except Exception as err:
            if self.skip_on_fetch_errors:
                logging.warning("Skipping this run due to fetch error: %s", err)
                return 0
            raise

        last = self._load_last_shortcode()
        if not self.force_post and last == latest.shortcode:
            logging.info("No new post detected. Latest shortcode: %s", latest.shortcode)
            return 0

        logging.info("Will post shortcode=%s force_post=%s", latest.shortcode, self.force_post)

        if self.dry_run:
            logging.info("Dry run enabled. Would post permalink=%s", latest.permalink)
            self._save_last_shortcode(latest.shortcode)
            logging.info("Dry run: updated state with shortcode %s", latest.shortcode)
            return 0

        with tempfile.TemporaryDirectory(prefix="instagram_media_") as tmp_dir:
            attachments = self._download_media(latest.media_items, Path(tmp_dir))
            self._send_to_discord(latest, attachments)

        self._save_last_shortcode(latest.shortcode)
        logging.info("Posted shortcode %s and updated state.", latest.shortcode)
        return 0

    def _download_media(self, media_items: Iterable[MediaItem], output_dir: Path) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        for idx, media in enumerate(media_items):
            if len(files) >= self.max_media_files:
                break
            try:
                resp = requests.get(media.url, stream=True, timeout=30)
                resp.raise_for_status()
                size = resp.headers.get("content-length")
                if size and int(size) > self.max_download_bytes:
                    continue
                ext = self._guess_extension(media.url, resp.headers.get("content-type"))
                fp = output_dir / f"media_{idx + 1}{ext}"

                written = 0
                with fp.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        written += len(chunk)
                        if written > self.max_download_bytes:
                            fp.unlink(missing_ok=True)
                            break
                    else:
                        files.append(fp)
            except requests.RequestException:
                continue
        return files

    @staticmethod
    def _guess_extension(url: str, content_type: str | None) -> str:
        suffix = Path(urlparse(url).path).suffix
        if suffix:
            return suffix
        if content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if guessed:
                return guessed
        return ".bin"

    def _post_with_retries(self, *, data=None, json_payload=None, files=None, timeout=60) -> requests.Response:
        # Minimal retry handling for Discord rate limits / transient failures.
        for attempt in range(1, 4):
            resp = requests.post(self.discord_webhook_url, data=data, json=json_payload, files=files, timeout=timeout)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_s = float(retry_after) if retry_after else 2.0
                logging.warning("Discord rate limited (429). Sleeping %.2fs (attempt %d/3).", sleep_s, attempt)
                time.sleep(sleep_s)
                continue
            if 500 <= resp.status_code < 600:
                logging.warning("Discord server error (%s). Retrying (attempt %d/3).", resp.status_code, attempt)
                time.sleep(1.5 * attempt)
                continue
            return resp
        return resp

    def _send_to_discord(self, post: InstagramPost, attachments: list[Path]) -> None:
        # Discord embed description max is 4096; keep margin.
        desc = post.caption[:3900] if post.caption else "No caption provided."

        embed = {
            "title": f"New Instagram post from @{post.username}",
            "url": post.permalink,
            "description": desc,
            "timestamp": post.created_at.astimezone(timezone.utc).isoformat(),
            "footer": {"text": f"Shortcode: {post.shortcode}"},
        }

        if not attachments:
            first_image = next((x.url for x in post.media_items if not x.is_video), None)
            if first_image:
                embed["image"] = {"url": first_image}

        media_lines = [f"{i + 1}. {m.url}" for i, m in enumerate(post.media_items)]
        payload = {
            "content": f"📸 **Instagram update from @{post.username}**\nPost: {post.permalink}\n"
                       + ("Media links:\n" + "\n".join(media_lines) if media_lines else ""),
            "embeds": [embed],
            "allowed_mentions": {"parse": []},
        }

        if attachments:
            handles = []
            files = {}
            try:
                for i, path in enumerate(attachments):
                    h = path.open("rb")
                    handles.append(h)
                    files[f"files[{i}]"] = (path.name, h)

                resp = self._post_with_retries(
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                    timeout=60,
                )
            finally:
                for h in handles:
                    h.close()
        else:
            resp = self._post_with_retries(json_payload=payload, timeout=30)

        if resp.status_code >= 300:
            raise RuntimeError(f"Discord webhook failed ({resp.status_code}): {resp.text[:500]}")

    def _load_last_shortcode(self) -> str | None:
        if not self.state_file.exists():
            return None
        return self.state_file.read_text(encoding="utf-8").strip() or None

    def _save_last_shortcode(self, shortcode: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(shortcode, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instagram-username", default=os.getenv("INSTAGRAM_USERNAME", "HashtagUtd"))
    p.add_argument("--discord-webhook-url", default=os.getenv("DISCORD_WEBHOOK_URL"))
    p.add_argument("--state-file", type=Path, default=Path(os.getenv("STATE_FILE", ".cache/instagram_last_post.txt")))
    p.add_argument("--force-post", action="store_true", default=os.getenv("FORCE_POST", "false").lower() == "true")
    p.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "false").lower() == "true")
    p.add_argument("--max-media-files", type=int, default=int(os.getenv("MAX_MEDIA_FILES", "4")))
    p.add_argument("--max-download-mb", type=int, default=int(os.getenv("MAX_DOWNLOAD_MB", "8")))
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    p.add_argument("--skip-on-fetch-errors", action="store_true", default=os.getenv("SKIP_ON_FETCH_ERRORS", "true").lower() == "true")
    p.add_argument("--fetch-timeout-seconds", type=int, default=int(os.getenv("FETCH_TIMEOUT_SECONDS", "90")))
    p.add_argument("--apify-api-token", default=os.getenv("APIFY_API_TOKEN"))
    p.add_argument(
        "--provider-order",
        default=os.getenv("PROVIDER_ORDER", "apify,instaloader"),
        help="Comma-separated provider order. Supported: apify,instaloader",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.discord_webhook_url and not args.dry_run:
        raise SystemExit("DISCORD_WEBHOOK_URL is required unless --dry-run is used.")

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    providers = [x.strip() for x in args.provider_order.split(",") if x.strip()]
    for prov in providers:
        if prov not in {"apify", "instaloader"}:
            raise SystemExit(f"Unsupported provider in PROVIDER_ORDER: {prov}")

    bridge = InstagramToDiscordBridge(
        instagram_username=args.instagram_username,
        discord_webhook_url=args.discord_webhook_url,
        state_file=args.state_file,
        force_post=args.force_post,
        dry_run=args.dry_run,
        max_media_files=max(1, args.max_media_files),
        max_download_mb=max(1, args.max_download_mb),
        skip_on_fetch_errors=args.skip_on_fetch_errors,
        fetch_timeout_seconds=max(10, args.fetch_timeout_seconds),
        apify_api_token=args.apify_api_token,
        provider_order=providers,
    )
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
