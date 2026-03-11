#!/usr/bin/env python3
"""
Batch image downloader for species metadata.

Downloads, smart-crops (YOLO), and saves species images as named WebP files.
Images are saved to dev/images/ or dist/images/ with filenames:
    <scientific name>_<common name>_<author>.webp

Incremental: existing files are skipped.  Supports --limit, --dry-run,
and graceful shutdown (Ctrl-C saves progress).

Uses a thread pool for concurrent downloads.

Usage:
    python -m collectors.images              # → dist/images/
    python -m collectors.images --dev        # → dev/images/
    python -m collectors.images --limit 100  # first 100 species
    python -m collectors.images --dry-run    # preview work
    python -m collectors.images --workers 8  # 8 threads (default: 4)
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from collectors._common import ROOT, is_shutting_down, setup_shutdown
from config import load_config
from utils.images import (
    ImageSize,
    crop_and_resize,
    download_image,
    image_filename,
    to_webp,
)


def _load_species(dev: bool) -> list[dict]:
    """Load species_metadata.json from dev/ or dist/."""
    for d in (["dev", "dist"] if dev else ["dist", "dev"]):
        path = ROOT / d / "species_metadata.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    print("ERROR: No species_metadata.json found. Run: python -m build.metadata")
    sys.exit(1)


def _load_image_config() -> tuple[ImageSize, int]:
    """Load image size and quality from config.yml."""
    cfg = load_config()
    img = cfg.get("images", {})
    size = ImageSize(img.get("width", 480), img.get("height", 320))
    quality = img.get("quality", 60)
    return size, quality


def _process_species(rec: dict, size: ImageSize,
                     out_dir: Path, quality: int) -> tuple[int, int]:
    """Download one species image, crop and save.

    Returns (downloaded_count, failed_count).
    """
    url = rec["image_url"]
    sci = rec.get("scientific_name", "")
    common = rec.get("common_name", "")
    author = rec.get("image_author", "")

    img = download_image(url)
    if img is None:
        return 0, 1

    fname = image_filename(sci, common, author)
    dest = out_dir / fname
    if dest.exists():
        return 1, 0
    try:
        cropped = crop_and_resize(img, size)
        webp = to_webp(cropped, quality)
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(webp)
        tmp.replace(dest)
        return 1, 0
    except Exception:
        return 0, 1


def main():
    parser = argparse.ArgumentParser(description="Batch download species images")
    parser.add_argument("--dev", action="store_true",
                        help="Save to dev/images/ instead of dist/images/")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of download threads (default: 4)")
    parser.add_argument("--quality", type=int, default=0,
                        help="WebP quality 1-100 (default: from config.yml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded without doing it")
    args = parser.parse_args()

    setup_shutdown()
    species = _load_species(args.dev)
    size, quality = _load_image_config()
    if args.quality:
        quality = args.quality

    out_dir = ROOT / ("dev" if args.dev else "dist") / "images"

    # Build work list: species that have an image_url and need downloading
    work: list[dict] = []
    already_done = 0

    for rec in species:
        url = rec.get("image_url", "")
        if not url:
            continue
        sci = rec.get("scientific_name", "")
        common = rec.get("common_name", "")
        author = rec.get("image_author", "")

        fname = image_filename(sci, common, author)
        if (out_dir / fname).exists():
            already_done += 1
        else:
            work.append(rec)

    if args.limit:
        work = work[:args.limit]

    print(f"Species with images: {sum(1 for r in species if r.get('image_url'))}")
    print(f"Already downloaded:  {already_done}")
    print(f"To download:         {len(work)} species")
    print(f"Workers:             {args.workers}")
    print(f"Output:              {out_dir}")

    if args.dry_run or not work:
        return

    downloaded = 0
    failed = 0
    lock = threading.Lock()

    with tqdm(total=len(work), unit="img", desc="Downloading") as pbar:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for rec in work:
                if is_shutting_down():
                    break
                fut = pool.submit(_process_species, rec, size,
                                  out_dir, quality)
                futures[fut] = 1

            for fut in as_completed(futures):
                if is_shutting_down():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                ok, fail = fut.result()
                with lock:
                    downloaded += ok
                    failed += fail
                pbar.update(1)

    print(f"\nDone. Downloaded {downloaded}, failed {failed}, "
          f"total on disk {already_done + downloaded}.")


if __name__ == "__main__":
    main()
