#!/usr/bin/env python3
"""Fetch the latest Instagram post from a public profile and publish it to Discord."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import multiprocessing as mp
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import instaloader
import requests


@dataclass
class MediaItem:
    """Represents one media object from an Instagram post."""

    url: str
    is_video: bool


@dataclass
class InstagramPost:
    """A simplified model for a fetched Instagram post."""

    shortcode: str
    username: str
    caption: str
    created_at: datetime
    permalink: str
    media_items: list[MediaItem]


def fetch_latest_post_via_instaloader(instagram_username: str) -> InstagramPost:
    """Fetch latest post metadata from a public Instagram profile."""
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

    media_items: list[MediaItem] = []
    if latest.typename == "GraphSidecar":
        for node in latest.get_sidecar_nodes():
            media_items.append(MediaItem(url=node.video_url or node.display_url, is_video=node.is_video))
    else:
        media_items.append(MediaItem(url=latest.video_url or latest.url, is_video=latest.is_video))

    return InstagramPost(
        shortcode=latest.shortcode,
        username=instagram_username,
        caption=(latest.caption or "").strip(),
        created_at=latest.date_utc.replace(tzinfo=timezone.utc),
        permalink=f"https://www.instagram.com/p/{latest.shortcode}/",
        media_items=media_items,
    )


def _fetch_worker(instagram_username: str, queue: mp.Queue) -> None:
    """Worker process used to isolate and timeout potential Instaloader rate-limit waits."""
    try:
        post = fetch_latest_post_via_instaloader(instagram_username)
        queue.put(("ok", _serialize_post(post)))
    except Exception as err:  # pragma: no cover - remote behavior
        queue.put(("error", str(err)))


def _serialize_post(post: InstagramPost) -> dict:
    data = asdict(post)
    data["created_at"] = post.created_at.isoformat()
    return data


def _deserialize_post(data: dict) -> InstagramPost:
    return InstagramPost(
        shortcode=data["shortcode"],
        username=data["username"],
        caption=data["caption"],
        created_at=datetime.fromisoformat(data["created_at"]),
        permalink=data["permalink"],
        media_items=[MediaItem(**item) for item in data["media_items"]],
    )


class InstagramToDiscordBridge:
    """Coordinates state handling, Instagram fetch and Discord webhook posting."""

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

    def run(self) -> int:
        """Run the bridge once. Returns 0 on success and non-zero on failure."""
        try:
            latest_post = self._fetch_latest_post_with_timeout()
        except Exception as err:  # pragma: no cover - depends on remote Instagram behavior
            if self.skip_on_fetch_errors:
                # Instagram often returns HTTP 429 for anonymous traffic from cloud IP ranges.
                # We treat these fetch errors as transient and skip this run.
                logging.warning("Skipping this run due to fetch error: %s", err)
                return 0
            raise

        previous_shortcode = self._load_last_shortcode()
        if not self.force_post and previous_shortcode == latest_post.shortcode:
            logging.info("No new post detected. Latest shortcode: %s", latest_post.shortcode)
            return 0

        if self.dry_run:
            logging.info("Dry run enabled. Would post shortcode %s", latest_post.shortcode)
            return 0

        with tempfile.TemporaryDirectory(prefix="instagram_media_") as tmp_dir:
            attachments = self._download_media(latest_post.media_items, Path(tmp_dir))
            self._send_to_discord(latest_post, attachments)

        self._save_last_shortcode(latest_post.shortcode)
        logging.info("Posted shortcode %s and updated state.", latest_post.shortcode)
        return 0

    def _fetch_latest_post_with_timeout(self) -> InstagramPost:
        """Fetch in a subprocess so we can hard-timeout Instaloader 429 wait loops."""
        context = mp.get_context("spawn")
        queue: mp.Queue = context.Queue()
        process = context.Process(target=_fetch_worker, args=(self.instagram_username, queue))
        process.start()
        process.join(timeout=self.fetch_timeout_seconds)

        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            raise TimeoutError(
                f"Instagram fetch exceeded timeout ({self.fetch_timeout_seconds}s), likely due to rate limit wait."
            )

        if queue.empty():
            raise RuntimeError(f"Instagram fetch process exited without result (exit_code={process.exitcode}).")

        status, payload = queue.get()
        if status == "ok":
            return _deserialize_post(payload)
        raise RuntimeError(payload)

    def _download_media(self, media_items: Iterable[MediaItem], output_dir: Path) -> list[Path]:
        """Download media files so Discord can show attachments directly where possible."""
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_files: list[Path] = []

        for index, media in enumerate(media_items):
            if len(saved_files) >= self.max_media_files:
                logging.info("Reached max_media_files=%s; skipping remaining media.", self.max_media_files)
                break
            try:
                response = requests.get(media.url, stream=True, timeout=30)
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self.max_download_bytes:
                    logging.warning("Skipping media #%s due to size limit (%s bytes).", index + 1, content_length)
                    continue

                ext = self._guess_extension(media.url, response.headers.get("content-type"))
                filename = output_dir / f"media_{index + 1}{ext}"

                bytes_written = 0
                with filename.open("wb") as file_obj:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        file_obj.write(chunk)
                        bytes_written += len(chunk)
                        if bytes_written > self.max_download_bytes:
                            logging.warning("Skipping media #%s because streamed size exceeded limit.", index + 1)
                            filename.unlink(missing_ok=True)
                            break
                    else:
                        saved_files.append(filename)
            except requests.RequestException as err:
                logging.warning("Failed to download media #%s (%s): %s", index + 1, media.url, err)

        return saved_files

    @staticmethod
    def _guess_extension(url: str, content_type: str | None) -> str:
        parsed = urlparse(url)
        if Path(parsed.path).suffix:
            return Path(parsed.path).suffix
        if content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if guessed:
                return guessed
        return ".bin"

    def _send_to_discord(self, post: InstagramPost, attachments: list[Path]) -> None:
        """Post message + optional attachments to Discord webhook."""
        embed = {
            "title": f"New Instagram post from @{post.username}",
            "url": post.permalink,
            "description": post.caption[:3900] if post.caption else "No caption provided.",
            "timestamp": post.created_at.astimezone(timezone.utc).isoformat(),
            "footer": {"text": f"Shortcode: {post.shortcode}"},
        }

        if not attachments:
            first_image = next((item.url for item in post.media_items if not item.is_video), None)
            if first_image:
                embed["image"] = {"url": first_image}

        media_lines = [f"{idx + 1}. {item.url}" for idx, item in enumerate(post.media_items)]
        payload = {
            "content": (
                f"📸 **Instagram update from @{post.username}**\n"
                f"Post: {post.permalink}\n"
                "Media links:\n" + "\n".join(media_lines)
            ),
            "embeds": [embed],
            "allowed_mentions": {"parse": []},
        }

        if attachments:
            files = {}
            handles = []
            try:
                for idx, path in enumerate(attachments):
                    handle = path.open("rb")
                    handles.append(handle)
                    files[f"files[{idx}]"] = (path.name, handle)
                response = requests.post(
                    self.discord_webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                    timeout=60,
                )
            finally:
                for handle in handles:
                    handle.close()
        else:
            response = requests.post(self.discord_webhook_url, json=payload, timeout=30)

        if response.status_code >= 300:
            raise RuntimeError(f"Discord webhook failed ({response.status_code}): {response.text}")

    def _load_last_shortcode(self) -> str | None:
        if not self.state_file.exists():
            return None
        return self.state_file.read_text(encoding="utf-8").strip() or None

    def _save_last_shortcode(self, shortcode: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(shortcode, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instagram-username", default=os.getenv("INSTAGRAM_USERNAME", "HashtagUtd"))
    parser.add_argument("--discord-webhook-url", default=os.getenv("DISCORD_WEBHOOK_URL"))
    parser.add_argument("--state-file", type=Path, default=Path(os.getenv("STATE_FILE", ".cache/instagram_last_post.txt")))
    parser.add_argument("--force-post", action="store_true", default=os.getenv("FORCE_POST", "false").lower() == "true")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "false").lower() == "true")
    parser.add_argument("--max-media-files", type=int, default=int(os.getenv("MAX_MEDIA_FILES", "4")))
    parser.add_argument("--max-download-mb", type=int, default=int(os.getenv("MAX_DOWNLOAD_MB", "20")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--skip-on-fetch-errors",
        action="store_true",
        default=os.getenv("SKIP_ON_FETCH_ERRORS", "true").lower() == "true",
        help="Return success if Instagram fetch fails (useful for temporary 429 rate limits).",
    )
    parser.add_argument(
        "--fetch-timeout-seconds",
        type=int,
        default=int(os.getenv("FETCH_TIMEOUT_SECONDS", "90")),
        help="Hard timeout for Instagram fetch to prevent 30-minute Instaloader waits.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.discord_webhook_url and not args.dry_run:
        raise SystemExit("DISCORD_WEBHOOK_URL is required unless --dry-run is used.")

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

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
    )
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
