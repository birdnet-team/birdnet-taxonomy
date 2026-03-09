#!/usr/bin/env python3
"""
Batch image downloader for species metadata.

Downloads, smart-crops (YOLO), and saves species images as named WebP files.
Images are saved to dev/images/ or dist/images/ with filenames:
    <scientific_name>_<common_name>_<author>_<size>.webp

Incremental: existing files are skipped.  Supports --limit, --dry-run,
and graceful shutdown (Ctrl-C saves progress).

Usage:
    python -m collectors.images              # all sizes → dist/images/
    python -m collectors.images --dev        # → dev/images/
    python -m collectors.images --size medium # only medium size
    python -m collectors.images --limit 100  # first 100 species
    python -m collectors.images --dry-run    # preview work
"""

import argparse
import json
import sys
import time
from pathlib import Path

from tqdm import tqdm

from collectors._common import ROOT, RateLimiter, is_shutting_down, setup_shutdown
from config import load_config
from utils.images import ImageSize, image_filename, save_species_image


def _load_species(dev: bool) -> list[dict]:
    """Load species_metadata.json from dev/ or dist/."""
    for d in (["dev", "dist"] if dev else ["dist", "dev"]):
        path = ROOT / d / "species_metadata.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    print("ERROR: No species_metadata.json found. Run: python -m build.metadata")
    sys.exit(1)


def _load_image_config() -> tuple[dict[str, ImageSize], int]:
    """Load image sizes and quality from config.yml."""
    cfg = load_config()
    img = cfg.get("images", {})
    sizes = {
        "thumb": ImageSize(img.get("thumb_width", 150), img.get("thumb_height", 100)),
        "medium": ImageSize(img.get("medium_width", 480), img.get("medium_height", 320)),
        "large": ImageSize(img.get("large_width", 1200), img.get("large_height", 800)),
    }
    quality = img.get("quality", 80)
    return sizes, quality


def main():
    parser = argparse.ArgumentParser(description="Batch download species images")
    parser.add_argument("--dev", action="store_true",
                        help="Save to dev/images/ instead of dist/images/")
    parser.add_argument("--size", choices=["thumb", "medium", "large"],
                        help="Only download a specific size (default: all)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded without doing it")
    parser.add_argument("--rps", type=float, default=5.0,
                        help="Max requests per second (default: 5)")
    args = parser.parse_args()

    shutdown = setup_shutdown()
    species = _load_species(args.dev)
    sizes, quality = _load_image_config()

    out_dir = ROOT / ("dev" if args.dev else "dist") / "images"

    # Filter sizes
    if args.size:
        sizes = {args.size: sizes[args.size]}

    # Build work list: species that have an image_url and need at least one size
    work: list[tuple[dict, list[str]]] = []
    already_done = 0

    for rec in species:
        url = rec.get("image_url", "")
        if not url:
            continue
        sci = rec.get("scientific_name", "")
        common = rec.get("common_name", "")
        author = rec.get("image_author", "")

        needed_sizes = []
        for size_name in sizes:
            fname = image_filename(sci, common, author, size_name)
            if not (out_dir / fname).exists():
                needed_sizes.append(size_name)

        if needed_sizes:
            work.append((rec, needed_sizes))
        else:
            already_done += 1

    if args.limit:
        work = work[:args.limit]

    total_files = sum(len(s) for _, s in work)
    print(f"Species with images: {sum(1 for r in species if r.get('image_url'))}")
    print(f"Already downloaded:  {already_done}")
    print(f"To download:         {len(work)} species, {total_files} files")
    print(f"Sizes:               {', '.join(sizes.keys())}")
    print(f"Output:              {out_dir}")

    if args.dry_run or not work:
        return

    limiter = RateLimiter(args.rps)
    downloaded = 0
    failed = 0

    with tqdm(total=total_files, unit="img", desc="Downloading") as pbar:
        for rec, needed_sizes in work:
            if is_shutting_down():
                print(f"\nStopped early. Downloaded {downloaded}, failed {failed}.")
                break

            url = rec["image_url"]
            sci = rec.get("scientific_name", "")
            common = rec.get("common_name", "")
            author = rec.get("image_author", "")

            for size_name in needed_sizes:
                if is_shutting_down():
                    break

                limiter.acquire()
                result = save_species_image(
                    url=url,
                    scientific_name=sci,
                    common_name=common,
                    author=author,
                    size_name=size_name,
                    size=sizes[size_name],
                    image_dir=out_dir,
                    quality=quality,
                )
                if result:
                    downloaded += 1
                else:
                    failed += 1
                pbar.update(1)

    print(f"\nDone. Downloaded {downloaded}, failed {failed}, "
          f"total on disk {already_done + downloaded}.")


if __name__ == "__main__":
    main()
