# P1S1 Dataset Generator

Downloads COCO 2017 images that meet the protocol v2 selection rules, from the terminal.

    python3 -m venv .venv && source .venv/bin/activate
    python3 -m pip install -r requirements.txt     # pinned versions
    python3 generate_dataset.py                 # asks for each setting; Enter keeps the default
    python3 generate_dataset.py -n 500 -s 400 -o ~/COCOSubset      # no questions
    python3 generate_dataset.py --help

## Where this script sits in the experiment

The 21 steps of the experiment protocol (final version, approved 29 October 2026). Each script reads only the output of the script before it.

```mermaid
flowchart TB
  subgraph A["A. Fix the protocol before any data exists"]
    direction LR
    S1["1. Criteria table from the literature<br/><i>document</i>"]
    S2["2. Pre-register the protocol<br/><i>document</i>"]
    S1 --> S2
  end
  subgraph B["B. Build the dataset"]
    direction LR
    S3["3. Select 2,000 originals<br/>from COCO train + val<br/><b>P1S1</b>"]
    S4["4. Partition<br/>1,000 calibration / 1,000 test<br/><b>P1S2</b>"]
    S5["5. Catalogues<br/>24 benign + 15 altering cells<br/><b>P1S2</b>"]
    S6["6. Generate and store<br/>every copy<br/><b>P1S2</b>"]
    S7["7. Visual QA<br/>contact sheets<br/><b>P1S2</b><br/><i>sheets made by P1S2;<br/>inspection by the researcher</i>"]
    S3 --> S4 --> S5 --> S6 --> S7
  end
  subgraph C["C. Hash and measure"]
    direction LR
    S8["8. Hash: aHash, dHash,<br/>pHash, wHash"]
    S9["9. Distances to the original<br/>and unrelated pairs"]
    S10["10. Sanity checks<br/>and counts per cell"]
    S8 --> S9 --> S10
  end
  subgraph D["D. Phase 1, calibration half"]
    direction LR
    S11["11. Describe distributions"]
    S12["12. Separation P0<br/>Wilcoxon, AUC"]
    S13["13. Thresholds at α and β"]
    S14["14. Uncertainty<br/>bootstrap, beta-binomial"]
    S15["15. Cell check"]
    S11 --> S12 --> S13 --> S14 --> S15
  end
  subgraph E["E. Phase 2, test half"]
    direction LR
    S16["16. Validate thresholds"]
    S17["17. Sweep 2,145<br/>threshold pairs"]
    S18["18. Map: heatmaps,<br/>operating point"]
    S19["19. Break down by edit,<br/>area and cell"]
    S20["20. Compare hashes"]
    S16 --> S17 --> S18 --> S19 --> S20
  end
  subgraph F["F. Report"]
    direction LR
    S21["21. Report per hash"]
  end
  A --> B --> C --> D --> E --> F
  classDef this fill:#0b5cad,stroke:#0b5cad,color:#ffffff,stroke-width:3px
  classDef other fill:#cfe3f7,stroke:#0b5cad,color:#0b2a4a
  classDef manual fill:#eeeeee,stroke:#888888,color:#333333
  classDef pending fill:#ffffff,stroke:#999999,color:#555555,stroke-dasharray:4 3
  class S3 this
  class S4,S5,S6,S7 other
  class S1,S2 manual
  class S8,S9,S10,S11,S12,S13,S14,S15,S16,S17,S18,S19,S20,S21 pending
```

**Dark blue:** this repository (P1S1 · COCO_Dataset-Retriever), protocol step 3. **Light blue:** the other finished script (P1S2 · P1S2_Copy_Generator), steps 4, 5, 6, 7. **Grey:** protocol documents, not scripts. **Dashed:** scripts not written yet. Step 7 is partly automated: P1S2 draws the contact sheets and the researcher inspects them.

## Usage

Settings: number of images (`-n`), minimum short side in px (`-s`), COCO pool (`--split val|train|trainval`;
`val` has 5,000 images and is enough for a few hundred; `train` and `trainval` need the much larger train
annotation file and ask for confirmation unless `-y`), seed (`--seed`), output folder (`-o`, must have no
images yet). The first run downloads the COCO annotation zip (about 250 MB) into `OUT/annotations/`; later
runs reuse it. Ctrl+C cancels cleanly and keeps the images already saved, with their labels.

The protocol's pool is train2017 + val2017:

    python3 generate_dataset.py -n 2000 --split trainval -o ~/COCOProtocol -y

Rules: short side >= the chosen minimum; at least one non-crowd object whose filled outline covers
1-20% of the image; EXIF orientation 1; each image once (by COCO id and by pixel content).

Output: `OUT/images/*.jpg` (original bytes, untouched), `OUT/labels/*.txt` (YOLO segmentation,
format read by `run_pilot.py`), `OUT/classes.txt`, `OUT/selection_log.csv` (why each image examined
was kept or rejected, and its split), `OUT/images_manifest.csv` (one row per saved image: order, id,
split, size, licence id, URL, SHA-256 of the saved bytes, SHA-1 of the decoded pixels),
`OUT/run_metadata.json` (settings, result counts, SHA-256 of the annotation files, of the output
tables and of the two scripts, Python and package versions). The same seed and settings give the
same images; `val` and `train` select exactly what version 1.0 selected.

Exit codes: 0 finished, 1 error or invalid settings, 130 cancelled.

Tests (no network): `python3 -m unittest discover -s tests -v`

Files: `dataset_core.py` (selection and download logic), `generate_dataset.py` (terminal interface),
`tests/test_core.py`, `tests/test_cli.py`.

## Author and license
Author: Jose Luis del Valle (joseluisdelvalle@gmail.com).
Open source under the MIT License; see the `LICENSE` file.
