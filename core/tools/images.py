"""Image edits the assistant can perform on an uploaded image.

Mirrors artifacts.py: the tool takes bytes already held in memory by the
upload flow, produces a new file, and returns a URL. The input bytes are
never trusted as anything but pixels - Pillow re-encodes on load, so a
crafted file that is not really a valid image fails at decode time rather
than being passed through.

Output is capped in both pixel dimensions and file size before writing,
since an artifact once written is reachable by anyone with the link for as
long as it exists on disk.
"""
import hashlib
import io
import time
from pathlib import Path
from typing import Any

from PIL import Image

from .errors import ToolError

DIR = Path("data/artifacts")
MAX_INPUT_BYTES = 12 * 1024 * 1024
MAX_OUTPUT_BYTES = 6 * 1024 * 1024
MAX_DIMENSION = 6000          # either side, in or out
FORMATS = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "webp": "WEBP"}


class ImageError(ToolError):
    pass


def _load(data: bytes) -> Image.Image:
    if len(data) > MAX_INPUT_BYTES:
        raise ImageError(f"image too large, limit "
                         f"{MAX_INPUT_BYTES // 1024 // 1024}MB")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # decode now, not lazily on first use inside the tool
    except Exception as exc:
        raise ImageError(f"could not read that as an image: "
                         f"{type(exc).__name__}") from exc
    if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
        raise ImageError(f"image dimensions too large, limit "
                         f"{MAX_DIMENSION}px per side")
    return img


def _save(img: Image.Image, fmt: str, stem: str) -> dict[str, Any]:
    pillow_fmt = FORMATS.get(fmt.lower())
    if not pillow_fmt:
        raise ImageError(f"unsupported output format: {fmt!r}. "
                         f"Use one of: {', '.join(sorted(set(FORMATS)))}")
    if pillow_fmt == "JPEG" and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")  # JPEG has no alpha channel

    buf = io.BytesIO()
    img.save(buf, format=pillow_fmt)
    out_bytes = buf.getvalue()
    if len(out_bytes) > MAX_OUTPUT_BYTES:
        raise ImageError(f"result too large to save, limit "
                         f"{MAX_OUTPUT_BYTES // 1024 // 1024}MB")

    DIR.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if pillow_fmt == "JPEG" else pillow_fmt.lower()
    digest = hashlib.sha256(out_bytes).hexdigest()[:8]
    name = f"{stem}-{digest}.{ext}"
    (DIR / name).write_bytes(out_bytes)
    return {"url": f"/artifacts/{name}", "width": img.width,
           "height": img.height, "format": pillow_fmt,
           "bytes": len(out_bytes), "created": time.time()}


def crop(data: bytes, left: int, top: int, right: int, bottom: int,
         out_format: str = "png") -> dict[str, Any]:
    img = _load(data)
    box = (left, top, right, bottom)
    if not (0 <= left < right <= img.width and 0 <= top < bottom <= img.height):
        raise ImageError(f"crop box {box} is outside the image "
                         f"({img.width}x{img.height})")
    cropped = img.crop(box)
    return _save(cropped, out_format, "crop")


def resize(data: bytes, width: int, height: int,
           out_format: str = "png") -> dict[str, Any]:
    if width <= 0 or height <= 0:
        raise ImageError("width and height must be positive")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ImageError(f"requested size too large, limit "
                         f"{MAX_DIMENSION}px per side")
    img = _load(data)
    resized = img.resize((width, height), Image.LANCZOS)
    return _save(resized, out_format, "resize")


def convert_format(data: bytes, out_format: str) -> dict[str, Any]:
    img = _load(data)
    return _save(img, out_format, "convert")


def set_transparency(data: bytes, alpha: float) -> dict[str, Any]:
    """Uniform opacity, not a mask. Good enough for "make it faded" style
    requests without pretending this is a real editing surface."""
    if not 0.0 <= alpha <= 1.0:
        raise ImageError("alpha must be between 0.0 (invisible) and 1.0 (opaque)")
    img = _load(data).convert("RGBA")
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * alpha))
    img.putalpha(a)
    return _save(img, "png", "alpha")  # PNG only, JPEG cannot hold alpha
