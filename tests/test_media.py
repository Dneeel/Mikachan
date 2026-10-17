import unittest
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from mika_nexus.media import gallery_budget, groups, media_kind, verify


class GalleryTests(unittest.TestCase):
    def test_gallery_has_bounded_total_distinct_from_one_file_limit(self):
        self.assertEqual(gallery_budget(48 * 1024 * 1024), 288 * 1024 * 1024)
        self.assertEqual(gallery_budget(1900 * 1024 * 1024), 1900 * 1024 * 1024)

    def test_partition_preserves_order(self):
        for n in (1, 2, 9, 10, 11, 12, 20, 21, 31):
            with self.subTest(n=n):
                paths = [Path(str(i)) for i in range(n)]
                chunks = groups(paths)
                self.assertEqual([p for chunk in chunks for p in chunk], paths)
                self.assertTrue(all(1 <= len(chunk) <= 10 for chunk in chunks))
                self.assertTrue(all(len(chunk) >= 2 for chunk in chunks[:-1]))


class ImageValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_web_page_disguised_as_photo(self):
        with tempfile.TemporaryDirectory() as d:
            photo = Path(d) / "photo.jpg"
            photo.write_bytes(b"<html>Access denied</html>")
            with self.assertRaisesRegex(ValueError, "image content"):
                await verify(photo, 1000)

    async def test_accepts_image_signature(self):
        with tempfile.TemporaryDirectory() as d:
            photo = Path(d) / "photo.png"
            photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"padding")
            self.assertEqual(media_kind(photo, await verify(photo, 1000)), "photo")


class CachedDeliveryTests(unittest.TestCase):
    def test_only_definite_bad_file_id_is_safe_to_redownload(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendAudio
        from mika_nexus.jobs import invalid_cached_file_id

        method = SendAudio(chat_id=1, audio="bad")
        invalid = TelegramBadRequest(method, "Bad Request: wrong file identifier/HTTP URL specified")
        uncertain = TelegramBadRequest(method, "Bad Request: message caption is too long")
        self.assertTrue(invalid_cached_file_id(invalid))
        self.assertFalse(invalid_cached_file_id(uncertain))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class Mp3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_compatible_video_keeps_original_file(self):
        from mika_nexus.jobs import Worker

        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / "original.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=320x240:rate=10:duration=1", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=1", "-c:v", "libx264", "-c:a", "aac",
                 "-shortest", "-y", str(original)], check=True,
            )
            prepared = (await Worker._prepare_media(
                object.__new__(Worker), [original], 2_000_000,
            ))[0]
            self.assertEqual(prepared, original)
            self.assertTrue(original.exists())

    async def test_large_episode_is_reencoded_below_public_upload_limit(self):
        from mika_nexus.jobs import Worker

        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / "episode.mp3"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=60", "-c:a", "libmp3lame",
                 "-b:a", "128k", "-y", str(original)], check=True,
            )
            self.assertGreater(original.stat().st_size, 300_000)
            compressed = await Worker._compress_mp3(object.__new__(Worker), original, 300_000)
            self.assertLessEqual(compressed.stat().st_size, 300_000)
            self.assertEqual(media_kind(compressed, await verify(compressed, 300_000)), "audio")

    async def test_webm_is_delivered_as_playable_mp4(self):
        from mika_nexus.jobs import Worker

        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / "clip.webm"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=320x240:rate=10:duration=1", "-c:v", "libvpx-vp9",
                 "-y", str(original)], check=True,
            )
            converted = (await Worker._prepare_media(
                object.__new__(Worker), [original], 2_000_000,
            ))[0]
            self.assertEqual(converted.suffix, ".mp4")
            self.assertFalse(original.exists())
            self.assertEqual(media_kind(converted, await verify(converted, 2_000_000)), "video")
            codec = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
                 str(converted)], capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertEqual(codec, "h264")

    async def test_rejects_truncated_video_with_only_one_tiny_frame(self):
        with tempfile.TemporaryDirectory() as d:
            tiny = Path(d) / "tiny.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "color=size=320x240:rate=10:duration=0.1", "-c:v", "libx264",
                 "-y", str(tiny)], check=True,
            )
            with self.assertRaisesRegex(ValueError, "meaningful duration"):
                await verify(tiny, 2_000_000)

    async def test_post_audio_is_real_mp3(self):
        from mika_nexus.jobs import Worker

        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "post.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:a",
                    "aac",
                    "-y",
                    str(source),
                ],
                check=True,
            )
            target = await Worker._transcode_mp3(object.__new__(Worker), source, 2_000_000)
            self.assertEqual(target.suffix, ".mp3")
            self.assertFalse(source.exists())
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(target),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("codec_name=mp3", result.stdout)


class TikTokFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_mismatched_post_id(self):
        from mika_nexus.jobs import Worker

        response = Mock()
        response.json.return_value = {
            "code": 0,
            "data": {"id": "999", "play": "https://cdn.example/video.mp4"},
        }
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        worker = object.__new__(Worker)
        worker._direct = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as d,
            patch("mika_nexus.jobs.validate_dns"),
            patch("mika_nexus.jobs.httpx.AsyncClient", return_value=client),
        ):
            files = await worker._tikwm("https://www.tiktok.com/@creator/video/123", Path(d), 1000)
        self.assertIsNone(files)
        worker._direct.assert_not_awaited()
