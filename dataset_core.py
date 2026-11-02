# Copyright (c) 2026 Jose Luis del Valle <joseluisdelvalle@gmail.com>
# Released under the MIT License (see LICENSE).
"""Core of the P1S1 dataset generator: pick COCO 2017 images that meet the selection rules and save them.

No GUI code lives here, so everything can be tested without a display.

Rules (protocol v2 selection): short side >= min_short_side; at least one non-crowd object whose
filled outline covers area_min..area_max of the image; EXIF orientation 1; each image once
(by COCO id and by decoded pixels). Pool: COCO 2017 val, train, or train + val (the protocol's pool).

Output: OUT_DIR/images/*.jpg, OUT_DIR/labels/*.txt (YOLO segmentation: class x1 y1 x2 y2 ...,
normalised), OUT_DIR/classes.txt, OUT_DIR/selection_log.csv, OUT_DIR/images_manifest.csv (one row per
saved image, with checksums), OUT_DIR/run_metadata.json (settings, result, input and script checksums,
software versions), OUT_DIR/annotations/ (cached).
"""
import csv
import hashlib
import io
import json
import platform
import queue
import random
import shutil
import threading
import time
import urllib.request
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path

import numpy as np
from PIL import Image
from pycocotools.coco import COCO

ANN_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
SPLITS = ("val", "train", "trainval")      # trainval = train2017 + val2017, the protocol's pool
VERSION = "1.1.0"
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
        raise ValueError("Split must be 'val', 'train' or 'trainval'.")
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


def split_parts(split):
    """COCO 2017 splits that make up a pool: 'trainval' -> ['train', 'val']."""
    return ["train", "val"] if split == "trainval" else [split]


def download_annotations(out_dir, split, progress=None, cancel=None, url=ANN_URL):
    """Return the path of instances_<split>2017.json (split 'val' or 'train'), downloading if needed."""
    return download_annotation_files(out_dir, [split], progress, cancel, url)[0]


def download_annotation_files(out_dir, splits, progress=None, cancel=None, url=ANN_URL):
    """Return the paths of instances_<s>2017.json for each split, downloading the COCO annotation zip
    at most once and extracting only the files that are missing."""
    progress = progress or (lambda *a: None)
    ann_dir = Path(out_dir) / "annotations"
    names = [f"instances_{s}2017.json" for s in splits]
    targets = [ann_dir / n for n in names]
    missing = [n for n, t in zip(names, targets) if not t.exists()]
    if not missing:
        return targets
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
        with zipfile.ZipFile(tmp_zip) as z:
            for name in missing:
                tmp_json = ann_dir / (name + ".part")
                with z.open(f"annotations/{name}") as src, open(tmp_json, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                tmp_json.replace(ann_dir / name)
    finally:
        tmp_zip.unlink(missing_ok=True)
        for name in missing:
            (ann_dir / (name + ".part")).unlink(missing_ok=True)
    return targets


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def environment():
    """Python, platform and package versions, recorded so a run can be reproduced."""
    pk = {}
    for name in ("numpy", "pillow", "pycocotools"):
        try:
            pk[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pk[name] = None
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(), "packages": pk}


def script_checksums():
    here = Path(__file__).resolve().parent
    return {p.name: sha256_file(p) for p in (here / "dataset_core.py", here / "generate_dataset.py") if p.exists()}


MANIFEST_FIELDS = ["order", "id", "split", "file_name", "width", "height", "license_id", "coco_url",
                   "bytes_sha256", "pixels_sha1", "objects_in_range"]


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
    rejected, log, seen, manifest = Counter(), [], set(), []
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ann_files = {}

    try:
        parts = split_parts(settings.split)
        ann_paths = download_annotation_files(out, parts, progress, cancel, ann_url)
        progress("loading", 0, 1, "Checksumming and reading annotations (can take a while for train)...")
        ann_files = {p.name: sha256_file(p) for p in ann_paths}
        cocos, owner = {}, {}
        for part, path in zip(parts, ann_paths):
            cocos[part] = COCO(str(path))
            for i in cocos[part].getImgIds():
                if i in owner:
                    raise ValueError(f"Image id {i} appears in both {owner[i]} and {part} annotations.")
                owner[i] = part
        first = cocos[parts[0]]
        cat_ids = sorted(first.getCatIds())
        for part in parts[1:]:
            if sorted(cocos[part].getCatIds()) != cat_ids:
                raise ValueError("The annotation files do not share the same categories.")
        cat_index = {c: i for i, c in enumerate(cat_ids)}
        (out / "classes.txt").write_text("\n".join(c["name"] for c in first.loadCats(cat_ids)) + "\n")
        ids = sorted(owner)
        random.Random(settings.seed).shuffle(ids)

        for img_id in ids:
            if res.saved >= settings.n_images:
                break
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            part = owner[img_id]
            coco = cocos[part]
            img = coco.loadImgs(img_id)[0]
            anns = coco.loadAnns(coco.getAnnIds(imgIds=img_id))
            reason = ""
            in_range = 0
            if min(img["width"], img["height"]) < settings.min_short_side:
                reason = f"short side < {settings.min_short_side} px"
            elif not (in_range := sum(settings.area_min <= s <= settings.area_max
                                      for s in object_shares(coco, img, anns))):
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
                            manifest.append({"order": res.saved, "id": img_id, "split": part,
                                             "file_name": img["file_name"], "width": img["width"],
                                             "height": img["height"], "license_id": img.get("license"),
                                             "coco_url": img["coco_url"],
                                             "bytes_sha256": hashlib.sha256(data).hexdigest(),
                                             "pixels_sha1": digest, "objects_in_range": in_range})
                            res.saved += 1
                            progress("images", res.saved, settings.n_images,
                                     f"Saved {res.saved} of {settings.n_images}")
            if reason:
                rejected[reason] += 1
            log.append({"id": img_id, "split": part, "width": img["width"], "height": img["height"],
                        "passed": not reason, "reason": reason})
            res.examined = len(log)
    except Cancelled:
        res.cancelled = True
    finally:
        res.rejected = dict(rejected)
        with open(out / "selection_log.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["id", "split", "width", "height", "passed", "reason"])
            w.writeheader()
            w.writerows(log)
        with open(out / "images_manifest.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            w.writeheader()
            w.writerows(manifest)
        write_metadata(out, settings, res, started, ann_files, ann_url)
    return res


def write_metadata(out, settings, res, started, ann_files, ann_url):
    """run_metadata.json: everything needed to reproduce and audit this selection."""
    s = asdict(settings)
    s["out_dir"] = str(settings.out_dir)
    outputs = {}
    for name in ("selection_log.csv", "images_manifest.csv", "classes.txt"):
        if (out / name).exists():
            outputs[name] = sha256_file(out / name)
    meta = {"tool": "P1S1 dataset generator", "version": VERSION, "settings": s,
            "pool": [f"{p}2017" for p in split_parts(settings.split)],
            "started": started, "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "result": {"saved": res.saved, "requested": res.requested, "examined": res.examined,
                       "cancelled": res.cancelled, "rejected": res.rejected},
            "annotations": {"url": ann_url, "sha256": ann_files},
            "outputs_sha256": outputs, "scripts_sha256": script_checksums(), "environment": environment()}
    tmp = out / "run_metadata.json.part"
    tmp.write_text(json.dumps(meta, indent=2) + "\n")
    tmp.replace(out / "run_metadata.json")


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
