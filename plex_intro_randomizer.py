#!/usr/bin/env python3
"""
Plex Intro Randomizer & Google Photos Sync
Syncs video clips from a public Google Photos shared album and randomizes 'intro.mp4' on an hourly schedule.
"""

import sys
import os
import time
import json
import signal
import logging
import argparse
import tempfile
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from gphotos_downloader import GPhotosAlbumDownloader
from rotator import IntroRotator

DEFAULT_ALBUM_URL = None
DEFAULT_TARGET_INTRO = "intro.mp4"
DEFAULT_SYNC_INTERVAL_HOURS = 24
DEFAULT_ROTATE_INTERVAL_MINUTES = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("plex_intro_randomizer")


def load_config(config_path: Optional[str | Path]) -> Dict[str, Any]:
    """Load configuration from a JSON or YAML file if present."""
    if not config_path:
        default_cfg = Path("config.json")
        if default_cfg.exists():
            config_path = default_cfg
        else:
            return {}

    path = Path(config_path)
    if not path.exists():
        logger.warning("Config file %s does not exist. Using defaults and CLI arguments.", path)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            if path.suffix in [".yaml", ".yml"]:
                try:
                    import yaml  # type: ignore
                    return yaml.safe_load(f) or {}
                except ImportError:
                    logger.error("PyYAML is not installed to read YAML config. Install with 'pip install pyyaml'.")
                    return {}
            else:
                return json.load(f)
    except Exception as e:
        logger.error("Failed loading configuration from %s: %s", path, e)
        return {}


def run_sync(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Execute one-way sync from Google Photos album."""
    album_url = args.album_url or config.get("album_url") or DEFAULT_ALBUM_URL
    if not album_url:
        logger.error("Album URL is required. Specify with --album-url or in config.")
        sys.exit(1)
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)

    transcode = config.get("transcode_to_mp4", True) if args.transcode is None else args.transcode
    normalize_audio = config.get("normalize_audio", True) if args.normalize_audio is None else args.normalize_audio
    visual_filter = config.get("visual_filter", False)

    force = getattr(args, "force", False) or args.command in ["redownload", "force-download", "first-five", "first-5"]
    clean = getattr(args, "clean", False)
    limit = getattr(args, "count", None) or getattr(args, "limit", None) or getattr(args, "first", None)
    if args.command in ["first-five", "first-5"]:
        if limit is None:
            limit = 5
        force = True
        if getattr(args, "clean", None) is not False:
            clean = True

    target_intro = config.get("target_intro_name", DEFAULT_TARGET_INTRO)

    logger.info("Starting Google Photos album sync...")
    logger.info("Album URL: %s", album_url)
    logger.info("Target Directory: %s", video_dir)
    logger.info("Transcode non-MP4: %s", transcode)
    logger.info("Normalize Audio (EBU R128): %s", normalize_audio)
    logger.info("Visual Filter: %s", visual_filter)
    logger.info("Force Re-download / Replace All: %s", force)
    if clean:
        logger.info("Clean Directory Before Download: True")
    if limit:
        logger.info("Limit Download Count: %d videos", limit)

    rotator = IntroRotator(video_dir=video_dir, target_intro_name=target_intro)
    if force or clean:
        restored = rotator.restore_current_intro(dry_run=args.dry_run)
        if restored:
            logger.info("Restored active intro '%s' -> '%s' before downloading.", target_intro, restored)

    downloader = GPhotosAlbumDownloader(
        album_url=album_url,
        output_dir=video_dir,
        transcode_to_mp4=transcode,
        normalize_audio=normalize_audio,
        visual_filter=visual_filter,
        target_intro_name=target_intro,
    )
    new_files = downloader.sync_album(dry_run=args.dry_run, force=force, clean=clean, limit=limit)
    logger.info("Sync finished. %d items downloaded / updated.", len(new_files))

    if (force or clean) and not args.dry_run:
        intro_file = Path(video_dir) / target_intro
        if not intro_file.exists():
            logger.info("Selecting a fresh '%s' from downloaded videos...", target_intro)
            rotator.rotate()


def run_normalize(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Scan and normalize all existing video files in the directory to 16:9 MP4."""
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)

    normalize_audio = config.get("normalize_audio", True) if args.normalize_audio is None else args.normalize_audio
    visual_filter = config.get("visual_filter", False)

    logger.info("Scanning %s to fix aspect ratios, audio levels, and visual filters...", video_dir)
    logger.info("Visual Filter: %s | Normalize Audio: %s | Force All: %s", visual_filter, normalize_audio, args.force)

    downloader = GPhotosAlbumDownloader(
        album_url="https://photos.app.goo.gl/dummy",
        output_dir=video_dir,
        transcode_to_mp4=True,
        normalize_audio=normalize_audio,
        visual_filter=visual_filter,
    )
    fixed = downloader.normalize_existing_videos(force=args.force)
    logger.info("Normalization complete. %d files processed.", fixed)


def run_rotate(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Execute intro video rotation."""
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)

    target_intro = config.get("target_intro_name", DEFAULT_TARGET_INTRO)

    rotator = IntroRotator(video_dir=video_dir, target_intro_name=target_intro)
    res = rotator.rotate(dry_run=args.dry_run)
    if res:
        logger.info("Rotation completed successfully.")
        print(json.dumps(res, indent=2))
    else:
        logger.warning("Rotation could not be completed.")


def run_status(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Display current status of intros and directory."""
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)

    target_intro = config.get("target_intro_name", DEFAULT_TARGET_INTRO)
    rotator = IntroRotator(video_dir=video_dir, target_intro_name=target_intro)
    status = rotator.get_status()
    print(json.dumps(status, indent=2))


def run_compare(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Compare original Google Photos video resolutions with local transcoded files."""
    album_url = args.album_url or config.get("album_url") or DEFAULT_ALBUM_URL
    if not album_url:
        logger.error("Album URL is required. Specify with --album-url or in config.")
        sys.exit(1)
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)

    downloader = GPhotosAlbumDownloader(
        album_url=album_url,
        output_dir=video_dir,
    )
    downloader.compare_resolutions(limit=getattr(args, "limit", None))


def generate_calibration_video(output_path: Path, aspect: str = "16:9", duration_secs: int = 10) -> bool:
    """Generate an aspect ratio calibration card video with concentric circles and grid borders."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.error("Pillow (PIL) is required to generate calibration pattern. Install with: pip install pillow")
        return False

    if aspect == "4:3":
        w, h = 1440, 1080
    else:
        w, h = 1920, 1080

    img = Image.new("RGB", (w, h), color=(15, 18, 25))
    draw = ImageDraw.Draw(img)

    # Outer border (Cyan)
    draw.rectangle([10, 10, w - 10, h - 10], outline=(0, 255, 255), width=5)

    if aspect == "16:9":
        # 4:3 boundary box (Yellow) - 1440x1080 centered: x from 240 to 1680
        draw.rectangle([240, 10, 1680, h - 10], outline=(255, 220, 0), width=4)
        draw.text((255, 25), "YELLOW BOX = 4:3 BOUNDARY (1440x1080)", fill=(255, 220, 0))
        draw.text((25, 25), "CYAN FRAME = 16:9 FULLSCREEN (1920x1080)", fill=(0, 255, 255))
    else:
        draw.text((25, 25), "CYAN FRAME = NATIVE 4:3 CONTAINER (1440x1080)", fill=(0, 255, 255))

    # Concentric circles in center
    cx, cy = w // 2, h // 2
    for r in [150, 250, 350]:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 255, 0), width=4)

    # Center crosshair
    draw.line([cx - 400, cy, cx + 400, cy], fill=(0, 255, 0), width=2)
    draw.line([cx, cy - 400, cx, cy + 400], fill=(0, 255, 0), width=2)

    # Text instructions
    draw.text((cx - 240, cy - 80), "ASPECT RATIO GEOMETRY TEST", fill=(255, 255, 255))
    draw.text((cx - 290, cy - 40), "• If green rings are ROUND: Aspect ratio is TRUE.", fill=(180, 255, 180))
    draw.text((cx - 290, cy - 10), "• If green rings are OVAL (tall/skinny): Squeezed horizontally.", fill=(255, 180, 180))
    draw.text((cx - 290, cy + 20), "• If green rings are OVAL (wide/flat): Stretched horizontally.", fill=(255, 220, 180))
    draw.text((cx - 290, cy + 60), f"CANVAS: {w}x{h} ({aspect}) with square pixels (1:1 SAR)", fill=(200, 220, 255))

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_img:
        tmp_img_path = Path(tmp_img.name)
    img.save(str(tmp_img_path))

    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(tmp_img_path),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-t", str(duration_secs),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tmp_img_path.unlink(missing_ok=True)
    return res.returncode == 0


def run_test_pattern(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Generate an aspect ratio calibration card with perfect circles and set it as intro.mp4."""
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)
    target_intro = config.get("target_intro_name", DEFAULT_TARGET_INTRO)
    dest_path = Path(video_dir) / target_intro
    aspect = getattr(args, "aspect", "16:9")

    logger.info("Generating %s Aspect Ratio Calibration Card...", aspect)
    temp_file = Path(tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name)
    success = generate_calibration_video(temp_file, aspect=aspect)
    if not success:
        logger.error("Failed to generate calibration video.")
        sys.exit(1)

    rotator = IntroRotator(video_dir=video_dir, target_intro_name=target_intro)
    rotator.restore_current_intro(dry_run=args.dry_run)

    shutil.move(str(temp_file), str(dest_path))
    state_file = Path(video_dir) / ".intro_state.json"
    now_iso = datetime.now(timezone.utc).isoformat()
    state_data = {
        "current_intro_original_name": f"calibration_pattern_{aspect.replace(':', 'x')}.mp4",
        "last_index": 0,
        "last_rotated_at": now_iso,
        "history": [{"name": f"calibration_pattern_{aspect.replace(':', 'x')}.mp4", "rotated_at": now_iso}],
    }
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
    except Exception as e:
        logger.warning("Could not update intro state: %s", e)

    print("\n" + "=" * 80)
    print(f"CALIBRATION INTRO ACTIVATED: {dest_path.name} ({aspect} Canvas)")
    print("=" * 80)
    print("Now go to Plex and press PLAY on any movie or TV show to see the pre-roll intro.")
    print("Look at the green concentric rings in the center:")
    print("  • If the rings are perfectly ROUND:")
    print(f"    -> Your Plex player is rendering {aspect} with correct, undistorted 1:1 geometry.")
    print("  • If the rings are an OVAL (skinny / squished horizontally):")
    print("    -> Your player or TV is actively compressing the video horizontally!")
    print("=" * 80 + "\n")


def run_test_clip(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Render a test video using a specific mode (blur, zoom, native, stretch) and set it as intro.mp4."""
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)
    target_intro = config.get("target_intro_name", DEFAULT_TARGET_INTRO)
    dest_path = Path(video_dir) / target_intro
    mode = getattr(args, "mode", "blur").lower()
    album_url = args.album_url or config.get("album_url") or DEFAULT_ALBUM_URL

    rotator = IntroRotator(video_dir=video_dir, target_intro_name=target_intro)
    candidates = rotator.get_candidate_videos()
    clip_source = None
    if getattr(args, "clip", None):
        specified = Path(args.clip)
        if specified.exists():
            clip_source = specified
        elif (Path(video_dir) / specified.name).exists():
            clip_source = Path(video_dir) / specified.name

    if not clip_source and candidates:
        clip_source = candidates[0]

    if not clip_source:
        intro_f = Path(video_dir) / target_intro
        if intro_f.exists():
            clip_source = intro_f

    if not clip_source:
        logger.info("No candidate video found locally. Downloading clip 1 from album...")
        dl = GPhotosAlbumDownloader(album_url=album_url, output_dir=video_dir, transcode_to_mp4=False)
        downloaded = dl.sync_album(dry_run=False, limit=1)
        if downloaded:
            clip_source = downloaded[0]
        else:
            logger.error("Could not find or download any video clip to test.")
            sys.exit(1)

    logger.info("Using source clip: %s", clip_source.name)
    dl = GPhotosAlbumDownloader(album_url=album_url, output_dir=video_dir, transcode_to_mp4=True)
    probe = dl.probe_video_details(clip_source)
    w = probe.get("effective_width") or probe.get("width")
    h = probe.get("effective_height") or probe.get("height")
    sar = probe.get("sar", "1:1")
    dar = probe.get("dar")
    logger.info("[DIAGNOSTIC] Source: %sx%s (SAR=%s, DAR=%s)", w or '?', h or '?', sar, dar or 'N/A')

    temp_out = Path(tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name)

    if mode == "zoom":
        vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1"
        mode_desc = "16:9 Crop & Zoom (fills 100% of widescreen with zero bars and zero blur)"
        cmd = [dl.ffmpeg_path, "-y", "-i", str(clip_source), "-vf", vf, "-map", "0:v", "-map", "0:a?"]
    elif mode == "native":
        vf = "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1"
        mode_desc = "Pure Native Resolution (no 16:9 canvas padding, no blur)"
        cmd = [dl.ffmpeg_path, "-y", "-i", str(clip_source), "-vf", vf, "-map", "0:v", "-map", "0:a?"]
    elif mode == "stretch":
        vf = "scale=1920:1080,setsar=1"
        mode_desc = "Stretch to 16:9 (forced full screen stretch)"
        cmd = [dl.ffmpeg_path, "-y", "-i", str(clip_source), "-vf", vf, "-map", "0:v", "-map", "0:a?"]
    else:  # blur
        mode = "blur"
        mode_desc = "16:9 Ambient Blurred Background Echo (true center proportions, blurred sides)"
        fstr = (
            "[0:v]split=2[in_bg][in_fg];"
            "[in_bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=luma_radius=min(h\\,w)/20:luma_power=2,eq=brightness=-0.12[bg];"
            "[in_fg]scale=1920:1080:force_original_aspect_ratio=decrease,setsar=1[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[outv]"
        )
        cmd = [dl.ffmpeg_path, "-y", "-i", str(clip_source), "-filter_complex", fstr, "-map", "[outv]", "-map", "0:a?"]

    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(temp_out)
    ]

    logger.info("Transcoding test clip with mode '%s' (%s)...", mode, mode_desc)
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        logger.error("FFmpeg failed: %s", res.stderr.decode("utf-8", errors="ignore")[-1000:])
        temp_out.unlink(missing_ok=True)
        sys.exit(1)

    rotator.restore_current_intro(dry_run=args.dry_run)
    shutil.move(str(temp_out), str(dest_path))

    out_probe = dl.probe_video_details(dest_path)
    out_w = out_probe.get("width")
    out_h = out_probe.get("height")
    out_sar = out_probe.get("sar")
    out_dar = out_probe.get("dar")

    state_file = Path(video_dir) / ".intro_state.json"
    now_iso = datetime.now(timezone.utc).isoformat()
    state_data = {
        "current_intro_original_name": f"{clip_source.stem}_test_{mode}.mp4",
        "last_index": 0,
        "last_rotated_at": now_iso,
        "history": [{"name": f"{clip_source.stem}_test_{mode}.mp4", "rotated_at": now_iso}],
    }
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
    except Exception as e:
        logger.warning("Could not update intro state: %s", e)

    print("\n" + "=" * 80)
    print(f"TEST CLIP ACTIVATED: {dest_path.name}")
    print(f"Source Video: {clip_source.name} ({w}x{h})")
    print(f"Render Mode:  {mode.upper()} -> {mode_desc}")
    print(f"Output File:  {out_w}x{out_h} (SAR={out_sar}, DAR={out_dar})")
    print("=" * 80)
    print("Now go to Plex and press PLAY to see how this mode looks on your screen!")
    print("=" * 80 + "\n")


class DaemonRunner:
    def __init__(
        self,
        video_dir: str | Path,
        album_url: str,
        target_intro: str = DEFAULT_TARGET_INTRO,
        sync_interval_secs: float = DEFAULT_SYNC_INTERVAL_HOURS * 3600,
        rotate_interval_secs: float = DEFAULT_ROTATE_INTERVAL_MINUTES * 60,
        transcode_to_mp4: bool = True,
        normalize_audio: bool = True,
        visual_filter: Any = True,
        dry_run: bool = False,
    ):
        self.video_dir = Path(video_dir)
        self.album_url = album_url
        self.target_intro = target_intro
        self.sync_interval_secs = sync_interval_secs
        self.rotate_interval_secs = rotate_interval_secs
        self.transcode_to_mp4 = transcode_to_mp4
        self.normalize_audio = normalize_audio
        self.visual_filter = visual_filter
        self.dry_run = dry_run
        self.running = True

        self.downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=self.video_dir,
            transcode_to_mp4=self.transcode_to_mp4,
            normalize_audio=self.normalize_audio,
            visual_filter=self.visual_filter,
            target_intro_name=self.target_intro,
        )
        self.rotator = IntroRotator(
            video_dir=self.video_dir,
            target_intro_name=self.target_intro,
        )

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info("Received signal %s. Shutting down daemon gracefully...", sig_name)
        self.running = False
        sys.exit(0)

    def run(self, initial_sync: bool = True, initial_rotate: bool = True) -> None:
        logger.info("Plex Intro Randomizer Daemon started.")
        logger.info("Directory: %s", self.video_dir)
        logger.info("Sync interval: %.1f hours (%.0f s)", self.sync_interval_secs / 3600, self.sync_interval_secs)
        logger.info("Rotate interval: %.1f minutes (%.0f s)", self.rotate_interval_secs / 60, self.rotate_interval_secs)
        logger.info("Visual Filter: %s | Normalize Audio: %s", self.visual_filter, self.normalize_audio)

        last_sync_time = 0.0
        last_rotate_time = 0.0

        if initial_sync:
            try:
                logger.info("Running startup Google Photos sync...")
                self.downloader.sync_album(dry_run=self.dry_run)
            except Exception as e:
                logger.error("Initial sync encountered an error: %s", e)
            last_sync_time = time.time()

        if initial_rotate:
            try:
                logger.info("Running startup intro rotation...")
                self.rotator.rotate(dry_run=self.dry_run)
            except Exception as e:
                logger.error("Initial rotation encountered an error: %s", e)
            last_rotate_time = time.time()

        while self.running:
            now = time.time()

            # Check rotation interval
            if now - last_rotate_time >= self.rotate_interval_secs:
                try:
                    logger.info("Scheduled rotation triggered.")
                    self.rotator.rotate(dry_run=self.dry_run)
                except Exception as e:
                    logger.error("Error during scheduled rotation: %s", e)
                last_rotate_time = time.time()

            # Check sync interval
            if now - last_sync_time >= self.sync_interval_secs:
                try:
                    logger.info("Scheduled daily sync triggered.")
                    self.downloader.sync_album(dry_run=self.dry_run)
                except Exception as e:
                    logger.error("Error during scheduled sync: %s", e)
                last_sync_time = time.time()

            # Sleep short duration to stay responsive to signals
            for _ in range(10):
                if not self.running:
                    break
                time.sleep(1)

        logger.info("Daemon exited.")


def run_daemon(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    video_dir = args.dir or config.get("video_directory")
    if not video_dir:
        logger.error("Video directory is required. Specify with --dir / -d or in config.")
        sys.exit(1)

    album_url = args.album_url or config.get("album_url") or DEFAULT_ALBUM_URL
    if not album_url:
        logger.error("Album URL is required. Specify with --album-url or in config.")
        sys.exit(1)
    target_intro = config.get("target_intro_name", DEFAULT_TARGET_INTRO)

    sync_hours = args.sync_interval if args.sync_interval is not None else config.get("sync_interval_hours", DEFAULT_SYNC_INTERVAL_HOURS)
    rotate_mins = args.rotate_interval if args.rotate_interval is not None else config.get("rotate_interval_minutes", DEFAULT_ROTATE_INTERVAL_MINUTES)
    transcode = config.get("transcode_to_mp4", True) if args.transcode is None else args.transcode
    normalize_audio = config.get("normalize_audio", True) if args.normalize_audio is None else args.normalize_audio
    visual_filter = config.get("visual_filter", False)

    runner = DaemonRunner(
        video_dir=video_dir,
        album_url=album_url,
        target_intro=target_intro,
        sync_interval_secs=sync_hours * 3600,
        rotate_interval_secs=rotate_mins * 60,
        transcode_to_mp4=transcode,
        normalize_audio=normalize_audio,
        visual_filter=visual_filter,
        dry_run=args.dry_run,
    )
    runner.run(
        initial_sync=not args.no_initial_sync,
        initial_rotate=not args.no_initial_rotate,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plex Intro Randomizer & Google Photos Sync for Linux Mint",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to JSON/YAML configuration file (default: config.json if present)",
        default=None,
    )
    parser.add_argument(
        "-d", "--dir",
        help="Directory where intro videos are stored / backed up",
        default=None,
    )
    parser.add_argument(
        "--album-url",
        help="Google Photos public shared album URL",
        default=None,
    )
    parser.add_argument(
        "--no-transcode",
        dest="transcode",
        action="store_false",
        help="Disable automatic transcoding of non-MP4 videos to MP4",
        default=None,
    )
    parser.add_argument(
        "--normalize-audio",
        dest="normalize_audio",
        action="store_true",
        help="Enable EBU R128 audio loudness normalization (prevents clipping & quiet audio)",
        default=None,
    )
    parser.add_argument(
        "--no-normalize-audio",
        dest="normalize_audio",
        action="store_false",
        help="Disable audio loudness normalization",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate operations without making changes to filesystem or downloading",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Command to run")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Run one-way download sync from Google Photos album")
    sync_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-download all videos and replace existing files (bypasses cache/manifest)",
    )
    sync_parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe existing videos in the directory first before downloading fresh from album",
    )
    sync_parser.add_argument(
        "-n", "--limit", "--first",
        dest="limit",
        type=int,
        default=None,
        help="Limit number of videos to download/process (e.g. --first 5)",
    )

    # redownload / force-download command
    redownload_parser = subparsers.add_parser(
        "redownload",
        help="Force download all videos from Google Photos and replace everything",
    )
    redownload_parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe existing videos in the directory first before downloading fresh from album",
    )
    redownload_parser.add_argument(
        "-n", "--limit", "--first",
        dest="limit",
        type=int,
        default=None,
        help="Limit number of videos to download/process (e.g. --first 5)",
    )

    force_dl_parser = subparsers.add_parser(
        "force-download",
        help="Alias for 'redownload': force download all videos and replace everything",
    )
    force_dl_parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe existing videos in the directory first before downloading fresh from album",
    )
    force_dl_parser.add_argument(
        "-n", "--limit", "--first",
        dest="limit",
        type=int,
        default=None,
        help="Limit number of videos to download/process (e.g. --first 5)",
    )

    # first-five / first-5 command for fast testing
    first_five_parser = subparsers.add_parser(
        "first-five",
        help="Download and process only the first 5 videos from the album for fast testing",
    )
    first_five_parser.add_argument(
        "--clean",
        action="store_true",
        default=True,
        help="Wipe existing videos in the directory first before downloading (default: True)",
    )
    first_five_parser.add_argument(
        "--no-clean",
        action="store_false",
        dest="clean",
        help="Do not wipe existing videos before downloading",
    )
    first_five_parser.add_argument(
        "-n", "--count", "--first", "--limit",
        dest="limit",
        type=int,
        default=5,
        help="Number of videos to download (default: 5)",
    )

    first_5_parser = subparsers.add_parser(
        "first-5",
        help="Alias for 'first-five'",
    )
    first_5_parser.add_argument(
        "--clean",
        action="store_true",
        default=True,
        help="Wipe existing videos in the directory first before downloading (default: True)",
    )
    first_5_parser.add_argument(
        "--no-clean",
        action="store_false",
        dest="clean",
        help="Do not wipe existing videos before downloading",
    )
    first_5_parser.add_argument(
        "-n", "--count", "--first", "--limit",
        dest="limit",
        type=int,
        default=5,
        help="Number of videos to download (default: 5)",
    )

    # normalize command
    norm_parser = subparsers.add_parser("normalize", help="Process existing videos to apply config filters and audio normalization")
    norm_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-processing of all video files to apply visual filter and audio normalization",
    )

    # rotate command
    subparsers.add_parser("rotate", help="Rotate intro video once (renames previous intro safely and cycles to next intro in sequence)")

    # status command
    subparsers.add_parser("status", help="Show current intro status, candidate videos, and state")

    # compare command
    cmp_parser = subparsers.add_parser("compare", help="Compare original Google Photos resolutions side-by-side with local transcoded files")
    cmp_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of videos to check",
    )

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Run continuous background service (daily sync + hourly rotate)")
    daemon_parser.add_argument(
        "--sync-interval",
        type=float,
        help=f"Sync interval in hours (default: {DEFAULT_SYNC_INTERVAL_HOURS})",
        default=None,
    )
    daemon_parser.add_argument(
        "--rotate-interval",
        type=float,
        help=f"Rotation interval in minutes (default: {DEFAULT_ROTATE_INTERVAL_MINUTES})",
        default=None,
    )
    daemon_parser.add_argument(
        "--no-initial-sync",
        action="store_true",
        help="Skip immediate album sync upon daemon startup",
    )
    daemon_parser.add_argument(
        "--no-initial-rotate",
        action="store_true",
        help="Skip immediate intro rotation upon daemon startup",
    )

    # test-pattern command
    pattern_parser = subparsers.add_parser(
        "test-pattern",
        help="Generate an aspect ratio calibration card (with circles and grid) and set it as intro.mp4",
    )
    pattern_parser.add_argument(
        "--aspect",
        choices=["16:9", "4:3"],
        default="16:9",
        help="Target canvas aspect ratio to test (default: 16:9)",
    )

    # test-clip command
    clip_test_parser = subparsers.add_parser(
        "test-clip",
        help="Render a video clip using a specific scaling mode (blur, zoom, native, stretch) and set it as intro.mp4",
    )
    clip_test_parser.add_argument(
        "--mode",
        choices=["blur", "zoom", "native", "stretch"],
        default="blur",
        help="Scaling mode: 'blur' (ambient fill), 'zoom' (fill 16:9), 'native' (raw 4:3), 'stretch' (forced wide)",
    )
    clip_test_parser.add_argument(
        "--clip",
        default=None,
        help="Path or name of specific clip to test (defaults to first video)",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)

    if args.command in ["sync", "redownload", "force-download", "first-five", "first-5"]:
        run_sync(args, config)
    elif args.command == "normalize":
        run_normalize(args, config)
    elif args.command == "rotate":
        run_rotate(args, config)
    elif args.command == "status":
        run_status(args, config)
    elif args.command in ["compare", "check-resolutions"]:
        run_compare(args, config)
    elif args.command == "test-pattern":
        run_test_pattern(args, config)
    elif args.command == "test-clip":
        run_test_clip(args, config)
    elif args.command == "daemon":
        run_daemon(args, config)
    else:
        logger.error("Unknown command: %s", args.command)
        sys.exit(1)


if __name__ == "__main__":
    main()
