# Copyright (c) 2026 Jose Luis del Valle <joseluisdelvalle@gmail.com>
# Released under the MIT License (see LICENSE).
"""Offline tests with a synthetic COCO-style dataset. Run from the package folder:
    python3 -m unittest discover -s tests -v
"""
import csv
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dataset_core as dc  # noqa: E402


def jpeg(w, h, seed, exif_orientation=None):
    rng = np.random.default_rng(seed)
    im = Image.fromarray(rng.integers(0, 256, (h, w, 3), dtype=np.uint8))
    b = io.BytesIO()
    if exif_orientation:
        ex = Image.Exif()
        ex[274] = exif_orientation
        im.save(b, format="JPEG", exif=ex)
    else:
        im.save(b, format="JPEG")
    return b.getvalue()


def rect(w, h, share):
    """Axis-aligned rectangle polygon at the image origin covering about `share` of the image."""
    rw, rh = w * share ** 0.5, h * share ** 0.5
    return [0, 0, rw, 0, rw, rh, 0, rh]


# id -> (w, h, share or None, extra)  ; the expected outcome is in GOOD / REASON
SPEC = {
    1: (640, 480, 0.05, {}),                         # good
    2: (640, 300, 0.05, {}),                         # short side 300
    3: (640, 480, 0.005, {}),                        # object too small
    4: (640, 480, 0.30, {}),                         # object too big
    5: (640, 480, 0.10, {"crowd": True}),            # only a crowd annotation
    6: (640, 480, 0.05, {"orientation": 6}),         # EXIF rotated
    7: (640, 480, 0.05, {"same_pixels_as": 1}),      # duplicate of 1
    8: (640, 480, None, {}),                         # no annotations
    9: (640, 480, 0.05, {"fail": True}),             # download fails
    10: (500, 500, 0.15, {}),                        # good
    11: (640, 427, 0.02, {}),                        # good
    12: (480, 640, 0.19, {}),                        # good
    13: (640, 480, 0.05, {"corrupt": True}),         # not a JPEG
    14: (640, 480, 0.012, {}),                       # good
}
GOOD = {1, 10, 11, 12, 14}


def build_world(root):
    """Write instances_val2017.json into root/annotations and return (fetch function, fetch log)."""
    images, anns, blobs = [], [], {}
    for i, (w, h, share, ex) in SPEC.items():
        images.append({"id": i, "file_name": f"{i:012d}.jpg", "width": w, "height": h,
                       "coco_url": f"fake://{i}"})
        if share is not None:
            anns.append({"id": i, "image_id": i, "category_id": 18 if i % 2 else 1, "iscrowd": int(ex.get("crowd", 0)),
                         "segmentation": [rect(w, h, share)], "area": w * h * share, "bbox": [0, 0, 1, 1]})
        if "corrupt" in ex:
            blobs[f"fake://{i}"] = b"not an image"
        else:
            src = ex.get("same_pixels_as", i)
            blobs[f"fake://{i}"] = jpeg(w, h, src, ex.get("orientation"))
    data = {"images": images, "annotations": anns,
            "categories": [{"id": 1, "name": "person"}, {"id": 18, "name": "dog"}]}
    ann_dir = Path(root) / "annotations"
    ann_dir.mkdir(parents=True)
    (ann_dir / "instances_val2017.json").write_text(json.dumps(data))
    calls = []

    def fetch(url):
        calls.append(url)
        if url == "fake://9":
            raise OSError("network down")
        return blobs[url]

    return fetch, calls


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.fetch, self.calls = build_world(self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def run_gen(self, **kw):
        s = dc.Settings(out_dir=self.out, **kw)
        return dc.generate(s, fetch=self.fetch)

    def saved_ids(self):
        """Saved COCO ids; ids 1 and 7 hold the same pixels, so whichever comes first in the shuffle is
        kept and the other is a duplicate. Both are reported as 1."""
        return {1 if int(p.stem) == 7 else int(p.stem) for p in (self.out / "images").glob("*.jpg")}

    def twin(self):
        """The id (1 or 7) that was saved."""
        return 1 if (self.out / "images" / f"{1:012d}.jpg").exists() else 7

    def test_selects_exactly_the_images_that_meet_the_rules(self):
        r = self.run_gen(n_images=100, min_short_side=400)
        self.assertEqual(self.saved_ids(), GOOD)
        self.assertEqual(r.saved, len(GOOD))
        self.assertTrue(r.shortfall)
        self.assertEqual(r.examined, len(SPEC))
        self.assertEqual(r.rejected["short side < 400 px"], 1)
        self.assertEqual(r.rejected["download failed"], 1)
        self.assertEqual(r.rejected["duplicate pixels"], 1)
        self.assertEqual(r.rejected["EXIF orientation differs from stored pixels"], 1)
        self.assertEqual(r.rejected["unreadable image"], 1)
        self.assertEqual(sum(v for k, v in r.rejected.items() if k.startswith("no non-crowd")), 4)

    def test_every_saved_image_satisfies_every_rule(self):
        self.run_gen(n_images=100)
        seen = set()
        for p in (self.out / "images").glob("*.jpg"):
            im = Image.open(p)
            self.assertGreaterEqual(min(im.size), 400)
            self.assertEqual(im.getexif().get(274, 1), 1)
            digest = hash(np.asarray(im.convert("RGB")).tobytes())
            self.assertNotIn(digest, seen)
            seen.add(digest)

    def test_exact_count_requested(self):
        r = self.run_gen(n_images=3)
        self.assertEqual(r.saved, 3)
        self.assertFalse(r.shortfall)
        self.assertEqual(len(self.saved_ids()), 3)
        self.assertEqual(len(list((self.out / "labels").glob("*.txt"))), 3)

    def test_stops_downloading_once_enough(self):
        self.run_gen(n_images=2)
        self.assertLessEqual(len(self.calls), len(SPEC))
        fetched_good = [u for u in self.calls if int(u[7:]) in GOOD]
        self.assertEqual(len(fetched_good), 2)

    def test_min_short_side_setting_changes_the_selection(self):
        self.run_gen(n_images=100, min_short_side=250)
        self.assertEqual(self.saved_ids(), GOOD | {2})

    def test_area_range_matches_label_geometry(self):
        self.run_gen(n_images=100)
        for p in (self.out / "labels").glob("*.txt"):
            w, h, share, _ = SPEC[int(p.stem)]
            line = p.read_text().split("\n")[0].split()
            pts = np.array(line[1:], dtype=float).reshape(-1, 2)
            self.assertTrue((pts >= 0).all() and (pts <= 1).all())
            x, y = pts[:, 0], pts[:, 1]
            shoelace = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
            self.assertAlmostEqual(shoelace, share, delta=0.005)
            self.assertTrue(0.01 <= shoelace <= 0.20 + 0.005)

    def test_labels_class_indices_and_classes_file(self):
        self.run_gen(n_images=100)
        self.assertEqual((self.out / "classes.txt").read_text().split(), ["person", "dog"])
        # category 18 ("dog", odd ids) -> index 1; category 1 ("person", even ids) -> index 0
        self.assertTrue((self.out / "labels" / f"{self.twin():012d}.txt").read_text().startswith("1 "))
        self.assertTrue((self.out / "labels" / f"{10:012d}.txt").read_text().startswith("0 "))

    def test_saved_bytes_are_untouched_originals(self):
        self.run_gen(n_images=100)
        self.assertEqual((self.out / "images" / f"{self.twin():012d}.jpg").read_bytes(), jpeg(640, 480, 1))

    def test_selection_log(self):
        r = self.run_gen(n_images=100)
        with open(self.out / "selection_log.csv") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), r.examined)
        self.assertEqual(sum(row["passed"] == "True" for row in rows), r.saved)
        self.assertEqual({1 if int(row["id"]) == 7 else int(row["id"]) for row in rows if row["passed"] == "True"}, GOOD)

    def test_same_seed_same_result_different_seed_different_order(self):
        self.run_gen(n_images=3, seed=7)
        a = self.saved_ids()
        self.tmp.cleanup()
        self.setUp()
        self.run_gen(n_images=3, seed=7)
        self.assertEqual(a, self.saved_ids())
        self.tmp.cleanup()
        self.setUp()
        picks = set()
        for seed in range(10):
            import shutil
            shutil.rmtree(self.out / "images", ignore_errors=True)
            shutil.rmtree(self.out / "labels", ignore_errors=True)
            self.run_gen(n_images=3, seed=seed)
            picks.add(frozenset(self.saved_ids()))
        self.assertGreater(len(picks), 1)

    def test_refuses_non_empty_images_folder(self):
        (self.out / "images").mkdir()
        (self.out / "images" / "keep.jpg").write_bytes(b"x")
        with self.assertRaises(FileExistsError):
            self.run_gen(n_images=1)
        self.assertEqual((self.out / "images" / "keep.jpg").read_bytes(), b"x")

    def test_cancel_before_start_saves_nothing(self):
        ev = threading.Event()
        ev.set()
        r = dc.generate(dc.Settings(out_dir=self.out, n_images=5), cancel=ev, fetch=self.fetch)
        self.assertTrue(r.cancelled)
        self.assertFalse(r.shortfall)
        self.assertEqual(r.saved, 0)
        self.assertTrue((self.out / "selection_log.csv").exists())

    def test_cancel_midway_keeps_images_and_labels_consistent(self):
        ev = threading.Event()

        def progress(stage, done, total, msg):
            if stage == "images" and done == 2:
                ev.set()

        r = dc.generate(dc.Settings(out_dir=self.out, n_images=5), progress=progress, cancel=ev, fetch=self.fetch)
        self.assertTrue(r.cancelled)
        self.assertEqual(r.saved, 2)
        self.assertEqual({p.stem for p in (self.out / "images").glob("*.jpg")},
                         {p.stem for p in (self.out / "labels").glob("*.txt")})

    def test_progress_events(self):
        events = []
        dc.generate(dc.Settings(out_dir=self.out, n_images=3), progress=lambda *a: events.append(a), fetch=self.fetch)
        img = [e for e in events if e[0] == "images"]
        self.assertEqual([e[1] for e in img], [1, 2, 3])
        self.assertTrue(all(e[2] == 3 for e in img))


class AnnotationDownloadTests(unittest.TestCase):
    def make_zip(self, folder, split="val"):
        z = Path(folder) / "ann.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(f"annotations/instances_{split}2017.json", '{"images": []}')
            zf.writestr("annotations/other.json", "{}")
        return z

    def test_downloads_and_extracts_only_the_wanted_file(self):
        with tempfile.TemporaryDirectory() as t:
            z = self.make_zip(t)
            events = []
            path = dc.download_annotations(Path(t) / "out", "val", lambda *a: events.append(a), url=z.as_uri())
            self.assertEqual(path.read_text(), '{"images": []}')
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["instances_val2017.json"])
            self.assertTrue(events and events[0][0] == "annotations")

    def test_cached_file_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as t:
            ann = Path(t) / "out" / "annotations"
            ann.mkdir(parents=True)
            (ann / "instances_val2017.json").write_text("cached")
            p = dc.download_annotations(Path(t) / "out", "val", url="file:///does/not/exist.zip")
            self.assertEqual(p.read_text(), "cached")

    def test_cancel_leaves_no_partial_files(self):
        with tempfile.TemporaryDirectory() as t:
            z = self.make_zip(t)
            ev = threading.Event()
            ev.set()
            with self.assertRaises(dc.Cancelled):
                dc.download_annotations(Path(t) / "out", "val", cancel=ev, url=z.as_uri())
            self.assertEqual(list((Path(t) / "out" / "annotations").iterdir()), [])

    def test_bad_url_raises_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(Exception):
                dc.download_annotations(Path(t) / "out", "val", url="file:///does/not/exist.zip")
            self.assertEqual(list((Path(t) / "out" / "annotations").iterdir()), [])

    def test_full_generate_from_zip(self):
        """generate() on an empty folder downloads (here: from a local zip) and then selects."""
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "src"
            fetch, _ = build_world(src)
            z = Path(t) / "ann.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.write(src / "annotations" / "instances_val2017.json", "annotations/instances_val2017.json")
            out = Path(t) / "out"
            r = dc.generate(dc.Settings(out_dir=out, n_images=3), fetch=fetch, ann_url=z.as_uri())
            self.assertEqual(r.saved, 3)


class ParseSettingsTests(unittest.TestCase):
    def ok(self, **kw):
        base = dict(out_dir="/tmp/x", n_images="10", min_short_side="400", split="val", seed="5")
        base.update(kw)
        return dc.parse_settings(**base)

    def test_valid(self):
        s = self.ok(n_images=" 25 ")
        self.assertEqual((s.n_images, s.min_short_side, s.split, s.seed), (25, 400, "val", 5))

    def test_bad_values(self):
        for kw in ({"n_images": "0"}, {"n_images": "-3"}, {"n_images": "abc"}, {"n_images": ""}, {"n_images": "2.5"},
                   {"n_images": str(dc.MAX_IMAGES + 1)}, {"min_short_side": "0"}, {"min_short_side": "641"},
                   {"min_short_side": "x"}, {"split": "test"}, {"seed": "-1"}, {"seed": "q"}, {"out_dir": "  "}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.ok(**kw)

    def test_limits_accepted(self):
        self.ok(n_images="1", min_short_side="1")
        self.ok(n_images=str(dc.MAX_IMAGES), min_short_side=str(dc.MAX_SHORT_SIDE))


class JobTests(unittest.TestCase):
    def test_job_reports_progress_then_done(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "out"
            fetch, _ = build_world(out)
            job = dc.Job(dc.Settings(out_dir=out, n_images=2), fetch=fetch)
            job.start()
            job.thread.join(30)
            kinds = []
            while not job.events.empty():
                kinds.append(job.events.get())
            self.assertEqual(kinds[-1][0], "done")
            self.assertEqual(kinds[-1][1].saved, 2)
            self.assertIn("progress", [k[0] for k in kinds])

    def test_job_reports_errors(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "out"
            (out / "images").mkdir(parents=True)
            (out / "images" / "a.jpg").write_bytes(b"x")
            job = dc.Job(dc.Settings(out_dir=out, n_images=2))
            job.start()
            job.thread.join(30)
            kind, msg = job.events.get(timeout=5)
            self.assertEqual(kind, "error")
            self.assertIn("FileExistsError", msg)

    def test_job_cancel(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "out"
            fetch, _ = build_world(out)
            job = dc.Job(dc.Settings(out_dir=out, n_images=5), fetch=fetch)
            job.cancel()
            job.start()
            job.thread.join(30)
            last = None
            while not job.events.empty():
                last = job.events.get()
            self.assertEqual(last[0], "done")
            self.assertTrue(last[1].cancelled)


if __name__ == "__main__":
    unittest.main()
