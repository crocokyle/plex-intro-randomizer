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

        # Verify output preserves original 320x240 dimensions
        w, h = downloader.get_video_dimensions(dest_mp4)
        self.assertEqual(w, 320)
        self.assertEqual(h, 240)

    def test_portrait_aspect_ratio_preservation(self):
        """Verify that a 720x1280 portrait video preserves its exact 9:16 aspect ratio and dimensions."""
        portrait_src = Path(self.test_dir) / "vertical_clip.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=green:s=720x1280:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1",
            str(portrait_src),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0)

        downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=self.test_dir,
            transcode_to_mp4=True,
        )
        w, h = downloader.get_video_dimensions(portrait_src)
        self.assertEqual((w, h), (720, 1280))

        # Run normalize_existing_videos with force=True
        fixed = downloader.normalize_existing_videos(force=True)
        self.assertEqual(fixed, 1)

        # Verify exact dimensions and 9:16 aspect ratio are preserved (not padded to 16:9)
        w2, h2 = downloader.get_video_dimensions(portrait_src)
        self.assertEqual((w2, h2), (720, 1280))

    def test_visual_filters_and_audio_norm(self):
        """Verify that sepia, bw, and disabled visual filters transcode successfully with audio norm."""
        src_p = Path(self.test_dir) / "test_filter_src.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-t", "1",
            str(src_p),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0)

        for filter_opt in [True, "sepia", "bw", False]:
            dst_p = Path(self.test_dir) / f"test_filter_{filter_opt}.mp4"
            dl = GPhotosAlbumDownloader(
                album_url=self.album_url,
                output_dir=self.test_dir,
                transcode_to_mp4=True,
                normalize_audio=True,
                visual_filter=filter_opt,
            )
            success = dl.transcode_video_to_mp4(src_p, dst_p)
            self.assertTrue(success, f"Transcoding failed for visual_filter={filter_opt}")
            self.assertTrue(dst_p.exists())
            w, h = dl.get_video_dimensions(dst_p)
            self.assertEqual((w, h), (320, 240))
            self.assertTrue(dl.has_audio_stream(dst_p))

    def test_sync_album_force_and_clean(self):
        """Verify that sync_album respects force=True (redownloads/replaces) and clean=True (wipes old clips)."""
        import io
        from unittest.mock import patch, MagicMock

        # Create a valid 1-second sample video to serve as mock download payload
        sample_mp4 = Path(self.test_dir) / "source_mock.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1",
            str(sample_mp4),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0)
        sample_bytes = sample_mp4.read_bytes()

        output_sync_dir = Path(self.test_dir) / "intros_sync"
        output_sync_dir.mkdir(parents=True, exist_ok=True)

        downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=output_sync_dir,
            transcode_to_mp4=True,
            normalize_audio=False,
            visual_filter="none",
        )

        downloader.fetch_album_page = MagicMock(return_value=("<html>mock</html>", None))
        downloader.extract_media_urls = MagicMock(return_value=["https://lh3.googleusercontent.com/pw/ITEM_ALPHA"])
        downloader.get_media_info = MagicMock(return_value={
            "download_url": "https://example.com/video.mp4",
            "filename": "clip_alpha.mp4",
            "content_type": "video/mp4",
        })

        class MockResponse:
            def __init__(self, data):
                self._io = io.BytesIO(data)
            def read(self, size=65536):
                return self._io.read(size)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch("urllib.request.urlopen", side_effect=lambda req, timeout=120: MockResponse(sample_bytes)):
            # 1. Initial sync
            synced1 = downloader.sync_album(dry_run=False, force=False)
            self.assertEqual(len(synced1), 1)
            clip_path = output_sync_dir / "clip_alpha.mp4"
            self.assertTrue(clip_path.exists())
            self.assertIn("ITEM_ALPHA", downloader.manifest)

            # 2. Second sync without force -> Should skip existing
            synced2 = downloader.sync_album(dry_run=False, force=False)
            self.assertEqual(len(synced2), 0)

            # 3. Tamper with file to verify force replace
            clip_path.write_bytes(b"tampered_data")
            self.assertEqual(clip_path.stat().st_size, len(b"tampered_data"))

            # 4. Sync with force=True -> Should re-download and replace file
            synced3 = downloader.sync_album(dry_run=False, force=True)
            self.assertEqual(len(synced3), 1)
            self.assertTrue(clip_path.exists())
            self.assertNotEqual(clip_path.read_bytes(), b"tampered_data")

            # 5. Add an extra old file and sync with clean=True
            stray_file = output_sync_dir / "stray_old_video.mp4"
            stray_file.write_bytes(b"old clip")
            self.assertTrue(stray_file.exists())

            synced4 = downloader.sync_album(dry_run=False, force=True, clean=True)
            self.assertEqual(len(synced4), 1)
            self.assertFalse(stray_file.exists(), "Clean should have wiped stray_old_video.mp4")
            self.assertTrue(clip_path.exists(), "clip_alpha.mp4 should have been re-downloaded")


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
