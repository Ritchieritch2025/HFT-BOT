#!/usr/bin/env python3
"""Offline tests for tools/warehouse_status.py (three-layer warehouse panel).

Fixture-driven: builds a synthetic staging.duckdb + archive partitions and
checks the status summary — present vs absent warehouse, staging row counts,
schema check, source_kind classification (operator_capture vs
synthetic_fixture), archive partition/manifest reconciliation.

Run: python3 tests/test_warehouse_status.py
"""
import csv
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ingest  # noqa: E402
import warehouse_status as ws  # noqa: E402


def _staging(wh, checkpoint_file, n_l1=3):
    import duckdb
    os.makedirs(wh, exist_ok=True)
    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    con.execute(ingest.STAGING_DDL)
    for i in range(n_l1):
        con.execute("INSERT INTO orderbooks_l1 VALUES (?, 'KXBTC-X-B1', 'KXBTC', "
                    "'KXBTC-X', 'Crypto', 'BTC', 'BTC', 'A', 40, 1, 42, 1, "
                    "41, 1, 1, ?)", [1_783_300_000_000_000 + i, i == 0])
    con.execute("INSERT INTO checkpoint VALUES (?, 100, 1783300000000000)",
                [checkpoint_file])
    con.close()


class TestWarehouseStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="whstatus-")
        self.wh = os.path.join(self.tmp, "warehouse")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absent_warehouse(self):
        s = ws.collect_status(self.wh)
        self.assertFalse(s["present"])
        self.assertEqual(s["source_kind"], "unknown")

    def test_operator_capture_source(self):
        _staging(self.wh, "/repo/work/raw/date=2026-07-06/firehose_01.ndjson")
        s = ws.collect_status(self.wh)
        self.assertTrue(s["present"])
        self.assertTrue(s["schema_ok"])
        self.assertEqual(s["source_kind"], "operator_capture")
        self.assertEqual(s["row_counts"]["orderbooks_l1"], 3)
        self.assertIn("finished_at", s["last_run"])

    def test_synthetic_fixture_source(self):
        _staging(self.wh, "/repo/build/scratch/demo_capture_fixture.ndjson")
        s = ws.collect_status(self.wh)
        self.assertEqual(s["source_kind"], "synthetic_fixture")

    def test_manifest_file_reconciliation(self):
        _staging(self.wh, "/repo/work/raw/date=2026-07-06/firehose_01.ndjson")
        pdir = os.path.join(self.wh, "facts", "trades", "category=Sports",
                            "subcategory=MLB", "date=2026-07-05")
        os.makedirs(pdir)
        fpath = os.path.join(pdir, "trades__Sports__MLB__2026-07-05.csv.gz")
        open(fpath, "wb").write(b"x")
        # manifest listing MORE rows than files -> warning
        with open(os.path.join(self.wh, "manifest.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "table", "category", "subcategory", "row_count",
                        "file_path", "file_md5", "created_ts"])
            w.writerow(["2026-07-05", "trades", "Sports", "MLB", 1, fpath, "x", "t"])
            w.writerow(["2026-07-05", "trades", "Sports", "NBA", 1, "gone", "x", "t"])
        s = ws.collect_status(self.wh)
        self.assertEqual(s["partitions"], 1)
        self.assertEqual(s["latest_partition_date"], "2026-07-05")
        self.assertTrue(any("manifest rows" in w_ for w_ in s["warnings"]), s["warnings"])

    def test_archive_only_is_present(self):
        pdir = os.path.join(self.wh, "facts", "orderbooks_l1", "category=Crypto",
                            "subcategory=BTC", "date=2026-07-05")
        os.makedirs(pdir)
        open(os.path.join(pdir, "orderbooks_l1__Crypto__BTC__2026-07-05.parquet"),
             "wb").write(b"x")
        s = ws.collect_status(self.wh)
        self.assertTrue(s["present"])
        self.assertTrue(s["schema_ok"])


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=0).result
    ran = result.testsRun
    bad = len(result.failures) + len(result.errors)
    for _ in range(ran - bad):
        print("PASS: warehouse status test")
    for fail in result.failures + result.errors:
        print("FAIL: %s" % (fail[0],))
    print("ALL PASS" if bad == 0 else "FAILURES")
    sys.exit(1 if bad else 0)
