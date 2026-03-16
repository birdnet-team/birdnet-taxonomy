#!/usr/bin/env python3
"""
Batch image downloader for species metadata.

Downloads, smart-crops (YOLO), and saves species images as named WebP files.
Images are saved to dev/images/ or dist/images/ with filenames:
    <scientific name>_<common name>_<author>.webp

Incremental: files whose cache state still matches the current source URL and
crop settings are skipped. Obsolete image files from older naming schemes or
stale dummy fallbacks are pruned automatically. Supports --limit, --dry-run,
and graceful shutdown (Ctrl-C saves progress).

Uses a thread pool for concurrent downloads.

Usage:
    python -m collectors.images              # → dist/images/
    python -m collectors.images --new-only   # only species with no cached files yet
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
    image_cache_is_current,
    image_filename,
    to_webp,
    write_image_cache_state,
)

LOGO_PATH = ROOT / "birdnet-logo-circle.png"


def _image_src(rec: dict) -> str:
    """Return the upstream source image URL from either metadata shape."""
    image = rec.get("image")
    if isinstance(image, dict):
        src = image.get("src", "")
        if src:
            return src
    return rec.get("image_url", "")


def _generate_dummy_images(base_dir: Path, sizes: dict[str, ImageSize],
                           qualities: dict[str, int]) -> None:
    """Generate grayscale dummy.webp fallback images with the BirdNET logo."""
    try:
        from PIL import Image
    except ImportError:
        print("WARN: Pillow not installed, skipping dummy image generation")
        return

    if not LOGO_PATH.exists():
        print(f"WARN: Logo not found at {LOGO_PATH}, skipping dummy images")
        return

    logo_rgba = Image.open(LOGO_PATH).convert("RGBA")

    for size_name, size in sizes.items():
        out_dir = base_dir / size_name
        dest = out_dir / "dummy.webp"
        if dest.exists():
            continue

        # Create grayscale canvas with neutral gray background
        canvas = Image.new("L", (size.width, size.height), 200)

        # Scale logo to fit 60% of the smaller dimension
        fit = int(min(size.width, size.height) * 0.6)
        logo = logo_rgba.copy()
        logo.thumbnail((fit, fit), Image.LANCZOS)

        # Composite logo onto canvas using alpha as mask
        logo_gray = logo.convert("L")
        mask = logo.split()[3]  # alpha channel
        x = (size.width - logo.width) // 2
        y = (size.height - logo.height) // 2
        canvas.paste(logo_gray, (x, y), mask)

        quality = qualities.get(size_name, 60)
        out_dir.mkdir(parents=True, exist_ok=True)
        canvas.save(dest, "WEBP", quality=quality)
        print(f"  Generated {dest}")


def _load_species(dev: bool) -> list[dict]:
    """Load species_metadata.json from dev/ or dist/."""
    for d in (["dev", "dist"] if dev else ["dist", "dev"]):
        path = ROOT / d / "species_metadata.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    print("ERROR: No species_metadata.json found. Run: python -m build.metadata")
    sys.exit(1)


def _load_image_config() -> tuple[dict[str, ImageSize], dict[str, int]]:
    """Load image sizes and qualities from config.yml."""
    cfg = load_config()
    img = cfg.get("images", {})
    sizes = {
        "thumb": ImageSize(img.get("thumb_width", 150), img.get("thumb_height", 100)),
        "medium": ImageSize(img.get("medium_width", 480), img.get("medium_height", 320)),
    }
    qualities = {
        "thumb": img.get("thumb_quality", 20),
        "medium": img.get("medium_quality", 60),
    }
    return sizes, qualities


def _prune_image_cache(base_dir: Path, sizes: dict[str, ImageSize],
                       species: list[dict]) -> None:
    """Remove orphaned cached images and stale sidecar files."""
    expected = set()
    for rec in species:
        url = _image_src(rec)
        if not url:
            continue
        expected.add(image_filename(
            rec.get("scientific_name", ""),
            rec.get("common_name", ""),
            rec.get("image_author", ""),
        ))

    for size_name in sizes:
        size_dir = base_dir / size_name
        if not size_dir.is_dir():
            continue

        for path in size_dir.iterdir():
            if not path.is_file():
                continue
            if path.name == "dummy.webp":
                continue
            if path.suffix != ".webp" or path.name not in expected:
                print(f"  Removing stale cache file: {path}")
                path.unlink()

        state_dir = size_dir / ".state"
        if not state_dir.is_dir():
            continue
        for state_path in state_dir.iterdir():
            if not state_path.is_file():
                continue
            if state_path.suffix != ".json":
                print(f"  Removing stale cache metadata: {state_path}")
                state_path.unlink()
                continue
            image_name = state_path.name[:-5]
            image_path = size_dir / image_name
            if image_name == "dummy.webp" or image_name not in expected or not image_path.exists():
                print(f"  Removing stale cache metadata: {state_path}")
                state_path.unlink()


def _has_any_cached_image(base_dir: Path, sizes: dict[str, ImageSize],
                          rec: dict) -> bool:
    """Return True if the species already has at least one cached output file."""
    fname = image_filename(
        rec.get("scientific_name", ""),
        rec.get("common_name", ""),
        rec.get("image_author", ""),
    )
    return any((base_dir / size_name / fname).exists() for size_name in sizes)


def _process_species(rec: dict, sizes: dict[str, ImageSize],
                     base_dir: Path, qualities: dict[str, int]) -> tuple[int, int]:
    """Download one species image, crop to all sizes.

    Downloads the source image once, then crops and saves to
    base_dir/thumb/ and base_dir/medium/.
    Returns (downloaded_count, failed_count).
    """
    url = _image_src(rec)
    sci = rec.get("scientific_name", "")
    common = rec.get("common_name", "")
    author = rec.get("image_author", "")
    crop_anchor = rec.get("image_crop_anchor")

    img = download_image(url)
    if img is None:
        # Use dummy image as fallback for failed downloads
        ok = 0
        for size_name in sizes:
            out_dir = base_dir / size_name
            dummy = out_dir / "dummy.webp"
            fname = image_filename(sci, common, "Stefan Kahl")
            dest = out_dir / fname
            if image_cache_is_current(dest, url, crop_anchor=crop_anchor):
                ok += 1
            elif dummy.exists():
                import shutil
                shutil.copy2(dummy, dest)
                ok += 1
        return ok, len(sizes) - ok

    fname = image_filename(sci, common, author)
    ok = 0
    fail = 0
    for size_name, size in sizes.items():
        out_dir = base_dir / size_name
        dest = out_dir / fname
        if image_cache_is_current(dest, url, crop_anchor=crop_anchor):
            ok += 1
            continue
        try:
            cropped = crop_and_resize(img.copy(), size, crop_anchor=crop_anchor)
            webp = to_webp(cropped, qualities.get(size_name, 60))
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(webp)
            tmp.replace(dest)
            write_image_cache_state(dest, url, crop_anchor=crop_anchor)
            ok += 1
        except Exception:
            # Use dummy image as fallback for crop/convert failures
            dummy = out_dir / "dummy.webp"
            if dummy.exists():
                import shutil
                shutil.copy2(dummy, dest)
                ok += 1
            else:
                fail += 1
    return ok, fail


def main():
    parser = argparse.ArgumentParser(description="Batch download species images")
    parser.add_argument("--dev", action="store_true",
                        help="Save to dev/images/ instead of dist/images/")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max species to process (0 = all)")
    parser.add_argument("--workers", type=int, default=0,
                        help="Number of download threads (default: from config.yml)")
    parser.add_argument("--quality", type=int, default=0,
                        help="WebP quality 1-100 (default: from config.yml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded without doing it")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Only process species with no cached image files yet",
    )
    args = parser.parse_args()

    setup_shutdown()
    species = _load_species(args.dev)
    sizes, qualities = _load_image_config()

    # Resolve workers default from config
    if not args.workers:
        cfg = load_config()
        args.workers = cfg.get("images", {}).get("workers", 4)

    if args.quality:
        qualities = {k: args.quality for k in qualities}

    base_dir = ROOT / ("dev" if args.dev else "dist") / "images"

    # Generate fallback dummy images
    _generate_dummy_images(base_dir, sizes, qualities)

    if not args.new_only:
        _prune_image_cache(base_dir, sizes, species)

    # Build work list: species that need at least one size
    work: list[tuple[dict, dict[str, ImageSize]]] = []
    already_done = 0
    skipped_existing = 0

    for rec in species:
        url = _image_src(rec)
        if not url:
            continue
        sci = rec.get("scientific_name", "")
        common = rec.get("common_name", "")
        author = rec.get("image_author", "")
        crop_anchor = rec.get("image_crop_anchor")

        fname = image_filename(sci, common, author)
        if args.new_only and _has_any_cached_image(base_dir, sizes, rec):
            skipped_existing += 1
            continue
        needed = {k: v for k, v in sizes.items()
                  if not image_cache_is_current(base_dir / k / fname, url, crop_anchor=crop_anchor)}
        if needed:
            work.append((rec, needed))
        else:
            already_done += 1

    if args.limit:
        work = work[:args.limit]

    total_files = sum(len(n) for _, n in work)
    print(f"Species with images: {sum(1 for r in species if _image_src(r))}")
    print(f"Already downloaded:  {already_done}")
    if args.new_only:
        print(f"Skipped existing:    {skipped_existing}")
    print(f"To download:         {len(work)} species, {total_files} files")
    print(f"Sizes:               {', '.join(sizes.keys())}")
    print(f"Workers:             {args.workers}")
    print(f"Output:              {base_dir}/{{thumb,medium}}")

    if args.dry_run or not work:
        return

    downloaded = 0
    failed = 0
    lock = threading.Lock()

    with tqdm(total=total_files, unit="img", desc="Downloading") as pbar:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for rec, needed in work:
                if is_shutting_down():
                    break
                fut = pool.submit(_process_species, rec, needed,
                                  base_dir, qualities)
                futures[fut] = len(needed)

            for fut in as_completed(futures):
                if is_shutting_down():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                ok, fail = fut.result()
                with lock:
                    downloaded += ok
                    failed += fail
                pbar.update(futures[fut])

    print(f"\nDone. Downloaded {downloaded}, failed {failed}, "
          f"total on disk {already_done + downloaded}.")


if __name__ == "__main__":
    main()
