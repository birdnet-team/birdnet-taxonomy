"""
Shared image utilities: download, crop, resize, and convert to WebP.

Provides a single pipeline for fetching remote images and converting them
to WebP at various sizes with content-aware smart cropping.  A lightweight
YOLOv8-nano model detects the animal bounding box and the crop window is
placed to include as much of the subject as possible.  Falls back to
center crop when the model is unavailable or no animal is detected.

Used by the web server (image proxy endpoint) and can be used by any
collector or build script that needs processed images.

Usage:
    from utils.images import fetch_and_convert, crop_and_resize, ImageSize

    # Fetch from URL and get WebP bytes
    webp = fetch_and_convert(url, ImageSize(480, 320), quality=80)

    # Or process a local PIL Image directly
    from PIL import Image
    img = Image.open("photo.jpg")
    result = crop_and_resize(img, ImageSize(480, 320))
    result.save("out.webp", "WEBP", quality=80)
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image

from collectors._common import USER_AGENT

try:
    import onnxruntime as ort
    _ORT_AVAILABLE = True
except ImportError:
    _ORT_AVAILABLE = False

log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# YOLOv8-nano animal detector
# ---------------------------------------------------------------------------

_YOLO_SIZE = 640
_CONF_THRESHOLD = 0.25
_IOU_THRESHOLD = 0.45
_MODEL_DIR = Path(__file__).resolve().parent / ".models"
_MODEL_FILE = "yolov8n.onnx"
_PT_URL = (
    "https://github.com/ultralytics/assets/releases/download/"
    "v8.3.0/yolov8n.pt"
)

# COCO class IDs for animals
_ANIMAL_CLASSES = frozenset({
    14,  # bird
    15,  # cat
    16,  # dog
    17,  # horse
    18,  # sheep
    19,  # cow
    20,  # elephant
    21,  # bear
    22,  # zebra
    23,  # giraffe
})

_ort_session: "ort.InferenceSession | None" = None  # type: ignore[name-defined]


def _ensure_model() -> "ort.InferenceSession | None":  # type: ignore[name-defined]
    """Load YOLOv8n ONNX model, exporting from .pt if needed."""
    global _ort_session
    if _ort_session is not None:
        return _ort_session
    if not _ORT_AVAILABLE:
        return None

    model_path = _MODEL_DIR / _MODEL_FILE
    if not model_path.exists():
        # Try exporting from .pt via ultralytics
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            log.info("Downloading and exporting YOLOv8n ONNX model …")
            from ultralytics import YOLO
            pt_path = _MODEL_DIR / "yolov8n.pt"
            # Download .pt if not cached
            if not pt_path.exists():
                req = Request(_PT_URL, headers={"User-Agent": USER_AGENT})
                with urlopen(req, timeout=120) as resp:
                    data = resp.read()
                pt_path.write_bytes(data)
            # Export to ONNX
            m = YOLO(str(pt_path))
            m.export(format="onnx", imgsz=640)
            exported = pt_path.with_suffix(".onnx")
            if exported.exists():
                exported.replace(model_path)
            pt_path.unlink(missing_ok=True)
            log.info("Model saved to %s", model_path)
        except Exception as exc:
            log.warning("Failed to prepare YOLO model: %s", exc)
            return None

    try:
        _ort_session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        return _ort_session
    except Exception as exc:
        log.warning("Failed to load YOLO model: %s", exc)
        return None


def _letterbox(img: Image.Image) -> tuple[np.ndarray, float, int, int]:
    """Resize with letterbox padding to 640×640. Returns (array, scale, pad_x, pad_y)."""
    w, h = img.size
    scale = _YOLO_SIZE / max(w, h)
    nw, nh = int(w * scale), int(h * scale)

    resized = img.resize((nw, nh), Image.BILINEAR)
    padded = Image.new("RGB", (_YOLO_SIZE, _YOLO_SIZE), (114, 114, 114))
    px, py = (_YOLO_SIZE - nw) // 2, (_YOLO_SIZE - nh) // 2
    padded.paste(resized, (px, py))

    arr = np.array(padded, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)[np.newaxis]  # BCHW
    return arr, scale, px, py


def _nms(boxes: np.ndarray, scores: np.ndarray) -> list[int]:
    """Non-maximum suppression. Returns indices to keep."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while len(order) > 0:
        i = order[0]
        keep.append(int(i))
        if len(order) == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[np.where(iou <= _IOU_THRESHOLD)[0] + 1]
    return keep


def _detect_animal(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Run YOLOv8n and return the best animal bounding box as (x1, y1, x2, y2).

    Returns pixel coordinates in the original image, or None if no animal
    is detected or the model is unavailable.
    """
    session = _ensure_model()
    if session is None:
        return None

    w, h = img.size
    rgb = img.convert("RGB")
    inp, scale, pad_x, pad_y = _letterbox(rgb)

    # Run inference
    input_name = session.get_inputs()[0].name
    output = session.run(None, {input_name: inp})[0]  # [1, 84, 8400]

    # Transpose to [8400, 84]
    pred = output[0].T  # [8400, 84]
    cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    class_scores = pred[:, 4:]  # [8400, 80]

    # Best class per detection
    max_scores = class_scores.max(axis=1)
    class_ids = class_scores.argmax(axis=1)

    # Confidence filter
    mask = max_scores > _CONF_THRESHOLD
    if not mask.any():
        return None

    cx, cy, bw, bh = cx[mask], cy[mask], bw[mask], bh[mask]
    max_scores = max_scores[mask]
    class_ids = class_ids[mask]

    # Convert to xyxy
    x1 = cx - bw / 2
    y1 = cy - bh / 2
    x2 = cx + bw / 2
    y2 = cy + bh / 2
    boxes = np.stack([x1, y1, x2, y2], axis=1)

    # NMS
    keep = _nms(boxes, max_scores)
    boxes = boxes[keep]
    max_scores = max_scores[keep]
    class_ids = class_ids[keep]

    # Prefer animal classes; fall back to any detection
    animal_mask = np.array([int(c) in _ANIMAL_CLASSES for c in class_ids])
    if animal_mask.any():
        idx = max_scores[animal_mask].argmax()
        box = boxes[animal_mask][idx]
    else:
        # No animal class found — use the highest-confidence detection
        box = boxes[max_scores.argmax()]

    # Unpad and unscale to original pixel coords
    ox1 = (box[0] - pad_x) / scale
    oy1 = (box[1] - pad_y) / scale
    ox2 = (box[2] - pad_x) / scale
    oy2 = (box[3] - pad_y) / scale

    # Clamp to image bounds
    ox1 = max(0, min(int(ox1), w))
    oy1 = max(0, min(int(oy1), h))
    ox2 = max(0, min(int(ox2), w))
    oy2 = max(0, min(int(oy2), h))

    if ox2 - ox1 < 10 or oy2 - oy1 < 10:
        return None  # too small to be meaningful

    return (ox1, oy1, ox2, oy2)


# ---------------------------------------------------------------------------
# Smart crop
# ---------------------------------------------------------------------------

def _center_crop(img: Image.Image, target_ratio: float) -> Image.Image:
    """Simple center crop to target aspect ratio."""
    w, h = img.size
    if w / h > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))


def smart_crop(img: Image.Image, target_ratio: float) -> Image.Image:
    """Content-aware crop using YOLOv8 animal detection.

    Detects the animal bounding box and places the crop window to include
    as much of the subject as possible.  When the bounding box is taller
    than the crop window (e.g. a woodpecker on a trunk), the upper portion
    is preferred so the head stays visible.

    Falls back to center crop when no animal is detected or the detector
    is not available.
    """
    w, h = img.size
    current_ratio = w / h

    if abs(current_ratio - target_ratio) < 0.01:
        return img

    bbox = _detect_animal(img)
    if bbox is None:
        return _center_crop(img, target_ratio)

    bx1, by1, bx2, by2 = bbox
    bcx = (bx1 + bx2) / 2
    bcy = (by1 + by2) / 2

    if current_ratio > target_ratio:
        # Too wide → crop horizontally (keep full height)
        crop_w = int(h * target_ratio)
        # Try to center on the bounding box
        left = int(bcx - crop_w / 2)
        # If bbox is wider than crop, center on bbox center
        # Clamp to image bounds
        left = max(0, min(left, w - crop_w))
        return img.crop((left, 0, left + crop_w, h))
    else:
        # Too tall → crop vertically (keep full width)
        crop_h = int(w / target_ratio)
        bbox_h = by2 - by1

        if bbox_h > crop_h:
            # Subject taller than crop window → prefer upper portion (head)
            top = int(by1)
        else:
            # Center crop on the bounding box
            top = int(bcy - crop_h / 2)

        # Clamp to image bounds
        top = max(0, min(top, h - crop_h))
        return img.crop((0, top, w, top + crop_h))


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


# ---------------------------------------------------------------------------
# Named image files: <sci>_<common>_<author>_<size>.webp
# ---------------------------------------------------------------------------

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitise(text: str) -> str:
    """Strip non-alphanumeric characters, collapse runs, lowercase."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode()
    text = _SAFE_RE.sub("_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower()


def image_filename(scientific_name: str, common_name: str,
                   author: str, size_name: str) -> str:
    """Build a human-readable WebP filename for a species image.

    Format: <scientific>_<common>_<author>_<size>.webp
    All parts are sanitised to ASCII alphanumerics, hyphens, underscores.
    The author field is truncated to 60 chars to avoid OS filename limits.
    """
    parts = [
        _sanitise(scientific_name),
        _sanitise(common_name) if common_name else "unknown",
        _sanitise(author)[:60].rstrip("_") if author else "unknown",
        size_name,
    ]
    return "_".join(p for p in parts if p) + ".webp"


def save_species_image(url: str, scientific_name: str, common_name: str,
                       author: str, size_name: str, size: ImageSize,
                       image_dir: Path, quality: int = 80) -> Path | None:
    """Download, crop, and save a species image with a readable filename.

    Returns the saved path, or None on failure.  Skips if the file already
    exists on disk.
    """
    fname = image_filename(scientific_name, common_name, author, size_name)
    dest = image_dir / fname

    if dest.exists():
        return dest

    webp_bytes = fetch_and_convert(url, size, quality)
    if webp_bytes is None:
        return None

    image_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(webp_bytes)
    tmp.replace(dest)
    return dest
