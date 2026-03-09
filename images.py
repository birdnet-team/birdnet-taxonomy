"""
Shared image utilities: download, crop, resize, and convert to WebP.

Provides a single pipeline for fetching remote images and converting them
to WebP at various sizes with smart cropping:
  - Landscape images: center crop to target aspect ratio
  - Square or portrait images: top crop (preserves head/subject at top)

Used by the web server (image proxy endpoint) and can be used by any
collector or build script that needs processed images.

Usage:
    from images import fetch_and_convert, crop_and_resize, ImageSize

    # Fetch from URL and get WebP bytes
    webp = fetch_and_convert(url, ImageSize(480, 320), quality=80)

    # Or process a local PIL Image directly
    from PIL import Image
    img = Image.open("photo.jpg")
    result = crop_and_resize(img, ImageSize(480, 320))
    result.save("out.webp", "WEBP", quality=80)
"""

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image

USER_AGENT = "species-data-pipeline/1.0 (image-utils)"

# Default sizes matching config.yml image proxy settings
SIZES = {
    "thumb": (150, 100),
    "medium": (480, 320),
    "large": (1200, 800),
}


@dataclass(frozen=True)
class ImageSize:
    """Target image dimensions."""
    width: int
    height: int

    @property
    def ratio(self) -> float:
        return self.width / self.height

    def as_tuple(self) -> tuple[int, int]:
        return (self.width, self.height)


def smart_crop(img: Image.Image, target_ratio: float) -> Image.Image:
    """Crop an image to the target aspect ratio.

    - Landscape (wider than target): center crop horizontally.
    - Square or portrait (taller than target): crop from the top.
      This preserves the subject which is typically at the top of
      wildlife/nature photos (head, perch, etc.).
    """
    w, h = img.size
    current_ratio = w / h

    # Already close enough to target ratio
    if abs(current_ratio - target_ratio) < 0.01:
        return img

    if current_ratio > target_ratio:
        # Landscape: too wide → center crop horizontally
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        # Square or portrait: too tall → crop from top
        new_h = int(w / target_ratio)
        return img.crop((0, 0, w, new_h))


def crop_and_resize(img: Image.Image, size: ImageSize) -> Image.Image:
    """Crop to target aspect ratio, then resize to fit within dimensions.

    Returns a new RGB image ready to be saved as WebP.
    """
    img = img.convert("RGB")
    img = smart_crop(img, size.ratio)
    img.thumbnail(size.as_tuple(), Image.LANCZOS)
    return img


def to_webp(img: Image.Image, quality: int = 80) -> bytes:
    """Convert a PIL Image to WebP bytes."""
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality)
    return buf.getvalue()


def download_image(url: str, timeout: int = 30) -> Image.Image | None:
    """Download an image from a URL and return as PIL Image, or None."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (HTTPError, URLError, TimeoutError):
        return None

    try:
        return Image.open(io.BytesIO(raw))
    except Exception:
        return None


def fetch_and_convert(url: str, size: ImageSize,
                      quality: int = 80) -> bytes | None:
    """Download image from URL, crop, resize, convert to WebP.

    Returns WebP bytes or None on failure.
    """
    img = download_image(url)
    if img is None:
        return None

    img = crop_and_resize(img, size)
    return to_webp(img, quality)


def cache_key(url: str, size_name: str) -> str:
    """Generate a deterministic cache filename for a URL + size."""
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return f"{h}_{size_name}.webp"


def fetch_cached(url: str, size_name: str, size: ImageSize,
                 cache_dir: Path, quality: int = 80) -> bytes | None:
    """Fetch an image with disk caching.

    Returns WebP bytes from cache if available, otherwise downloads,
    converts, caches, and returns. Returns None on failure.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / cache_key(url, size_name)

    if cache_file.exists():
        return cache_file.read_bytes()

    webp_bytes = fetch_and_convert(url, size, quality)
    if webp_bytes is None:
        return None

    # Atomic write via temp file
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_bytes(webp_bytes)
    tmp.replace(cache_file)

    return webp_bytes
