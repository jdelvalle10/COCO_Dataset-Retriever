# Copyright (c) 2026 Jose Luis del Valle <joseluisdelvalle@gmail.com>
# Released under the MIT License (see LICENSE).
"""Core of the P1S1 dataset generator: pick COCO 2017 images that meet the selection rules and save them.

No GUI code lives here, so everything can be tested without a display.

Rules (protocol v2 selection): short side >= min_short_side; at least one non-crowd object whose
filled outline covers area_min..area_max of the image; EXIF orientation 1; each image once
(by COCO id and by decoded pixels).

Output: OUT_DIR/images/*.jpg, OUT_DIR/labels/*.txt (YOLO segmentation: class x1 y1 x2 y2 ...,
normalised), OUT_DIR/classes.txt, OUT_DIR/selection_log.csv, OUT_DIR/annotations/ (cached).
"""
import csv
import hashlib
import io
import queue
import random
import shutil
import threading
import time
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
from pycocotools.coco import COCO

ANN_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
SPLITS = ("val", "train")
MAX_IMAGES = 20000
MAX_SHORT_SIDE = 640          # COCO images are at most 640 px on the long side


class Cancelled(Exception):
    pass


@dataclass
class Settings:
    out_dir: Path
    n_images: int = 500
    min_short_side: int = 400
    split: str = "val"
    seed: int = 20261029
    area_min: float = 0.01
    area_max: float = 0.20


@dataclass
class Result:
    out_dir: Path
    saved: int = 0
    requested: int = 0
    examined: int = 0
    rejected: dict = field(default_factory=dict)
    cancelled: bool = False

    @property
    def shortfall(self):
        return not self.cancelled and self.saved < self.requested


def parse_settings(out_dir, n_images, min_short_side, split="val", seed="20261029"):
    """Turn raw (string) inputs into Settings; raise ValueError with a readable message."""
    out_dir = str(out_dir).strip()
    if not out_dir:
        raise ValueError("Choose an output folder.")

    def whole(value, label, lo, hi):
        try:
            text = str(value).strip()
            v = int(text)
        except ValueError:
            raise ValueError(f"{label} must be a whole number.") from None
        if not lo <= v <= hi:
            raise ValueError(f"{label} must be between {lo} and {hi}.")
        return v

    n = whole(n_images, "Number of images", 1, MAX_IMAGES)
    s = whole(min_short_side, "Minimum short side", 1, MAX_SHORT_SIDE)
    sd = whole(seed, "Seed", 0, 2**32 - 1)
    if split not in SPLITS:
        raise ValueError("Split must be 'val' or 'train'.")
    return Settings(out_dir=Path(out_dir).expanduser(), n_images=n, min_short_side=s, split=split, seed=sd)


def default_fetch(url, retries=3, timeout=30):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(0.5 * (i + 1))


def download_annotations(out_dir, split, progress=None, cancel=None, url=ANN_URL):
    """Return the path of instances_<split>2017.json, downloading the COCO annotation zip if needed."""
    progress = progress or (lambda *a: None)
    ann_dir = Path(out_dir) / "annotations"
    name = f"instances_{split}2017.json"
    target = ann_dir / name
    if target.exists():
        return target
    ann_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = ann_dir / "annotations.zip.part"
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp_zip, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress("annotations", done, total, f"Downloading COCO annotations: {done / 1e6:.0f} MB")
        progress("annotations", 1, 1, "Extracting annotations...")
        tmp_json = ann_dir / (name + ".part")
        with zipfile.ZipFile(tmp_zip) as z, z.open(f"annotations/{name}") as src, open(tmp_json, "wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp_json.replace(target)
    finally:
        tmp_zip.unlink(missing_ok=True)
        (ann_dir / (name + ".part")).unlink(missing_ok=True)
    return target


def _usable(a):
    return not a.get("iscrowd", 0) and isinstance(a.get("segmentation"), list)


def object_shares(coco, img, anns):
    """Share of the image covered by each non-crowd object's filled outline."""
    area = img["width"] * img["height"]
    return [float(coco.annToMask(a).sum()) / area for a in anns if _usable(a)]


def yolo_lines(img, anns, cat_index):
    W, H = img["width"], img["height"]
    lines = []
    for a in anns:
        if not _usable(a):
            continue
        for poly in a["segmentation"]:
            if len(poly) < 6:
                continue
            xy = np.clip(np.array(poly, dtype=float).reshape(-1, 2) / [W, H], 0, 1)
            lines.append(f"{cat_index[a['category_id']]} " + " ".join(f"{v:.6f}" for v in xy.ravel()))
    return lines


def generate(settings, progress=None, cancel=None, fetch=default_fetch, ann_url=ANN_URL):
    """Select and save settings.n_images images. Returns a Result (never raises on cancel)."""
    progress = progress or (lambda *a: None)
    out = Path(settings.out_dir)
    images_dir, labels_dir = out / "images", out / "labels"
    if images_dir.exists() and any(images_dir.iterdir()):
        raise FileExistsError(f"{images_dir} already contains files. Choose an empty or new output folder.")
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(exist_ok=True)
    res = Result(out_dir=out, requested=settings.n_images)
    rejected, log, seen = Counter(), [], set()

    try:
        ann_path = download_annotations(out, settings.split, progress, cancel, ann_url)
        progress("loading", 0, 1, "Reading annotations (can take a few seconds)...")
        coco = COCO(str(ann_path))
        cat_ids = sorted(coco.getCatIds())
        cat_index = {c: i for i, c in enumerate(cat_ids)}
        (out / "classes.txt").write_text("\n".join(c["name"] for c in coco.loadCats(cat_ids)) + "\n")
        ids = sorted(coco.getImgIds())
        random.Random(settings.seed).shuffle(ids)

        for img_id in ids:
            if res.saved >= settings.n_images:
                break
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            img = coco.loadImgs(img_id)[0]
            anns = coco.loadAnns(coco.getAnnIds(imgIds=img_id))
            reason = ""
            if min(img["width"], img["height"]) < settings.min_short_side:
                reason = f"short side < {settings.min_short_side} px"
            elif not any(settings.area_min <= s <= settings.area_max for s in object_shares(coco, img, anns)):
                reason = f"no non-crowd object covering {settings.area_min:.0%}-{settings.area_max:.0%} of the image"
            else:
                try:
                    data = fetch(img["coco_url"])
                except Exception:
                    data = None
                    reason = "download failed"
                if data is not None:
                    try:
                        pil = Image.open(io.BytesIO(data))
                        orient = pil.getexif().get(274, 1)
                        digest = hashlib.sha1(np.asarray(pil.convert("RGB")).tobytes()).hexdigest()
                    except Exception:
                        reason = "unreadable image"
                    else:
                        if orient != 1:
                            reason = "EXIF orientation differs from stored pixels"
                        elif digest in seen:
                            reason = "duplicate pixels"
                        else:
                            seen.add(digest)
                            stem = Path(img["file_name"]).stem
                            (labels_dir / f"{stem}.txt").write_text("\n".join(yolo_lines(img, anns, cat_index)) + "\n")
                            (images_dir / img["file_name"]).write_bytes(data)
                            res.saved += 1
                            progress("images", res.saved, settings.n_images,
                                     f"Saved {res.saved} of {settings.n_images}")
            if reason:
                rejected[reason] += 1
            log.append({"id": img_id, "width": img["width"], "height": img["height"],
                        "passed": not reason, "reason": reason})
            res.examined = len(log)
    except Cancelled:
        res.cancelled = True
    finally:
        res.rejected = dict(rejected)
        with open(out / "selection_log.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["id", "width", "height", "passed", "reason"])
            w.writeheader()
            w.writerows(log)
    return res


class Job:
    """Runs generate() in a background thread; the GUI polls .events (a queue)."""

    def __init__(self, settings, **kwargs):
        self.settings, self.kwargs = settings, kwargs
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancel_event.set()

    def _run(self):
        try:
            res = generate(self.settings, progress=lambda *a: self.events.put(("progress", *a)),
                           cancel=self.cancel_event, **self.kwargs)
            self.events.put(("done", res))
        except Exception as e:  # shown to the user, not swallowed
            self.events.put(("error", f"{type(e).__name__}: {e}"))
