"""
Module for scraping and downloading videos from a public Google Photos shared album.
Handles pagination/extraction, download manifests, and transcoding non-mp4 videos using ffmpeg.
"""

import os
import re
import json
import logging
import urllib.request
import urllib.parse
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger("plex_intro_randomizer.downloader")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MANIFEST_FILENAME = ".download_manifest.json"


def sanitize_filename(name: str) -> str:
    """Sanitize filename to avoid path traversal or illegal characters."""
    base_name = Path(name).name
    cleaned = re.sub(r'[\\/*?:"<>|\0]', "_", base_name)
    cleaned = cleaned.strip().strip(".")
    return cleaned if cleaned else "video"


class GPhotosAlbumDownloader:
    def __init__(
        self,
        album_url: str,
        output_dir: str | Path,
        transcode_to_mp4: bool = True,
        ffmpeg_path: str = "ffmpeg",
    ):
        self.album_url = album_url
        self.output_dir = Path(output_dir)
        self.transcode_to_mp4 = transcode_to_mp4
        self.ffmpeg_path = ffmpeg_path
        self.manifest_path = self.output_dir / MANIFEST_FILENAME
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> Dict[str, dict]:
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Could not read existing manifest: %s. Starting fresh.", e)
        return {}

    def _save_manifest(self) -> None:
        temp_file = self.manifest_path.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, indent=2)
            temp_file.replace(self.manifest_path)
        except Exception as e:
            logger.error("Failed to save manifest: %s", e)
            if temp_file.exists():
                temp_file.unlink()

    def fetch_album_page(self) -> Tuple[str, str]:
        """Fetch the Google Photos shared album page HTML and return (html, final_url)."""
        logger.info("Fetching Google Photos shared album from %s", self.album_url)
        req = urllib.request.Request(self.album_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            final_url = resp.geturl()
            html = resp.read().decode("utf-8", errors="replace")
        return html, final_url

    def extract_media_urls(self, html: str) -> List[str]:
        """Extract unique Google Photos media base URLs (https://lh3.googleusercontent.com/pw/...)."""
        pattern = r"https://lh3\.googleusercontent\.com/pw/[a-zA-Z0-9_\-]+"
        matches = re.findall(pattern, html)
        # Preserve order while deduplicating
        seen = set()
        unique_urls = []
        for u in matches:
            if u not in seen:
                seen.add(u)
                unique_urls.append(u)
        logger.info("Discovered %d media items in album.", len(unique_urls))
        return unique_urls

    def get_media_info(self, base_url: str) -> Optional[Dict[str, str]]:
        """
        Probe Google Photos base URL with =dv parameter to determine if it is a video,
        its content type, content length, and original filename.
        """
        download_url = base_url + "=dv"
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": USER_AGENT},
            method="HEAD",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                headers = resp.headers
                content_type = headers.get("Content-Type", "")
                content_disposition = headers.get("Content-Disposition", "")
                content_length = headers.get("Content-Length", "0")

                filename = None
                if content_disposition:
                    fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', content_disposition)
                    if fn_match:
                        filename = urllib.parse.unquote(fn_match.group(1)).strip().strip('"')

                if not filename or not content_type:
                    return self._probe_media_via_get_range(download_url)

                return {
                    "download_url": download_url,
                    "content_type": content_type,
                    "filename": sanitize_filename(filename or "video.mp4"),
                    "size": int(content_length) if content_length.isdigit() else 0,
                }
        except Exception:
            return self._probe_media_via_get_range(download_url)

    def _probe_media_via_get_range(self, download_url: str) -> Optional[Dict[str, str]]:
        """Probe using a 1-byte GET request if HEAD is rejected."""
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                headers = resp.headers
                content_type = headers.get("Content-Type", "")
                content_disposition = headers.get("Content-Disposition", "")
                filename = None
                if content_disposition:
                    fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', content_disposition)
                    if fn_match:
                        filename = urllib.parse.unquote(fn_match.group(1)).strip().strip('"')

                if not filename:
                    ext = ".mp4" if "mp4" in content_type else ".mov"
                    filename = f"video_{hash(download_url) & 0xFFFFFFFF:08x}{ext}"

                return {
                    "download_url": download_url,
                    "content_type": content_type,
                    "filename": sanitize_filename(filename),
                    "size": 0,
                }
        except Exception as e:
            logger.debug("Failed to probe %s: %s", download_url, e)
            return None

    def transcode_video_to_mp4(self, input_path: Path, output_path: Path) -> bool:
        """
        Transcode input video to standard H.264/AAC MP4 playable by all Plex clients.
        Uses ffmpeg with -pix_fmt yuv420p and faststart for streaming/quick playback.
        """
        logger.info("Transcoding %s to MP4 (%s)...", input_path.name, output_path.name)
        temp_output = output_path.with_suffix(".transcoding.mp4")
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(temp_output),
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if res.returncode != 0:
                logger.error("FFmpeg transcoding failed for %s: %s", input_path.name, res.stderr.decode("utf-8", errors="ignore")[-400:])
                if temp_output.exists():
                    temp_output.unlink()
                return False

            temp_output.replace(output_path)
            logger.info("Successfully transcoded %s -> %s", input_path.name, output_path.name)
            return True
        except Exception as e:
            logger.error("Error executing ffmpeg: %s", e)
            if temp_output.exists():
                temp_output.unlink()
            return False

    def sync_album(self, dry_run: bool = False) -> List[Path]:
        """
        Run one-way sync. Downloads new video clips from Google Photos, transcodes if necessary,
        and records downloaded items in the manifest.
        Returns list of newly downloaded/transcoded video paths.
        """
        html, _ = self.fetch_album_page()
        base_urls = self.extract_media_urls(html)
        if not base_urls:
            logger.warning("No media items found in Google Photos album.")
            return []

        new_files: List[Path] = []

        for idx, base_url in enumerate(base_urls, start=1):
            media_id = base_url.split("/pw/")[-1]
            if media_id in self.manifest:
                existing_record = self.manifest[media_id]
                target_filename = existing_record.get("final_filename")
                if target_filename and (self.output_dir / target_filename).exists():
                    logger.debug("[%d/%d] Item %s already downloaded as %s. Skipping.", idx, len(base_urls), media_id[:12], target_filename)
                    continue

            info = self.get_media_info(base_url)
            if not info:
                logger.warning("[%d/%d] Could not retrieve media info for %s", idx, len(base_urls), base_url)
                continue

            content_type = info.get("content_type", "").lower()
            orig_filename = info.get("filename", f"clip_{idx}.mp4")
            download_url = info.get("download_url")

            # Check if it's a video
            is_video = "video" in content_type or orig_filename.lower().endswith(
                (".mp4", ".mov", ".wmv", ".mkv", ".avi", ".m4v", ".webm", ".flv", ".3gp")
            )
            if not is_video:
                logger.info("[%d/%d] Skipping non-video item: %s (%s)", idx, len(base_urls), orig_filename, content_type)
                continue

            # Determine final filename (always .mp4)
            stem = Path(orig_filename).stem
            is_already_mp4 = orig_filename.lower().endswith(".mp4")
            if self.transcode_to_mp4:
                final_filename = f"{stem}.mp4"
            else:
                final_filename = orig_filename

            # Ensure unique name in output directory if a different file already has that name
            dest_path = self.output_dir / final_filename
            if dest_path.exists() and media_id not in self.manifest:
                final_filename = f"{stem}_{media_id[:6]}.mp4"
                dest_path = self.output_dir / final_filename

            logger.info("[%d/%d] New video detected: %s (Original: %s)", idx, len(base_urls), final_filename, orig_filename)

            if dry_run:
                logger.info("[DRY RUN] Would download and sync: %s", final_filename)
                continue

            # Download to a temporary file first
            with tempfile.NamedTemporaryFile(delete=False, suffix=Path(orig_filename).suffix, dir=str(self.output_dir)) as tmp_f:
                temp_download_path = Path(tmp_f.name)

            try:
                dl_req = urllib.request.Request(download_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(dl_req, timeout=120) as resp, open(temp_download_path, "wb") as out_f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out_f.write(chunk)

                # Transcode if needed (or if source was not mp4)
                if self.transcode_to_mp4 and not is_already_mp4:
                    success = self.transcode_video_to_mp4(temp_download_path, dest_path)
                    temp_download_path.unlink(missing_ok=True)
                    if not success:
                        logger.error("Failed to transcode %s; skipping.", orig_filename)
                        continue
                else:
                    temp_download_path.replace(dest_path)

                # Record in manifest
                self.manifest[media_id] = {
                    "original_filename": orig_filename,
                    "final_filename": final_filename,
                    "content_type": content_type,
                    "size_bytes": dest_path.stat().st_size,
                    "synced_at": urllib.parse.quote(str(os.path.getmtime(dest_path))),
                }
                self._save_manifest()
                new_files.append(dest_path)
                logger.info("Saved: %s (%d bytes)", dest_path.name, dest_path.stat().st_size)

            except Exception as e:
                logger.error("Failed downloading %s: %s", orig_filename, e)
                if temp_download_path.exists():
                    temp_download_path.unlink(missing_ok=True)

        logger.info("Sync complete. %d new/updated videos downloaded.", len(new_files))
        return new_files
