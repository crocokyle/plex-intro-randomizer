"""
Module for rotating and randomizing Plex intros conflict-free.
"""

import os
import json
import time
import random
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("plex_intro_randomizer.rotator")

STATE_FILENAME = ".intro_state.json"
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".wmv", ".mkv", ".avi", ".webm", ".m4v"}


class IntroRotator:
    def __init__(self, video_dir: str | Path, target_intro_name: str = "intro.mp4"):
        self.video_dir = Path(video_dir)
        self.target_intro_name = target_intro_name
        self.state_file = self.video_dir / STATE_FILENAME
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Could not read intro state: %s. Starting fresh.", e)
        return {}

    def _save_state(self) -> None:
        temp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.error("Failed to save state file: %s", e)
            if temp_file.exists():
                temp_file.unlink()

    def get_candidate_videos(self) -> List[Path]:
        """List all video files in the directory eligible to become the intro."""
        candidates = []
        for p in self.video_dir.iterdir():
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            if p.name == self.target_intro_name:
                continue
            if p.suffix.lower() in SUPPORTED_EXTENSIONS and not p.name.endswith(".transcoding.mp4"):
                candidates.append(p)
        return sorted(candidates)

    def restore_current_intro(self, dry_run: bool = False) -> Optional[str]:
        """
        Safely rename the active intro.mp4 back to its original filename or a conflict-free backup.
        Returns the restored filename, or None if no intro was present.
        """
        intro_path = self.video_dir / self.target_intro_name
        if not intro_path.exists():
            return None

        orig_name = self.state.get("current_intro_original_name")
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        if orig_name:
            target_path = self.video_dir / orig_name
            if target_path.exists():
                # Conflict avoidance: a file with this name already exists
                stem = Path(orig_name).stem
                ext = Path(orig_name).suffix or ".mp4"
                safe_name = f"{stem}_backup_{timestamp_str}{ext}"
                target_path = self.video_dir / safe_name
                logger.warning(
                    "Original filename '%s' already exists in directory! Renaming existing intro to '%s' to avoid collision.",
                    orig_name, safe_name
                )
            else:
                safe_name = orig_name
        else:
            # No state recorded for this intro.mp4
            safe_name = f"intro_backup_{timestamp_str}.mp4"
            target_path = self.video_dir / safe_name
            logger.info("Existing intro has no recorded history. Renaming to '%s' to prevent data loss.", safe_name)

        if dry_run:
            logger.info("[DRY RUN] Would rename current %s -> %s", self.target_intro_name, safe_name)
            return safe_name

        try:
            intro_path.rename(target_path)
            logger.info("Restored active intro %s -> %s", self.target_intro_name, safe_name)
            self.state["current_intro_original_name"] = None
            self._save_state()
            return safe_name
        except Exception as e:
            logger.error("Failed to rename current intro %s to %s: %s", intro_path, target_path, e)
            raise

    def rotate(self, dry_run: bool = False) -> Optional[Dict[str, str]]:
        """
        Rotate intro video:
        1. Restore active intro.mp4 without conflict.
        2. Pick a random candidate video (avoiding immediate repetition).
        3. Rename chosen video to intro.mp4.
        4. Update state.
        """
        orig_name = self.state.get("current_intro_original_name")
        restored_name = self.restore_current_intro(dry_run=dry_run)

        candidates = self.get_candidate_videos()
        if not candidates:
            logger.warning("No candidate video files found in %s to set as %s.", self.video_dir, self.target_intro_name)
            return None

        # Filter out the one just restored if there are alternatives
        exclude = {n for n in (restored_name, orig_name) if n}
        pool = [c for c in candidates if c.name not in exclude]
        if not pool:
            pool = candidates

        chosen = random.choice(pool)
        original_name = chosen.name
        intro_path = self.video_dir / self.target_intro_name

        logger.info("Selected '%s' to become new '%s'.", original_name, self.target_intro_name)

        if dry_run:
            logger.info("[DRY RUN] Would rename '%s' -> '%s'", original_name, self.target_intro_name)
            return {
                "restored": restored_name or "none",
                "chosen": original_name,
                "intro": self.target_intro_name,
            }

        try:
            chosen.rename(intro_path)
            now_iso = datetime.now(timezone.utc).isoformat()
            history = self.state.get("history", [])
            history.append({"name": original_name, "rotated_at": now_iso})
            # Keep history limited to last 100 entries
            if len(history) > 100:
                history = history[-100:]

            self.state["current_intro_original_name"] = original_name
            self.state["last_rotated_at"] = now_iso
            self.state["history"] = history
            self._save_state()

            logger.info("Successfully activated '%s' as '%s'.", original_name, self.target_intro_name)
            return {
                "restored": restored_name or "none",
                "chosen": original_name,
                "intro": self.target_intro_name,
            }
        except Exception as e:
            logger.error("Failed to rename '%s' to '%s': %s", chosen, intro_path, e)
            raise

    def get_status(self) -> Dict[str, Any]:
        """Return current status of intros and rotator state."""
        intro_path = self.video_dir / self.target_intro_name
        candidates = self.get_candidate_videos()
        return {
            "video_dir": str(self.video_dir),
            "intro_exists": intro_path.exists(),
            "current_intro_original_name": self.state.get("current_intro_original_name"),
            "last_rotated_at": self.state.get("last_rotated_at"),
            "total_candidate_videos": len(candidates),
            "candidates": [c.name for c in candidates],
            "recent_history": self.state.get("history", [])[-5:],
        }
