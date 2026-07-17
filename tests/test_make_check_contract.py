#!/usr/bin/env python3
"""Regression tests for `make check` child exit-code propagation."""

import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MakeCheckContractTest(unittest.TestCase):
    def _run_probe(self, exit_code: int, marker: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            probe = pathlib.Path(tmp) / "make_check_probe.py"
            probe.write_text(
                "import sys\nprint(%r)\nsys.exit(%d)\n" % (marker, exit_code),
                encoding="utf-8",
            )
            return subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "check",
                    "PURE_TESTS=",
                    "OFFLINE_TESTS=",
                    "PY_WAREHOUSE_TESTS=%s" % probe,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_failing_child_makes_make_check_red(self):
        result = self._run_probe(23, "MAKE_CHECK_FORCED_FAILURE")
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("MAKE_CHECK_FORCED_FAILURE", output)

    def test_passing_child_keeps_make_check_green(self):
        result = self._run_probe(0, "MAKE_CHECK_FORCED_PASS")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("MAKE_CHECK_FORCED_PASS", output)


if __name__ == "__main__":
    unittest.main()
