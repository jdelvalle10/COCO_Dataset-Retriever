# Copyright (c) 2026 Jose Luis del Valle <joseluisdelvalle@gmail.com>
# Released under the MIT License (see LICENSE).
"""Tests of the terminal interface (synthetic COCO data, no network)."""
import _thread
import io
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import generate_dataset as cli  # noqa: E402
from test_core import GOOD, build_world  # noqa: E402


class TTY(io.StringIO):
    def isatty(self):
        return True


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.fetch, _ = build_world(self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, answers=(), stream=None, fetch=None, interactive=None):
        stream = stream or io.StringIO()
        it = iter(answers)
        code = cli.main(argv, input_fn=lambda prompt: (stream.write(prompt), next(it))[1], out=stream,
                        job_kwargs={"fetch": fetch or self.fetch}, interactive=interactive)
        return code, stream.getvalue()

    def images(self):
        return {int(p.stem) for p in (self.out / "images").glob("*.jpg")}

    def test_flags_run_without_questions(self):
        code, text = self.run_cli(["-n", "3", "-s", "400", "-o", str(self.out), "--seed", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.images()), 3)
        self.assertIn("Done: saved 3 of 3 images", text)
        self.assertNotIn("Number of images [", text)

    def test_shortfall_message_and_skip_summary(self):
        code, text = self.run_cli(["-n", "100", "-o", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual({1 if i == 7 else i for i in self.images()}, GOOD)
        self.assertIn("Fewer images met the criteria", text)
        self.assertIn("skipped 1: short side < 400 px", text)

    def test_min_short_side_flag(self):
        self.run_cli(["-n", "100", "-s", "250", "-o", str(self.out)])
        self.assertIn(2, self.images())

    def test_invalid_values_exit_1_and_download_nothing(self):
        for argv in (["-n", "0"], ["-n", "x"], ["-s", "9999"], ["--seed", "-1"], ["-n", "5", "-o", "  "]):
            with self.subTest(argv=argv):
                code, text = self.run_cli(argv + ([] if "-o" in argv else ["-o", str(self.out)]))
                self.assertEqual(code, 1)
                self.assertIn("Error:", text)
        self.assertFalse((self.out / "images").exists())

    def test_bad_split_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli(["--split", "test", "-o", str(self.out)])
        self.assertEqual(cm.exception.code, 2)

    def test_non_empty_folder_exit_1_files_untouched(self):
        (self.out / "images").mkdir()
        (self.out / "images" / "keep.jpg").write_bytes(b"x")
        code, text = self.run_cli(["-n", "2", "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("already has files", text)
        self.assertEqual((self.out / "images" / "keep.jpg").read_bytes(), b"x")

    def test_interactive_prompts_with_defaults_and_retry(self):
        answers = ["abc", "2", "", "", "", str(self.out)]   # invalid count, then 2; defaults for size/split/seed
        code, text = self.run_cli([], answers, interactive=True)
        self.assertEqual(code, 0)
        self.assertIn("Number of images must be a whole number.", text)
        self.assertIn("Minimum short side (px) [400]:", text)
        self.assertEqual(len(self.images()), 2)

    def test_interactive_rejects_bad_split_and_folder_then_accepts(self):
        answers = ["2", "999", "400", "test", "val", "", str(self.out)]
        code, text = self.run_cli([], answers, interactive=True)
        self.assertEqual(code, 0)
        self.assertIn("between 1 and 640", text)
        self.assertIn("Split must be", text)

    def test_train_split_asks_for_confirmation(self):
        code, text = self.run_cli(["--split", "train", "-o", str(self.out)], answers=["n"])
        self.assertEqual(code, 130)
        self.assertIn("Continue? [y/N]", text)
        self.assertFalse((self.out / "images").exists())

    def test_trainval_asks_for_confirmation(self):
        code, text = self.run_cli(["--split", "trainval", "-o", str(self.out)], answers=["n"])
        self.assertEqual(code, 130)
        self.assertIn("Continue? [y/N]", text)

    def test_end_message_points_to_records(self):
        code, text = self.run_cli(["-n", "2", "-o", str(self.out)])
        self.assertEqual(code, 0)
        self.assertIn("run_metadata.json", text)
        self.assertTrue((self.out / "images_manifest.csv").exists())

    def test_progress_bar_on_terminal(self):
        code, text = self.run_cli(["-n", "3", "-o", str(self.out)], stream=TTY())
        self.assertEqual(code, 0)
        self.assertIn("] 3/3", text)
        self.assertIn("\r", text)

    def test_piped_output_has_no_carriage_returns(self):
        _, text = self.run_cli(["-n", "3", "-o", str(self.out)])
        self.assertNotIn("\r", text)
        self.assertIn("3/3", text)

    def test_ctrl_c_cancels_and_keeps_consistent_files(self):
        calls = []
        inner = self.fetch

        def fetch(url):
            calls.append(url)
            if len(calls) == 3:
                _thread.interrupt_main()    # what Ctrl+C does
                time.sleep(0.6)
            return inner(url)

        code, text = self.run_cli(["-n", "100", "-o", str(self.out)], fetch=fetch)
        self.assertEqual(code, 130)
        self.assertIn("Cancelling", text)
        self.assertIn("Cancelled: saved", text)
        self.assertNotIn("Fewer images met", text)
        self.assertEqual({p.stem for p in (self.out / "images").glob("*.jpg")},
                         {p.stem for p in (self.out / "labels").glob("*.txt")})
        self.assertLess(len(self.images()), len(GOOD) + 1)

    def test_runs_as_a_real_command(self):
        p = subprocess.run([sys.executable, str(ROOT / "generate_dataset.py"), "-n", "0", "-o", str(self.out)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Number of images must be between 1 and", p.stdout)


if __name__ == "__main__":
    unittest.main()
