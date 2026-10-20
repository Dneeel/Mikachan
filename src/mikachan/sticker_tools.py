"""Bounded, on-demand still-image and Telegram sticker conversion helpers.

Only local, inspected image bytes are decoded. No URLs, shells, or external providers.
"""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_FILE = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_STATIC_STICKER = 512 * 1024
ACCEPTED_IMAGE_FORMATS = frozenset({
    "PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF", "ICO", "PPM", "TGA",
    "AVIF", "JPEG2000", "PCX", "DDS",
})


def decode_still(raw: bytes) -> Image.Image:
    """Return the first frame as RGBA; reject oversized or unrecognized images."""
    if not raw or len(raw) > MAX_FILE:
        raise ValueError("Image must be no larger than 8 MiB")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in ACCEPTED_IMAGE_FORMATS:
                raise ValueError("Unsupported image format. Try PNG, JPG, WebP, BMP, GIF or TIFF")
            if probe.width * probe.height > MAX_PIXELS:
                raise ValueError("Image exceeds the 20 MP utility limit")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as src:
            if src.width * src.height > MAX_PIXELS:
                raise ValueError("Image exceeds the 20 MP utility limit")
            # Multi-frame GIF/TIFF/WebP become a static sticker from frame one.
            src.seek(0)
            image = ImageOps.exif_transpose(src)
            image.load()
            return image.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, EOFError) as exc:
        raise ValueError("Unsupported or damaged image") from exc


def to_static_sticker(raw: bytes) -> bytes:
    """Output static WebP with one edge at 512px and <= 512 KiB."""
    image = decode_still(raw)
    scale = 512 / max(image.width, image.height)
    target = (max(1, round(image.width * scale)),
              max(1, round(image.height * scale)))
    image = image.resize(target, Image.Resampling.LANCZOS)
    for quality in (90, 80, 70, 55, 40, 25, 12):
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=quality, method=5)
        result = output.getvalue()
        if len(result) <= MAX_STATIC_STICKER:
            with Image.open(io.BytesIO(result)) as check:
                if check.format != "WEBP" or max(check.size) != 512:
                    raise ValueError("Static sticker verification failed")
            return result
    raise ValueError("Sticker still exceeds Telegram's 512 KiB limit")


def to_transparent_png(raw: bytes) -> bytes:
    """Convert a static sticker (or first frame of an animated preview) to PNG."""
    image = decode_still(raw)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    data = output.getvalue()
    if len(data) > MAX_FILE:
        raise ValueError("PNG exceeds the 8 MiB utility limit")
    return data
