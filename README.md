# P1S1 Dataset Generator

Downloads COCO 2017 images that meet the protocol v2 selection rules, from the terminal.

    python3 -m pip install -r requirements.txt
    python3 generate_dataset.py                 # asks for each setting; Enter keeps the default
    python3 generate_dataset.py -n 500 -s 400 -o ~/COCOSubset      # no questions
    python3 generate_dataset.py --help

Settings: number of images (`-n`), minimum short side in px (`-s`), COCO split (`--split val|train`;
`val` has 5,000 images and is enough for a few hundred, `train` needs a much larger annotation file
and asks for confirmation unless `-y`), seed (`--seed`), output folder (`-o`, must have no images
yet). The first run downloads the COCO annotation zip (about 250 MB) into `OUT/annotations/`; later
runs reuse it. Ctrl+C cancels cleanly and keeps the images already saved, with their labels.

Rules: short side >= the chosen minimum; at least one non-crowd object whose filled outline covers
1-20% of the image; EXIF orientation 1; each image once (by COCO id and by pixel content).

Output: `OUT/images/*.jpg` (original bytes, untouched), `OUT/labels/*.txt` (YOLO segmentation,
format read by `run_pilot.py`), `OUT/classes.txt`, `OUT/selection_log.csv` (why each image examined
was kept or rejected). The same seed and settings give the same images.

Exit codes: 0 finished, 1 error or invalid settings, 130 cancelled.

Tests (no network): `python3 -m unittest discover -s tests -v`

Files: `dataset_core.py` (selection and download logic), `generate_dataset.py` (terminal interface),
`tests/test_core.py`, `tests/test_cli.py`.

## Author and license
Author: Jose Luis del Valle (joseluisdelvalle@gmail.com).
Open source under the MIT License; see the `LICENSE` file.
