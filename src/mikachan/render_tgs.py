"""Bounded child process for untrusted Telegram TGS animation rendering."""

import io
import json
import math
import resource
import sys
import zlib

from PIL import Image


def render(raw: bytes) -> bytes:
    if not raw.startswith(b"\x1f\x8b") or len(raw) > 1024 * 1024:
        raise ValueError("Expected a TGS sticker under 1 MiB")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    body = inflater.decompress(raw, 2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024 or not inflater.eof:
        raise ValueError("TGS decompressed size exceeds 2 MiB")
    meta = json.loads(body)
    count = float(meta["op"]) - float(meta["ip"])
    if not (1 < count <= 180 and 0 < float(meta["fr"]) <= 120 and
            0 < int(meta["w"]) <= 512 and 0 < int(meta["h"]) <= 512):
        raise ValueError("TGS dimensions or frame count are unsupported")
    from lottie.importers.core import import_tgs  # type: ignore[import-untyped]
    from lottie.exporters.gif import export_gif  # type: ignore[import-untyped]

    animation = import_tgs(io.BytesIO(body))
    out = io.BytesIO()
    export_gif(animation, out, skip_frames=math.ceil(count / 45))
    result = out.getvalue()
    gif = Image.open(io.BytesIO(result))
    if gif.format != "GIF" or getattr(gif, "n_frames", 1) < 2 or len(result) > 8 * 1024 * 1024:
        raise ValueError("Rendered GIF failed animation validation")
    return result


def main() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (18, 18))
    try:
        sys.stdout.buffer.write(render(sys.stdin.buffer.read(1024 * 1024 + 1)))
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
