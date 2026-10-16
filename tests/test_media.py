import unittest
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from mika_nexus.media import groups, media_kind, verify


class GalleryTests(unittest.TestCase):
    def test_partition_preserves_order(self):
        for n in (1, 2, 9, 10, 11, 12, 20, 21, 31):
            with self.subTest(n=n):
                paths = [Path(str(i)) for i in range(n)]
                chunks = groups(paths)
                self.assertEqual([p for chunk in chunks for p in chunk], paths)
                self.assertTrue(all(1 <= len(chunk) <= 10 for chunk in chunks))
                self.assertTrue(all(len(chunk) >= 2 for chunk in chunks[:-1]))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class Mp3Tests(unittest.IsolatedAsyncioTestCase):
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
