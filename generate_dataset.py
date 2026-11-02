# Copyright (c) 2026 Jose Luis del Valle <joseluisdelvalle@gmail.com>
# Released under the MIT License (see LICENSE).
"""Terminal interface for the P1S1 dataset generator.

    python3 generate_dataset.py                      # asks for each setting (Enter keeps the default)
    python3 generate_dataset.py -n 500 -s 400 -o ~/COCOSubset      # no questions
    python3 generate_dataset.py -n 2000 --split trainval -o ~/COCOProtocol -y   # the protocol's pool
    python3 generate_dataset.py --help

Ctrl+C cancels cleanly: images already saved are kept, with their labels.
"""
import argparse
import queue
import sys
from pathlib import Path

import dataset_core as dc

DEFAULTS = {"n_images": "500", "min_short_side": "400", "split": "val", "seed": "20261029",
            "out_dir": str(Path.home() / "COCOSubset")}


def build_parser():
    p = argparse.ArgumentParser(description="Download COCO 2017 images that meet the selection rules "
                                            "(short side, one object covering 1-20%, EXIF orientation 1, no duplicates).")
    p.add_argument("-n", "--n-images", help=f"number of images (1-{dc.MAX_IMAGES}, default {DEFAULTS['n_images']})")
    p.add_argument("-s", "--min-short-side", help=f"minimum short side in px (1-{dc.MAX_SHORT_SIDE}, default {DEFAULTS['min_short_side']})")
    p.add_argument("--split", choices=dc.SPLITS,
                   help="COCO 2017 pool: val, train, or trainval = train + val, the protocol's pool (default val)")
    p.add_argument("--seed", help=f"random seed (default {DEFAULTS['seed']})")
    p.add_argument("-o", "--out-dir", help=f"output folder (default {DEFAULTS['out_dir']})")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation (train or trainval)")
    return p


def ask_settings(input_fn, out):
    """Prompt for every setting; ask again until the answer is valid (Enter keeps the default)."""
    prompts = [("n_images", "Number of images"), ("min_short_side", "Minimum short side (px)"),
               ("split", "COCO split (val/train/trainval)"), ("seed", "Seed"), ("out_dir", "Output folder")]
    answers = {}
    for key, label in prompts:
        while True:
            reply = input_fn(f"{label} [{DEFAULTS[key]}]: ").strip() or DEFAULTS[key]
            try:
                dc.parse_settings(**{**DEFAULTS, key: reply})   # the defaults are valid, so only this field can fail
            except ValueError as e:
                print(f"  {e}", file=out)
                continue
            answers[key] = reply
            break
    return answers


class Display:
    """One-line progress bar on a terminal; a line every 5% when output is piped."""

    def __init__(self, out):
        self.out, self.tty = out, out.isatty()
        self.open_line, self.last_stage, self.last_step = False, None, -1

    def update(self, stage, done, total, msg):
        pct = int(100 * done / total) if total else 0
        text = f"[{'#' * (pct // 4):<25}] {done}/{total}" if stage == "images" else msg
        if self.tty:
            self.out.write("\r" + text.ljust(70))
            self.out.flush()
            self.open_line = True
            return
        step = pct // 5 if stage == "images" else 0
        if stage != self.last_stage or step != self.last_step:
            print(text, file=self.out)
        self.last_stage, self.last_step = stage, step

    def end(self):
        if self.tty and self.open_line:
            self.out.write("\n")
        self.open_line = False


def main(argv=None, input_fn=input, out=None, job_kwargs=None, interactive=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    given = {k: v for k, v in vars(args).items() if k != "yes" and v is not None}
    if interactive is None:
        interactive = not given and not args.yes and sys.stdin.isatty()
    if interactive:
        raw = {**DEFAULTS, **ask_settings(input_fn, out)}
    else:
        raw = {**DEFAULTS, **given}
    try:
        settings = dc.parse_settings(**raw)
    except ValueError as e:
        print(f"Error: {e}", file=out)
        return 1
    images = settings.out_dir / "images"
    if images.exists() and any(images.iterdir()):
        print(f"Error: {images} already has files. Choose a new or empty output folder.", file=out)
        return 1
    if settings.split in ("train", "trainval") and not args.yes:
        reply = input_fn("The train annotations are large (about 450 MB unpacked, several GB of RAM). Continue? [y/N]: ")
        if reply.strip().lower() not in ("y", "yes"):
            print("Cancelled.", file=out)
            return 130
    print(f"Downloading {settings.n_images} images (short side >= {settings.min_short_side} px, "
          f"split {settings.split}, seed {settings.seed}) into {settings.out_dir}", file=out)
    if not all((settings.out_dir / "annotations" / f"instances_{p}2017.json").exists()
               for p in dc.split_parts(settings.split)):
        print("First run: the COCO annotations (about 250 MB) are downloaded once and reused. Ctrl+C cancels.", file=out)

    job, display, result, interrupted = dc.Job(settings, **(job_kwargs or {})), Display(out), None, False
    job.start()
    while result is None:
        try:
            kind, *rest = job.events.get(timeout=0.2)
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            if not interrupted:
                interrupted = True
                job.cancel()
                display.end()
                print("Cancelling (keeping what is saved so far)...", file=out)
            continue
        if kind == "progress":
            display.update(*rest)
        elif kind == "error":
            display.end()
            print(f"Error: {rest[0]}", file=out)
            return 1
        else:
            result = rest[0]
    display.end()
    for reason, n in sorted(result.rejected.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {n}: {reason}", file=out)
    word = "Cancelled" if result.cancelled else "Done"
    print(f"{word}: saved {result.saved} of {result.requested} images (examined {result.examined}) in {result.out_dir}", file=out)
    print(f"Settings, checksums and versions: {result.out_dir / 'run_metadata.json'}; "
          f"per-image checksums: {result.out_dir / 'images_manifest.csv'}", file=out)
    if result.shortfall:
        print("Fewer images met the criteria than requested. Try a lower short side or --split train.", file=out)
    return 130 if result.cancelled else 0


if __name__ == "__main__":
    sys.exit(main())
