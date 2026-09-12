"""
Comprehensive test suite for Plex Intro Randomizer and Google Photos Sync.
"""

import os
import json
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

        # Verify output is standardized 1920x1080 16:9 canvas
        w, h = downloader.get_video_dimensions(dest_mp4)
        self.assertEqual(w, 1920)
        self.assertEqual(h, 1080)

    def test_portrait_pillarbox_1080p(self):
        """Verify that a 720x1280 portrait video is fitted into 1920x1080 canvas with pillarboxing."""
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

        # Run normalize_existing_videos
        fixed = downloader.normalize_existing_videos(force=True)
        self.assertEqual(fixed, 1)

        # Verify output is 1920x1080 canvas with original 9:16 content centered inside
        w2, h2 = downloader.get_video_dimensions(portrait_src)
        self.assertEqual((w2, h2), (1920, 1080))

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
            self.assertEqual((w, h), (1920, 1080))
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
            # 1. Initial sync (clean dir) -> first downloaded video immediately becomes intro.mp4
            synced1 = downloader.sync_album(dry_run=False, force=False)
            self.assertEqual(len(synced1), 1)
            intro_path = output_sync_dir / "intro.mp4"
            self.assertTrue(intro_path.exists(), "First download should become intro.mp4 immediately")
            self.assertIn("ITEM_ALPHA", downloader.manifest)

            # Check that .intro_state.json tracks original name
            state_file = output_sync_dir / ".intro_state.json"
            self.assertTrue(state_file.exists())
            with open(state_file, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            self.assertEqual(state_data["current_intro_original_name"], "clip_alpha.mp4")

            # 2. Second sync without force -> Should skip existing (since intro.mp4 represents clip_alpha.mp4)
            synced2 = downloader.sync_album(dry_run=False, force=False)
            self.assertEqual(len(synced2), 0)

            # 3. Tamper with intro.mp4 to verify force replace
            intro_path.write_bytes(b"tampered_data")
            self.assertEqual(intro_path.stat().st_size, len(b"tampered_data"))

            # 4. Sync with force=True -> Should re-download and replace file
            synced3 = downloader.sync_album(dry_run=False, force=True)
            self.assertEqual(len(synced3), 1)
            self.assertTrue(intro_path.exists())
            self.assertNotEqual(intro_path.read_bytes(), b"tampered_data")

            # 5. Add an extra old file and sync with clean=True
            stray_file = output_sync_dir / "stray_old_video.mp4"
            stray_file.write_bytes(b"old clip")
            self.assertTrue(stray_file.exists())

            synced4 = downloader.sync_album(dry_run=False, force=True, clean=True)
            self.assertEqual(len(synced4), 1)
            self.assertFalse(stray_file.exists(), "Clean should have wiped stray_old_video.mp4")
            self.assertTrue(intro_path.exists(), "intro.mp4 should have been re-downloaded and activated fresh")

    def test_sync_with_limit(self):
        """Verify that sync_album respects the limit parameter (e.g. limit=2 stops after 2 videos)."""
        import io
        from unittest.mock import patch, MagicMock

        output_sync_dir = Path(self.test_dir) / "intros_limit_test"
        output_sync_dir.mkdir(parents=True, exist_ok=True)

        sample_mp4 = Path(self.test_dir) / "source_mock.mp4"
        if not sample_mp4.exists():
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", "1",
                str(sample_mp4),
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sample_bytes = sample_mp4.read_bytes()

        downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=output_sync_dir,
            transcode_to_mp4=False,
        )

        downloader.fetch_album_page = MagicMock(return_value=("<html>mock</html>", None))
        # Offer 5 media items
        downloader.extract_media_urls = MagicMock(return_value=[
            f"https://lh3.googleusercontent.com/pw/ITEM_{i}" for i in range(1, 6)
        ])
        downloader.get_media_info = MagicMock(side_effect=lambda u: {
            "download_url": f"{u}=dv",
            "filename": f"clip_{u.split('_')[-1]}.mp4",
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

        with patch("urllib.request.urlopen", return_value=MockResponse(sample_bytes)):
            synced = downloader.sync_album(dry_run=False, force=True, clean=True, limit=2)
            self.assertEqual(len(synced), 2, "Should have downloaded exactly 2 items due to limit=2")

    def test_parallel_multi_worker_sync(self):
        """Verify that multi-worker parallel syncing downloads multiple clips and assigns one intro.mp4."""
        import io
        from unittest.mock import patch, MagicMock

        sample_mp4 = Path(self.test_dir) / "source_parallel.mp4"
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

        output_sync_dir = Path(self.test_dir) / "intros_parallel"
        output_sync_dir.mkdir(parents=True, exist_ok=True)

        downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=output_sync_dir,
            transcode_to_mp4=True,
            normalize_audio=False,
            visual_filter="none",
            max_workers=3,
        )

        downloader.fetch_album_page = MagicMock(return_value=("<html>mock</html>", None))
        downloader.extract_media_urls = MagicMock(return_value=[
            "https://lh3.googleusercontent.com/pw/ITEM_1",
            "https://lh3.googleusercontent.com/pw/ITEM_2",
            "https://lh3.googleusercontent.com/pw/ITEM_3",
        ])
        downloader.get_media_info = MagicMock(side_effect=lambda url: {
            "download_url": "https://example.com/video.mp4",
            "filename": f"clip_{url.split('_')[-1]}.mp4",
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
            synced = downloader.sync_album(dry_run=False, force=True, clean=True, workers=3)
            self.assertEqual(len(synced), 3)
            self.assertTrue((output_sync_dir / "intro.mp4").exists())
            self.assertEqual(len(downloader.manifest), 3)

    def test_probe_and_compare_resolutions(self):
        """Verify probe_video_details accurately reads video metadata and compare_resolutions runs without error."""
        sample_mp4 = Path(self.test_dir) / "probe_target.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=640x480:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1",
            str(sample_mp4),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0)

        downloader = GPhotosAlbumDownloader(
            album_url=self.album_url,
            output_dir=self.test_dir,
        )

        details = downloader.probe_video_details(sample_mp4, is_url=False)
        self.assertEqual(details["width"], 640)
        self.assertEqual(details["height"], 480)
        self.assertEqual(details["effective_width"], 640)
        self.assertEqual(details["effective_height"], 480)

        # Mock compare_resolutions
        from unittest.mock import MagicMock
        downloader.fetch_album_page = MagicMock(return_value=("<html>mock</html>", None))
        downloader.extract_media_urls = MagicMock(return_value=["https://lh3.googleusercontent.com/pw/ITEM_1"])
        downloader.get_media_info = MagicMock(return_value={
            "download_url": str(sample_mp4),
            "filename": "probe_target.mp4",
            "content_type": "video/mp4",
        })
        downloader.compare_resolutions(limit=1)


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
        intro_path = Path(self.test_dir) / "intro.mp4"

        # 1. First rotation -> clip A
        res1 = self.rotator.rotate()
        self.assertEqual(res1["chosen"], "intro_clip_A.mp4")
        self.assertTrue(intro_path.exists())
        self.assertEqual(len(self.rotator.get_candidate_videos()), 2)

        # 2. Second rotation -> restores A, selects clip B
        res2 = self.rotator.rotate()
        self.assertEqual(res2["restored"], "intro_clip_A.mp4")
        self.assertEqual(res2["chosen"], "intro_clip_B.mp4")
        self.assertTrue((Path(self.test_dir) / "intro_clip_A.mp4").exists())

        # 3. Third rotation -> restores B, selects clip C
        res3 = self.rotator.rotate()
        self.assertEqual(res3["restored"], "intro_clip_B.mp4")
        self.assertEqual(res3["chosen"], "intro_clip_C.mp4")
        self.assertTrue((Path(self.test_dir) / "intro_clip_B.mp4").exists())

        # 4. Fourth rotation -> restores C, cycles back to clip A
        res4 = self.rotator.rotate()
        self.assertEqual(res4["restored"], "intro_clip_C.mp4")
        self.assertEqual(res4["chosen"], "intro_clip_A.mp4")
        self.assertTrue((Path(self.test_dir) / "intro_clip_C.mp4").exists())

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
