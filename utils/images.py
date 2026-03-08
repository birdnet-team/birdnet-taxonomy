#!/usr/bin/env python3
"""
Download species images, convert to WebP, and save as center-cropped
thumbnail and medium sizes in 3:2 aspect ratio.

Optional step — run after merge.py produces species_metadata.json.
Images are saved to images/ (gitignored).

Filename format:
  <scientific_name>_<common_name_en>_<author>_<thumb|medium>.webp
  (spaces → underscores, special characters stripped)

Usage:
    python -m utils.images [--limit N] [--dry-run] [--source inat|ebird|both]
"""

import argparse
import io
import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from PIL import Image

from utils.config import load_config

ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = ROOT / "images"

USER_AGENT = "species-data-collector/1.0 (https://github.com/birdnet-team/species-data)"


def _get_images_config() -> dict:
    cfg = load_config()
    return cfg.get("images", {})


def sanitize(text: str) -> str:
    """Convert text to a safe filename component.

    Lowercase, spaces to underscores, strip non-alphanumeric (except underscore).
    """
    text = text.strip().lower()
    text = text.replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]", "", text)
    # Collapse multiple underscores
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def extract_author(attribution: str) -> str:
    """Extract author name from iNat/eBird attribution strings.

    Examples:
      '(c) John Smith, some rights reserved (CC BY-NC)' → 'john_smith'
      'Eurasian Blue Tit - Ferit Başbuğ' → 'ferit_babuu'
      'Photo: Jane Doe / Macaulay Library' → 'jane_doe'
    """
    if not attribution:
        return "unknown"
    text = attribution

    # eBird format: "Common Name - Photographer Name"
    if " - " in text and not text.startswith("("):
        text = text.split(" - ", 1)[-1].strip()
        result = sanitize(text)
        return result if result else "unknown"

    # iNat format: "(c) Author, some rights reserved (CC ...)"
    text = re.sub(r"^\(c\)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r",?\s*(some|all)\s+rights\s+reserved.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*/\s*Macaulay Library.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^Photo:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(CC[^)]*\)", "", text)
    text = text.strip(" ,.")
    result = sanitize(text)
    return result if result else "unknown"


def build_filename(sci_name: str, common_name: str, author: str, size: str) -> str:
    """Build the standardised filename."""
    parts = [
        sanitize(sci_name),
        sanitize(common_name) if common_name else "unknown",
        sanitize(author) if author else "unknown",
        size,  # "thumb" or "medium"
    ]
    return "_".join(p for p in parts if p) + ".webp"


def center_crop_3_2(img: Image.Image) -> Image.Image:
    """Center-crop an image to 3:2 aspect ratio."""
    w, h = img.size
    target_ratio = 3 / 2

    current_ratio = w / h
    if abs(current_ratio - target_ratio) < 0.01:
        return img  # already ~3:2

    if current_ratio > target_ratio:
        # Too wide — crop width
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        # Too tall — crop height
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))


def download_image(url: str, timeout: int = 30) -> bytes | None:
    """Download image bytes from a URL."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"    Download error: {e}")
        return None


def process_image(
    img_bytes: bytes,
    sci_name: str,
    common_name: str,
    author: str,
    thumb_size: tuple[int, int],
    medium_size: tuple[int, int],
    quality: int,
    out_dir: Path,
) -> tuple[str, str] | None:
    """Crop, resize, and save as WebP. Returns (thumb_path, medium_path) or None."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img = img.convert("RGB")
    except Exception as e:
        print(f"    Image decode error: {e}")
        return None

    # Center crop to 3:2
    cropped = center_crop_3_2(img)

    results = []
    for size_name, target in [("thumb", thumb_size), ("medium", medium_size)]:
        resized = cropped.copy()
        resized.thumbnail(target, Image.LANCZOS)
        fname = build_filename(sci_name, common_name, author, size_name)
        path = out_dir / fname
        resized.save(path, "WEBP", quality=quality)
        results.append(fname)

    return tuple(results)


def load_metadata(dev: bool = False) -> list[dict]:
    """Load species_metadata.json from dist/ or dev/."""
    for d in (["dev", "dist"] if dev else ["dist", "dev"]):
        path = ROOT / d / "species_metadata.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return []


def main():
    img_cfg = _get_images_config()
    thumb_w = img_cfg.get("thumb_width", 150)
    thumb_h = img_cfg.get("thumb_height", 100)
    medium_w = img_cfg.get("medium_width", 480)
    medium_h = img_cfg.get("medium_height", 320)
    quality = img_cfg.get("quality", 80)
    delay = img_cfg.get("request_delay", 0.5)

    parser = argparse.ArgumentParser(
        description="Download and convert species images to WebP"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max images to download (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded without downloading")
    parser.add_argument("--source", choices=["inat", "ebird", "both"], default="both",
                        help="Which image source to use (default: both)")
    parser.add_argument("--dev", action="store_true",
                        help="Read metadata from dev/ instead of dist/")
    parser.add_argument("--save-every", type=int, default=0,
                        help="Not used (images are saved immediately)")
    args = parser.parse_args()

    records = load_metadata(dev=args.dev)
    if not records:
        print("ERROR: No species_metadata.json found. Run merge.py first.")
        raise SystemExit(1)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Loaded {len(records)} species from metadata")
    print(f"Output: {IMAGES_DIR}/")
    print(f"Sizes: thumb {thumb_w}x{thumb_h}, medium {medium_w}x{medium_h} (3:2, WebP q{quality})")

    # Build download list
    to_download = []
    for rec in records:
        sci = rec.get("scientific_name", "")
        common = rec.get("common_name", "")
        if not sci:
            continue

        # Check if already downloaded (check for medium file existence)
        if args.source in ("inat", "both"):
            url = rec.get("image_url", "")
            attr = rec.get("image_attribution", "")
            if url:
                author = extract_author(attr)
                medium_fname = build_filename(sci, common, author, "medium")
                if not (IMAGES_DIR / medium_fname).exists():
                    to_download.append((sci, common, url, attr, "inat"))

        if args.source in ("ebird", "both"):
            url = rec.get("ebird_image_url", "")
            attr = rec.get("ebird_image_attribution", "")
            if url:
                # Use large size from eBird CDN
                if not url.endswith(("/1200", "/1800", "/2400")):
                    url = url.rstrip("/") + "/1200"
                author = extract_author(attr)
                medium_fname = build_filename(sci, common, author, "medium")
                if not (IMAGES_DIR / medium_fname).exists():
                    to_download.append((sci, common, url, attr, "ebird"))

    if args.limit:
        to_download = to_download[:args.limit]

    print(f"Will download {len(to_download)} images")

    if args.dry_run:
        for sci, common, url, attr, src in to_download[:20]:
            author = extract_author(attr)
            fname = build_filename(sci, common, author, "medium")
            print(f"  [{src}] {fname}")
            print(f"         {url}")
        if len(to_download) > 20:
            print(f"  ... and {len(to_download) - 20} more")
        return

    downloaded = 0
    failed = 0
    for i, (sci, common, url, attr, src) in enumerate(to_download):
        author = extract_author(attr)
        print(f"  [{i+1}/{len(to_download)}] {sci} ({src})...", end=" ", flush=True)

        img_bytes = download_image(url)
        if not img_bytes:
            failed += 1
            continue

        result = process_image(
            img_bytes, sci, common, author,
            thumb_size=(thumb_w, thumb_h),
            medium_size=(medium_w, medium_h),
            quality=quality,
            out_dir=IMAGES_DIR,
        )

        if result:
            downloaded += 1
            print(f"OK ({len(img_bytes)//1024}KB → {result[0]}, {result[1]})")
        else:
            failed += 1

        time.sleep(delay)

    print(f"\nDone! Downloaded {downloaded}, failed {failed}.")
    print(f"Total images in {IMAGES_DIR}: {len(list(IMAGES_DIR.glob('*.webp')))} files")


if __name__ == "__main__":
    main()
