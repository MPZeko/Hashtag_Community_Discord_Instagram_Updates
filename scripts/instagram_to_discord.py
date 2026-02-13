#!/usr/bin/env python3
"""Fetch the latest Instagram post from a public profile and publish it to Discord."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import tempfile
import time
from dataclasses import dataclass
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
    ) -> None:
        self.instagram_username = instagram_username
        self.discord_webhook_url = discord_webhook_url
        self.state_file = state_file
        self.force_post = force_post
        self.dry_run = dry_run
        self.max_media_files = max_media_files
        self.max_download_bytes = max_download_mb * 1024 * 1024
        self.skip_on_fetch_errors = skip_on_fetch_errors

    def run(self) -> int:
        """Run the bridge once. Returns 0 on success and non-zero on failure."""
        try:
            latest_post = self._fetch_latest_post()
        except Exception as err:  # pragma: no cover - depends on remote Instagram behavior
            if self.skip_on_fetch_errors:
                # Instagram can temporarily rate-limit anonymous requests (HTTP 429).
                # For scheduled automation we treat this as a transient condition and
                # exit successfully so the next schedule can retry instead of failing hard.
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
            attachment_paths = self._download_media(latest_post.media_items, Path(tmp_dir))
            self._send_to_discord(latest_post, attachment_paths)

        self._save_last_shortcode(latest_post.shortcode)
        logging.info("Posted shortcode %s and updated state.", latest_post.shortcode)
        return 0

    def _fetch_latest_post(self) -> InstagramPost:
        """Fetch the latest post from a public Instagram profile using Instaloader."""
        loader = instaloader.Instaloader(
            sleep=False,
            quiet=True,
            download_comments=False,
            download_geotags=False,
            download_video_thumbnails=False,
            save_metadata=False,
            compress_json=False,
        )

        profile = instaloader.Profile.from_username(loader.context, self.instagram_username)
        posts = profile.get_posts()

        # Small pause helps avoid immediately hammering the endpoint in edge cases.
        time.sleep(1)

        latest = next(posts, None)
        if latest is None:
            raise RuntimeError(f"No posts found for profile '{self.instagram_username}'.")

        media_items: list[MediaItem] = []
        if latest.typename == "GraphSidecar":
            for node in latest.get_sidecar_nodes():
                media_items.append(MediaItem(url=node.video_url or node.display_url, is_video=node.is_video))
        else:
            media_items.append(MediaItem(url=latest.video_url or latest.url, is_video=latest.is_video))

        created_at = latest.date_utc.replace(tzinfo=timezone.utc)
        permalink = f"https://www.instagram.com/p/{latest.shortcode}/"

        return InstagramPost(
            shortcode=latest.shortcode,
            username=self.instagram_username,
            caption=(latest.caption or "").strip(),
            created_at=created_at,
            permalink=permalink,
            media_items=media_items,
        )

    def _download_media(self, media_items: Iterable[MediaItem], output_dir: Path) -> list[Path]:
        """Download media files to local temp files so Discord can display attachments directly."""
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_files: list[Path] = []

        for index, media in enumerate(media_items):
            if len(saved_files) >= self.max_media_files:
                logging.info("Reached max_media_files=%s; skipping remaining media.", self.max_media_files)
                break

            try:
                response = requests.get(media.url, stream=True, timeout=30)
                response.raise_for_status()

                # Skip very large files to avoid Discord upload limits and long workflow times.
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self.max_download_bytes:
                    logging.warning("Skipping media #%s due to size limit (%s bytes).", index + 1, content_length)
                    continue

                guessed_extension = self._guess_extension(media.url, response.headers.get("content-type"))
                filename = output_dir / f"media_{index + 1}{guessed_extension}"

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
        """Infer a local extension for downloaded media."""
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix
        if suffix:
            return suffix

        if content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if guessed:
                return guessed

        return ".bin"

    def _send_to_discord(self, post: InstagramPost, attachments: list[Path]) -> None:
        """Post message + optional attachments to Discord webhook."""
        description = post.caption[:3900] if post.caption else "No caption provided."
        created_iso = post.created_at.astimezone(timezone.utc).isoformat()

        embed = {
            "title": f"New Instagram post from @{post.username}",
            "url": post.permalink,
            "description": description,
            "timestamp": created_iso,
            "footer": {"text": f"Shortcode: {post.shortcode}"},
        }

        # If we could not upload files, use the first image URL in the embed to preserve preview.
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
            opened_files = []
            try:
                for idx, path in enumerate(attachments):
                    file_handle = path.open("rb")
                    opened_files.append(file_handle)
                    files[f"files[{idx}]"] = (path.name, file_handle)
                response = requests.post(
                    self.discord_webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                    timeout=60,
                )
            finally:
                for handle in opened_files:
                    handle.close()
        else:
            response = requests.post(self.discord_webhook_url, json=payload, timeout=30)

        if response.status_code >= 300:
            raise RuntimeError(f"Discord webhook failed ({response.status_code}): {response.text}")

    def _load_last_shortcode(self) -> str | None:
        """Load the most recently posted Instagram shortcode from state file."""
        if not self.state_file.exists():
            return None
        return self.state_file.read_text(encoding="utf-8").strip() or None

    def _save_last_shortcode(self, shortcode: str) -> None:
        """Persist the latest shortcode so we can avoid duplicate Discord posts."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(shortcode, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """Parse command-line flags and environment variable defaults."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instagram-username",
        default=os.getenv("INSTAGRAM_USERNAME", "HashtagUtd"),
        help="Public Instagram username to monitor.",
    )
    parser.add_argument(
        "--discord-webhook-url",
        default=os.getenv("DISCORD_WEBHOOK_URL"),
        help="Discord webhook URL used for posting updates.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.getenv("STATE_FILE", ".cache/instagram_last_post.txt")),
        help="Path to state file that stores the last posted shortcode.",
    )
    parser.add_argument(
        "--force-post",
        action="store_true",
        default=os.getenv("FORCE_POST", "false").lower() == "true",
        help="Post even if the latest shortcode already exists in state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "false").lower() == "true",
        help="Fetch latest post but do not send anything to Discord.",
    )
    parser.add_argument(
        "--max-media-files",
        type=int,
        default=int(os.getenv("MAX_MEDIA_FILES", "4")),
        help="Maximum number of media files to upload to Discord.",
    )
    parser.add_argument(
        "--max-download-mb",
        type=int,
        default=int(os.getenv("MAX_DOWNLOAD_MB", "20")),
        help="Maximum size in MB per downloaded media file.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    parser.add_argument(
        "--skip-on-fetch-errors",
        action="store_true",
        default=os.getenv("SKIP_ON_FETCH_ERRORS", "true").lower() == "true",
        help="Return success if Instagram fetch fails (useful for temporary 429 rate limits).",
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
    )
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
