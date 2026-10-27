"""Dependency-light tests of image/sticker conversion and actual utility handler body."""
import ast
import asyncio
import copy
import io
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from mikachan.sticker_tools import MAX_FILE, to_static_sticker, to_transparent_png


class FakeFile:
    def __init__(self, data: bytes, filename: str):
        self.data = data
        self.filename = filename


def picture(fmt: str, *, size=(641, 379), alpha=False) -> bytes:
    image = Image.new("RGBA" if alpha else "RGB", size,
                      (220, 5, 40, 90) if alpha else (220, 5, 40))
    stream = io.BytesIO()
    if fmt in {"JPEG", "PPM"}:
        image = image.convert("RGB")
    image.save(stream, format=fmt)
    return stream.getvalue()


def first_animated_frame() -> bytes:
    stream = io.BytesIO()
    a = Image.new("RGB", (64, 32), "red")
    b = Image.new("RGB", (64, 32), "green")
    a.save(stream, "GIF", save_all=True, append_images=[b], duration=100, loop=0)
    return stream.getvalue()


def handler_method(name: str):
    """Execute unmodified method AST, without importing optional production dependencies."""
    source = Path(__file__).resolve().parents[1] / "src/mikachan/utilities.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    utility = next(item for item in tree.body if isinstance(item, ast.ClassDef)
                   and item.name == "Utilities")
    method = copy.deepcopy(next(item for item in utility.body
                                if isinstance(item, ast.AsyncFunctionDef) and item.name == name))
    method.decorator_list.clear()
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = {"asyncio": asyncio, "to_static_sticker": to_static_sticker,
                 "to_transparent_png": to_transparent_png, "BufferedInputFile": FakeFile,
                 "Message": object, "shutil": shutil, "MAX_INPUT": MAX_FILE}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "BMP", "GIF", "TIFF", "ICO", "PPM"])
def test_many_image_formats_to_static_sticker(fmt):
    result = to_static_sticker(picture(fmt))
    assert 0 < len(result) <= 512 * 1024
    with Image.open(io.BytesIO(result)) as im:
        assert im.format == "WEBP"
        assert max(im.size) == 512
        assert im.size[0] >= 1 and im.size[1] >= 1


def test_image_to_sticker_transparency_preserved():
    sticker = to_static_sticker(picture("PNG", alpha=True))
    png = to_transparent_png(sticker)
    with Image.open(io.BytesIO(png)) as im:
        assert im.format == "PNG"
        assert im.mode == "RGBA"
        assert 50 < im.getpixel((10, 10))[3] < 130


def test_animated_gif_uses_first_frame():
    sticker = to_static_sticker(first_animated_frame())
    with Image.open(io.BytesIO(sticker)) as im:
        red, green, blue = im.convert("RGB").getpixel((10, 10))
        assert red > 160 and green < 80 and blue < 80


def test_rejects_untrusted_bytes_and_large_files():
    for raw in (b"<svg><rect/></svg>", b"<html>error</html>", os.urandom(100)):
        with pytest.raises(ValueError):
            to_static_sticker(raw)
    with pytest.raises(ValueError, match="8 MiB"):
        to_static_sticker(b"a" * (MAX_FILE + 1))
    with pytest.raises(ValueError, match="20 MP"):
        to_static_sticker(picture("PNG", size=(5000, 4500)))


@pytest.mark.asyncio
async def test_command_sticker_reply_photo_or_document():
    method = handler_method("_sticker_command")
    content = picture("TIFF")
    target = SimpleNamespace(photo=[SimpleNamespace(file_size=len(content))], document=None)
    message = SimpleNamespace(reply_to_message=target, photo=None, document=None,
                              reply_sticker=AsyncMock(), reply_document=AsyncMock())
    ctx = SimpleNamespace(reply_file=AsyncMock(return_value=(content, "")),
                          media_slots=asyncio.Semaphore(1))
    await method(ctx, message, "/st")
    media = message.reply_sticker.await_args.args[0]
    assert media.filename.endswith("webp")
    assert Image.open(io.BytesIO(media.data)).format == "WEBP"

    target.photo = None
    target.document = SimpleNamespace(file_name="image.bmp", mime_type="application/octet-stream")
    await method(ctx, message, "/st")
    assert message.reply_sticker.await_count == 2


@pytest.mark.asyncio
async def test_sticker_command_accepts_image_caption_without_reply():
    method = handler_method("_sticker_command")
    content = picture("PNG")
    message = SimpleNamespace(reply_to_message=None, photo=[SimpleNamespace(file_size=50)],
                              document=None, reply_sticker=AsyncMock())
    ctx = SimpleNamespace(reply_file=AsyncMock(return_value=(content, "")),
                          media_slots=asyncio.Semaphore(1))
    await method(ctx, message, "/st")
    assert ctx.reply_file.await_args.kwargs["source_message"] is message


@pytest.mark.asyncio
async def test_xst_static_and_animated_sticker():
    method = handler_method("_sticker_command")
    raw = picture("WEBP", alpha=True)
    sticker = SimpleNamespace(is_animated=False, is_video=False)
    message = SimpleNamespace(reply_to_message=SimpleNamespace(sticker=sticker),
                              reply_document=AsyncMock())
    ctx = SimpleNamespace(reply_file=AsyncMock(return_value=(raw, "")),
                          media_slots=asyncio.Semaphore(1),
                          tgs_to_gif=AsyncMock(return_value=first_animated_frame()),
                          _video_sticker_frame=AsyncMock(return_value=picture("PNG")))
    await method(ctx, message, "/xst")
    out = message.reply_document.await_args.args[0]
    assert Image.open(io.BytesIO(out.data)).format == "PNG"
    assert "transparency" in message.reply_document.await_args.kwargs["caption"]

    sticker.is_animated = True
    await method(ctx, message, "/xst")
    ctx.tgs_to_gif.assert_awaited_once()
    assert "first frame" in message.reply_document.await_args.kwargs["caption"]

    sticker.is_animated = False
    sticker.is_video = True
    await method(ctx, message, "/xst")
    ctx._video_sticker_frame.assert_awaited_once()
    assert Image.open(io.BytesIO(message.reply_document.await_args.args[0].data)).format == "PNG"


@pytest.mark.asyncio
async def test_xst_requires_replied_sticker():
    method = handler_method("_sticker_command")
    message = SimpleNamespace(reply_to_message=SimpleNamespace(sticker=None))
    with pytest.raises(ValueError, match="Reply to a sticker"):
        await method(SimpleNamespace(), message, "/xst")


def test_command_registration_and_removed_rate():
    from mikachan.command_guide import SECTIONS, commands

    src = (Path(__file__).resolve().parents[1] / "src/mikachan/bot.py").read_text()
    utilities = (Path(__file__).resolve().parents[1] / "src/mikachan/utilities.py").read_text()
    tools = dict(SECTIONS["tools"][1])
    assert tools["st"] == "Turn a replied image into a sticker"
    assert tools["xst"] == "Turn a replied sticker into PNG"
    registered = {item.command for item in commands(relay_enabled=False,
                                                    tts_enabled=False, stt_enabled=False)}
    assert {"st", "xst"} <= registered and "rate" not in registered
    assert 'if cmd == "/rate":' in src  # explicit no-op for deprecated slash route
    assert '"/rate"' not in utilities
    assert '"عکس به استیکر"' in src and '"استیکر به عکس"' in src
    assert 'asset_quote(m,' in src


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
async def test_real_webm_sticker_to_png_first_frame():
    process = subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=256x256:r=5:d=0.4", "-c:v", "libvpx-vp9",
        "-threads", "1", "-f", "webm", "pipe:1",
    ], capture_output=True, check=True, timeout=12)
    assert process.stdout.startswith(b"\x1a\x45\xdf\xa3")
    method = handler_method("_video_sticker_frame")
    result = await method(SimpleNamespace(), process.stdout)
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.size == (512, 512)
        r, g, b = image.convert("RGB").getpixel((100, 100))
        assert b > r and b > g
