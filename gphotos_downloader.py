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
import time
import tempfile
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Union

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


def resolve_visual_filter(filter_val: Any) -> Optional[str]:
    """Resolve boolean or string into a filter key ('sepia', 'bw', 'vintage', or None)."""
    if isinstance(filter_val, bool):
        return "sepia" if filter_val else None
    if isinstance(filter_val, str):
        v = filter_val.strip().lower()
        if v in ("false", "none", "off", "no", "0", ""):
            return None
        if v in ("true", "yes", "on", "1", "sepia"):
            return "sepia"
        if v in ("bw", "b&w", "black_and_white", "grayscale", "gray"):
            return "bw"
        if v in ("vintage", "nostalgic"):
            return "vintage"
    return None


class GPhotosAlbumDownloader:
    def __init__(
        self,
        album_url: str,
        output_dir: str | Path,
        transcode_to_mp4: bool = True,
        normalize_audio: bool = True,
        visual_filter: Any = True,
        target_intro_name: str = "intro.mp4",
        max_workers: int = 3,
        ffmpeg_path: str = "ffmpeg",
    ):
        self.album_url = album_url
        self.output_dir = Path(output_dir)
        self.transcode_to_mp4 = transcode_to_mp4
        self.normalize_audio = normalize_audio
        self.visual_filter = resolve_visual_filter(visual_filter)
        self.target_intro_name = target_intro_name
        self.max_workers = max(1, int(max_workers or 3))
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

    def get_video_dimensions(self, file_path: Path) -> Tuple[Optional[int], Optional[int]]:
        """Extract effective width and height accounting for rotation metadata."""
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,side_data_list:stream_tags=rotate",
            "-of", "json",
            str(file_path),
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            data = json.loads(res.stdout)
            stream = data["streams"][0]
            w = int(stream["width"])
            h = int(stream["height"])
            rotate = 0
            if "tags" in stream and "rotate" in stream["tags"]:
                try:
                    rotate = int(stream["tags"]["rotate"])
                except ValueError:
                    pass
            if "side_data_list" in stream:
                for sd in stream["side_data_list"]:
                    if "rotation" in sd:
                        try:
                            rotate = int(sd["rotation"])
                        except ValueError:
                            pass
            if rotate in (90, 270, -90, -270):
                w, h = h, w
            return w, h
        except Exception as e:
            logger.debug("Could not probe dimensions for %s: %s", file_path.name, e)
            return None, None

    def probe_video_details(self, target: str | Path, is_url: bool = False) -> Dict[str, Any]:
        """Probe video stream for dimensions, SAR, DAR, and rotation."""
        cmd = ["ffprobe", "-v", "error"]
        if is_url:
            cmd += ["-headers", f"User-Agent: {USER_AGENT}\r\n"]
        cmd += [
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,sample_aspect_ratio,display_aspect_ratio:stream_tags=rotate:side_data_list",
            "-of", "json",
            str(target),
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            data = json.loads(res.stdout)
            stream = data["streams"][0]
            w = int(stream["width"])
            h = int(stream["height"])
            sar = stream.get("sample_aspect_ratio", "1:1")
            dar = stream.get("display_aspect_ratio")
            rot = 0
            if "tags" in stream and "rotate" in stream["tags"]:
                try:
                    rot = int(stream["tags"]["rotate"])
                except Exception:
                    pass
            if "side_data_list" in stream:
                for sd in stream["side_data_list"]:
                    if "rotation" in sd:
                        try:
                            rot = int(sd["rotation"])
                        except Exception:
                            pass
            eff_w, eff_h = (h, w) if rot in (90, 270, -90, -270) else (w, h)
            return {
                "width": w,
                "height": h,
                "effective_width": eff_w,
                "effective_height": eff_h,
                "sar": sar,
                "dar": dar,
                "rotation": rot,
            }
        except Exception as e:
            return {"error": str(e)}

    def compare_resolutions(self, limit: Optional[int] = None) -> None:
        """Fetch video metadata from Google Photos and compare side-by-side with local transcoded files."""
        html, _ = self.fetch_album_page()
        base_urls = self.extract_media_urls(html)
        if not base_urls:
            logger.warning("No media items found in Google Photos album.")
            return

        if limit:
            base_urls = base_urls[:limit]

        # Check intro state
        intro_state_file = self.output_dir / ".intro_state.json"
        current_intro_orig = None
        if intro_state_file.exists():
            try:
                with open(intro_state_file, "r", encoding="utf-8") as f:
                    current_intro_orig = json.load(f).get("current_intro_original_name")
            except Exception:
                pass

        print("\n" + "=" * 95)
        print("GOOGLE PHOTOS ALBUM vs. LOCAL TRANSCODED VIDEOS COMPARISON")
        print("=" * 95)
        print(f"{'ORIGINAL FILENAME':<38} {'GOOGLE PHOTOS':<22} {'LOCAL MP4':<18} {'STATUS'}")
        print("-" * 95)

        for idx, base_url in enumerate(base_urls, start=1):
            info = self.get_media_info(base_url)
            if not info:
                continue
            content_type = info.get("content_type", "").lower()
            orig_filename = info.get("filename", f"clip_{idx}.mp4")
            download_url = info.get("download_url")

            is_video = "video" in content_type or orig_filename.lower().endswith(
                (".mp4", ".mov", ".wmv", ".mkv", ".avi", ".m4v", ".webm", ".flv", ".3gp")
            )
            if not is_video:
                continue

            stem = Path(orig_filename).stem
            local_name = f"{stem}.mp4"
            local_path = self.output_dir / local_name

            if not local_path.exists():
                media_id = base_url.split("/pw/")[-1]
                alt_path = self.output_dir / f"{stem}_{media_id[:6]}.mp4"
                if alt_path.exists():
                    local_path = alt_path
                    local_name = alt_path.name
                elif current_intro_orig and (current_intro_orig == local_name or current_intro_orig == orig_filename):
                    intro_path = self.output_dir / "intro.mp4"
                    if intro_path.exists():
                        local_path = intro_path
                        local_name = "intro.mp4"

            # Probe remote Google Photos file
            remote_probe = self.probe_video_details(download_url, is_url=True)
            if "error" in remote_probe:
                remote_str = "Probe failed"
                rem_w, rem_h = None, None
            else:
                rem_w = remote_probe["effective_width"]
                rem_h = remote_probe["effective_height"]
                rot = remote_probe.get("rotation", 0)
                rot_str = f" [rot {rot}°]" if rot else ""
                remote_str = f"{rem_w}x{rem_h}{rot_str}"

            # Probe local file
            if local_path.exists():
                local_probe = self.probe_video_details(local_path, is_url=False)
                if "error" in local_probe:
                    local_str = "Corrupt"
                    loc_w, loc_h = None, None
                else:
                    loc_w = local_probe["effective_width"]
                    loc_h = local_probe["effective_height"]
                    local_str = f"{loc_w}x{loc_h}"
            else:
                local_str = "Not Downloaded"
                loc_w, loc_h = None, None

            # Determine status
            if rem_w and rem_h and loc_w and loc_h:
                if (loc_w, loc_h) == (1920, 1080):
                    status = "OK (1920x1080 16:9 canvas)"
                elif (rem_w, rem_h) == (loc_w, loc_h):
                    status = "EXACT MATCH"
                elif abs(rem_w - loc_w) <= 2 and abs(rem_h - loc_h) <= 2:
                    status = "MATCH (even rounded)"
                else:
                    status = f"DIFF: {rem_w}x{rem_h}->{loc_w}x{loc_h}"
            elif not local_path.exists():
                status = "MISSING LOCALLY"
            else:
                status = "UNKNOWN"

            display_name = orig_filename if len(orig_filename) <= 36 else orig_filename[:33] + "..."
            print(f"{display_name:<38} {remote_str:<22} {local_str:<18} {status}")

        print("-" * 95 + "\n")

    def is_standard_16_9(self, width: Optional[int], height: Optional[int]) -> bool:
        """Check if video is roughly 16:9 landscape (e.g. 1.777 aspect ratio)."""
        if not width or not height or height <= 0:
            return False
        # If height > width, it is portrait -> definitely not 16:9 landscape
        if height > width:
            return False
        aspect = width / height
        # Accept ratios between 1.75 and 1.80 as 16:9
        return 1.75 <= aspect <= 1.80 and width >= 1280

    def has_audio_stream(self, file_path: Path) -> bool:
        """Check if video file contains an audio stream."""
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(file_path),
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            return "audio" in res.stdout.lower()
        except Exception:
            return False

    def transcode_video_to_mp4(self, input_path: Path, output_path: Path) -> bool:
        """
        Transcode input video to standard 1920x1080 16:9 H.264/AAC MP4.
        - Pillarboxes/letterboxes portrait and 4:3 videos with black bars.
        - Applies a visual filter (sepia, bw / black & white, or vintage) if visual_filter is enabled.
        - Normalizes audio loudness using EBU R128 (loudnorm) to prevent clipping and quiet audio.
        """
        logger.info(
            "Transcoding & normalizing %s (preserving exact aspect ratio, visual_filter=%s, normalize_audio=%s) -> %s",
            input_path.name, self.visual_filter, self.normalize_audio, output_path.name
        )
        temp_output_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        temp_output = Path(temp_output_file.name)
        temp_output_file.close()

        # Build video filter chain
        vf_parts = []
        if self.visual_filter == "sepia":
            vf_parts.append("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")
        elif self.visual_filter == "bw":
            vf_parts.append("hue=s=0")
        elif self.visual_filter == "vintage":
            vf_parts.append("curves=vintage")

        # Preserve original aspect ratio inside 1920x1080 canvas with black bars:
        vf_parts.append("scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2")
        vf_parts.append("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black")
        vf_parts.append("setsar=1")
        filter_str = ",".join(vf_parts)

        has_audio = self.has_audio_stream(input_path)

        cmd = [self.ffmpeg_path, "-y", "-nostdin", "-i", str(input_path)]
        if not has_audio:
            # Generate silent audio track so Plex client players have consistent audio stream
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]

        cmd += [
            "-vf", filter_str,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
        ]

        if has_audio:
            if self.normalize_audio:
                cmd += ["-af", "dynaudnorm=f=150:g=15:m=10.0", "-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]

        cmd += ["-movflags", "+faststart", str(temp_output)]

        start_t = time.time()
        try:
            res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            elapsed = time.time() - start_t
            if res.returncode != 0:
                err_msg = res.stderr.decode("utf-8", errors="ignore")
                logger.error("FFmpeg transcoding failed for %s (after %.1fs): %s", input_path.name, elapsed, err_msg[-2000:])
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)
                return False

            shutil.move(str(temp_output), str(output_path))
            logger.info("Finished transcoding %s -> %s (took %.1fs)", input_path.name, output_path.name, elapsed)
            return True
        except (KeyboardInterrupt, SystemExit):
            if temp_output.exists():
                temp_output.unlink(missing_ok=True)
            raise
        except Exception as e:
            logger.error("Error executing ffmpeg: %s", e)
            if temp_output.exists():
                temp_output.unlink(missing_ok=True)
            return False

    def normalize_existing_videos(self, force: bool = False) -> int:
        """
        Inspect existing video files in output directory and ensure they are standard MP4s
        with audio normalized and visual filters applied (preserving exact original aspect ratio).
        """
        logger.info("Checking existing video files in %s (force=%s)...", self.output_dir, force)
        fixed_count = 0
        for p in list(self.output_dir.iterdir()):
            if not p.is_file() or p.name.startswith(".") or p.name.endswith(".transcoding.mp4"):
                continue
            if p.suffix.lower() not in (".mp4", ".mov", ".wmv", ".mkv", ".avi", ".webm", ".m4v"):
                continue

            w, h = self.get_video_dimensions(p)
            needs_normalization = force or (p.suffix.lower() != ".mp4") or (w != 1920 or h != 1080)

            if needs_normalization:
                logger.info("Processing: %s (%sx%s)...", p.name, w or '?', h or '?')
                backup_temp = p.with_suffix(".orig_fix" + p.suffix)
                p.rename(backup_temp)
                target_mp4 = p.with_suffix(".mp4")
                success = self.transcode_video_to_mp4(backup_temp, target_mp4)
                if success:
                    backup_temp.unlink(missing_ok=True)
                    fixed_count += 1
                else:
                    logger.error("Failed to process %s. Restoring original.", p.name)
                    backup_temp.rename(p)
        if fixed_count > 0:
            logger.info("Successfully processed %d existing videos.", fixed_count)
        return fixed_count

    def sync_album(
        self,
        dry_run: bool = False,
        force: bool = False,
        clean: bool = False,
        workers: Optional[int] = None,
    ) -> List[Path]:
        """
        Run one-way sync. Downloads video clips from Google Photos, transcodes if necessary,
        and records downloaded items in the manifest.
        Supports multi-threaded parallel downloads & transcoding (default: 3 workers).
        If force=True, re-downloads all clips and replaces existing files in the directory.
        If clean=True, removes existing video files in output_dir prior to downloading.
        Returns list of newly downloaded/transcoded video paths.
        """
        html, _ = self.fetch_album_page()
        base_urls = self.extract_media_urls(html)
        if not base_urls:
            logger.warning("No media items found in Google Photos album.")
            return []

        if clean and not dry_run:
            logger.info("Clean option enabled. Removing existing video files in %s...", self.output_dir)
            for item in self.output_dir.iterdir():
                if item.is_file() and not item.name.startswith(".") and item.suffix.lower() in (
                    ".mp4", ".mov", ".wmv", ".mkv", ".avi", ".m4v", ".webm"
                ):
                    try:
                        item.unlink()
                        logger.debug("Removed existing file: %s", item.name)
                    except Exception as e:
                        logger.warning("Could not remove %s: %s", item.name, e)
            self.manifest = {}
            if self.manifest_path.exists():
                try:
                    self.manifest_path.unlink()
                except Exception:
                    pass
            intro_state_path = self.output_dir / ".intro_state.json"
            if intro_state_path.exists():
                try:
                    intro_state_path.unlink()
                except Exception:
                    pass

        intro_target_path = self.output_dir / self.target_intro_name
        intro_state_file = self.output_dir / ".intro_state.json"
        current_intro_orig = None
        if intro_state_file.exists():
            try:
                with open(intro_state_file, "r", encoding="utf-8") as f:
                    current_intro_orig = json.load(f).get("current_intro_original_name")
            except Exception:
                pass

        num_workers = max(1, workers if workers is not None else self.max_workers)
        logger.info("Syncing %d album items using %d worker thread(s)...", len(base_urls), num_workers)

        # Thread synchronization locks
        manifest_lock = threading.Lock()
        intro_lock = threading.Lock()
        claimed_names_lock = threading.Lock()
        claimed_names: set = set()
        first_intro_claimed = [False]
        new_files: List[Path] = []
        new_files_lock = threading.Lock()

        def process_item(idx: int, base_url: str) -> Optional[Path]:
            media_id = base_url.split("/pw/")[-1]
            with manifest_lock:
                if not force and media_id in self.manifest:
                    existing_record = self.manifest[media_id]
                    target_filename = existing_record.get("final_filename")
                    file_present = False
                    if target_filename:
                        if (self.output_dir / target_filename).exists():
                            file_present = True
                        elif intro_target_path.exists() and current_intro_orig == target_filename:
                            file_present = True
                    if file_present:
                        logger.debug("[%d/%d] Item %s already downloaded as %s. Skipping.", idx, len(base_urls), media_id[:12], target_filename)
                        return None

            info = self.get_media_info(base_url)
            if not info:
                logger.warning("[%d/%d] Could not retrieve media info for %s", idx, len(base_urls), base_url)
                return None

            content_type = info.get("content_type", "").lower()
            orig_filename = info.get("filename", f"clip_{idx}.mp4")
            download_url = info.get("download_url")

            # Check if it's a video
            is_video = "video" in content_type or orig_filename.lower().endswith(
                (".mp4", ".mov", ".wmv", ".mkv", ".avi", ".m4v", ".webm", ".flv", ".3gp")
            )
            if not is_video:
                logger.info("[%d/%d] Skipping non-video item: %s (%s)", idx, len(base_urls), orig_filename, content_type)
                return None

            # Determine final filename
            stem = Path(orig_filename).stem
            is_already_mp4 = orig_filename.lower().endswith(".mp4")
            if self.transcode_to_mp4:
                final_filename = f"{stem}.mp4"
            else:
                final_filename = orig_filename

            with claimed_names_lock:
                if final_filename in claimed_names:
                    final_filename = f"{stem}_{media_id[:6]}.mp4"
                claimed_names.add(final_filename)

            logger.info("[%d/%d] Downloading: %s (Original: %s)", idx, len(base_urls), final_filename, orig_filename)

            if dry_run:
                logger.info("[DRY RUN] Would download and sync: %s", final_filename)
                return None

            with tempfile.NamedTemporaryFile(delete=False, suffix=Path(orig_filename).suffix) as tmp_f:
                temp_download_path = Path(tmp_f.name)

            try:
                dl_req = urllib.request.Request(download_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(dl_req, timeout=120) as resp, open(temp_download_path, "wb") as out_f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out_f.write(chunk)

                # Check if transcoding/normalization is needed
                w, h = self.get_video_dimensions(temp_download_path)
                needs_transcode = (
                    force
                    or not is_already_mp4
                    or (w != 1920 or h != 1080)
                    or self.visual_filter is not None
                    or self.normalize_audio
                )

                temp_transcode_path = None
                if self.transcode_to_mp4 and needs_transcode:
                    logger.info("Transcoding/normalizing [%d/%d] %s (%s, %sx%s)...", idx, len(base_urls), orig_filename, content_type, w or '?', h or '?')
                    temp_transcode_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                    temp_transcode_path = Path(temp_transcode_file.name)
                    temp_transcode_file.close()

                    success = self.transcode_video_to_mp4(temp_download_path, temp_transcode_path)
                    temp_download_path.unlink(missing_ok=True)
                    if not success:
                        if temp_transcode_path.exists():
                            temp_transcode_path.unlink(missing_ok=True)
                        logger.error("Failed to transcode %s; skipping.", orig_filename)
                        return None
                    source_for_dest = temp_transcode_path
                else:
                    source_for_dest = temp_download_path

                with intro_lock:
                    is_active_intro = intro_target_path.exists() and current_intro_orig == final_filename
                    is_first_intro = not intro_target_path.exists() and not first_intro_claimed[0]

                    if is_active_intro or is_first_intro:
                        dest_path = intro_target_path
                        if is_first_intro:
                            first_intro_claimed[0] = True
                    else:
                        dest_path = self.output_dir / final_filename

                    shutil.move(str(source_for_dest), str(dest_path))

                    if is_first_intro:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        state_data = {
                            "current_intro_original_name": final_filename,
                            "last_index": 0,
                            "last_rotated_at": now_iso,
                            "history": [{"name": final_filename, "rotated_at": now_iso}],
                        }
                        try:
                            with open(intro_state_file, "w", encoding="utf-8") as f:
                                json.dump(state_data, f, indent=2)
                        except Exception as e:
                            logger.warning("Could not write initial intro state: %s", e)
                        logger.info("Activated first completed video '%s' immediately as '%s' for Plex!", final_filename, self.target_intro_name)

                # Record in manifest
                with manifest_lock:
                    self.manifest[media_id] = {
                        "original_filename": orig_filename,
                        "final_filename": final_filename,
                        "content_type": content_type,
                        "size_bytes": dest_path.stat().st_size,
                        "synced_at": urllib.parse.quote(str(os.path.getmtime(dest_path))),
                    }
                    self._save_manifest()

                with new_files_lock:
                    new_files.append(dest_path)

                logger.info("Saved [%d/%d]: %s (%d bytes)", idx, len(base_urls), dest_path.name, dest_path.stat().st_size)
                return dest_path

            except (KeyboardInterrupt, SystemExit):
                if temp_download_path.exists():
                    temp_download_path.unlink(missing_ok=True)
                raise
            except Exception as e:
                logger.error("Failed processing %s: %s", orig_filename, e)
                if temp_download_path.exists():
                    temp_download_path.unlink(missing_ok=True)
                return None

        if num_workers <= 1:
            for idx, base_url in enumerate(base_urls, start=1):
                process_item(idx, base_url)
        else:
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(process_item, idx, base_url) for idx, base_url in enumerate(base_urls, start=1)]
                try:
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except (KeyboardInterrupt, SystemExit):
                            for f in futures:
                                f.cancel()
                            raise
                        except Exception as e:
                            logger.error("Worker error: %s", e)
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise

        # Also inspect and normalize any existing videos already in directory
        if self.transcode_to_mp4 and not dry_run:
            self.normalize_existing_videos()

        logger.info("Sync complete. %d new/updated videos downloaded.", len(new_files))
        return new_files
