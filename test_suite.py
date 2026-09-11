"""
Comprehensive test suite for Plex Intro Randomizer and Google Photos Sync.
"""

import os
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

from gphotos_downloader import GPhotosAlbumDownloader, sanitize_filename
from rotator import IntroRotator, STATE_FILENAME


class TestGPhotosAlbumDownloader(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_gphotos_")
        self.album_url = "https://photos.app.goo.gl/TEST_ALBUM_KEY"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("valid_name.mp4"), "valid_name.mp4")
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename('bad:file*name?.mp4'), "bad_file_name_.mp4")

    def test_extract_media_urls(self):
        """Verify media URL extraction from HTML content."""
        downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=self.test_dir,
            transcode_to_mp4=True,
        )
        sample_html = (
            '<html><script>foo="https://lh3.googleusercontent.com/pw/ABCDEF123456"; '
            'bar="https://lh3.googleusercontent.com/pw/GHIJKL789012"; '
            'baz="https://lh3.googleusercontent.com/pw/ABCDEF123456";</script></html>'
        )
        urls = downloader.extract_media_urls(sample_html)
        self.assertEqual(len(urls), 2)
        self.assertIn("https://lh3.googleusercontent.com/pw/ABCDEF123456", urls)
        self.assertIn("https://lh3.googleusercontent.com/pw/GHIJKL789012", urls)

    def test_transcode_sample_video(self):
        """Generate a short video with ffmpeg and verify transcoding to mp4."""
        src_wmv = Path(self.test_dir) / "sample_clip.wmv"
        dest_mp4 = Path(self.test_dir) / "sample_clip.mp4"

        # Generate a 1-second test video using ffmpeg color generator
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1",
            str(src_wmv),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0, "Failed to create dummy video with ffmpeg")
        self.assertTrue(src_wmv.exists())

        downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=self.test_dir,
            transcode_to_mp4=True,
        )
        success = downloader.transcode_video_to_mp4(src_wmv, dest_mp4)
        self.assertTrue(success, "Transcoding failed")
        self.assertTrue(dest_mp4.exists())
        self.assertGreater(dest_mp4.stat().st_size, 0)


class TestIntroRotator(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_rotator_")
        self.rotator = IntroRotator(video_dir=self.test_dir, target_intro_name="intro.mp4")

        # Create 3 dummy mp4 files
        for name in ["intro_clip_A.mp4", "intro_clip_B.mp4", "intro_clip_C.mp4"]:
            p = Path(self.test_dir) / name
            p.write_bytes(b"dummy video data " + name.encode())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_candidate_listing(self):
        candidates = self.rotator.get_candidate_videos()
        self.assertEqual(len(candidates), 3)

    def test_rotation_flow(self):
        # 1. First rotation
        res1 = self.rotator.rotate()
        self.assertIsNotNone(res1)
        intro_path = Path(self.test_dir) / "intro.mp4"
        self.assertTrue(intro_path.exists())
        first_chosen = res1["chosen"]
        self.assertIn(first_chosen, ["intro_clip_A.mp4", "intro_clip_B.mp4", "intro_clip_C.mp4"])
        # The chosen file was renamed to intro.mp4, so remaining candidates should be 2
        self.assertEqual(len(self.rotator.get_candidate_videos()), 2)

        # 2. Second rotation
        res2 = self.rotator.rotate()
        self.assertIsNotNone(res2)
        # Check that the first chosen video was restored back to its original name!
        restored_file = Path(self.test_dir) / first_chosen
        self.assertTrue(restored_file.exists(), f"Expected {first_chosen} to be restored")
        # Check that the second chosen video is different (since multiple candidates existed)
        self.assertNotEqual(res2["chosen"], first_chosen)
        self.assertTrue(intro_path.exists())

    def test_collision_conflict_avoidance(self):
        """If a file with the original name is placed while intro.mp4 is active, rotation should not overwrite it."""
        res1 = self.rotator.rotate()
        chosen = res1["chosen"]

        # Create a new file with the exact same name as chosen!
        collision_path = Path(self.test_dir) / chosen
        collision_path.write_bytes(b"conflicting file content")

        # Perform second rotation
        res2 = self.rotator.rotate()
        # The conflicting file content should still be intact
        self.assertEqual(collision_path.read_bytes(), b"conflicting file content")

        # intro.mp4 should still have rotated safely
        intro_path = Path(self.test_dir) / "intro.mp4"
        self.assertTrue(intro_path.exists())

        # A backup file should have been created for the restored intro
        backup_files = list(Path(self.test_dir).glob(f"{Path(chosen).stem}_backup_*"))
        self.assertGreaterEqual(len(backup_files), 1, "Backup file should have been created to avoid conflict")

    def test_untracked_intro_preservation(self):
        """If intro.mp4 already exists with no state, it should be backed up safely."""
        intro_path = Path(self.test_dir) / "intro.mp4"
        intro_path.write_bytes(b"existing untracked intro content")

        res = self.rotator.rotate()
        self.assertIsNotNone(res)

        # intro.mp4 exists with new content
        self.assertTrue(intro_path.exists())

        # Backup of untracked intro should exist
        backups = list(Path(self.test_dir).glob("intro_backup_*.mp4"))
        self.assertGreaterEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"existing untracked intro content")


if __name__ == "__main__":
    unittest.main()
