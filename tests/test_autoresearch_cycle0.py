import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import duckdb


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "research" / "autoresearch_cycle.py"


class TestAutoresearchCycle0(unittest.TestCase):
    def _release(self, root: Path, verified=True) -> Path:
        rid = "2026-07-13__seal-aaaaaaaa__pub-1111111111111111"
        release = root / "cache" / "releases" / rid
        l1_dir = release / "facts" / "orderbooks_l1"
        tr_dir = release / "facts" / "trades"
        l2_dir = release / "facts" / "orderbooks_full"
        rfq_dir = release / "raw_rfq" / "date=2026-07-13"
        for path in (l1_dir, tr_dir, l2_dir, rfq_dir):
            path.mkdir(parents=True)
        con = duckdb.connect()
        con.execute("""
          CREATE TABLE l1(ts_utc BIGINT, market_ticker VARCHAR,
            series_ticker VARCHAR, event_ticker VARCHAR, category VARCHAR,
            subcategory VARCHAR, yes_bid_e4 BIGINT, yes_ask_e4 BIGINT)
        """)
        base = 1783900800000000
        rows = []
        for minute in (0, 1):
            for ticker, bid, ask in (("MKT-A", 3300, 3400),
                                     ("MKT-B", 3300, 3400),
                                     ("MKT-C", 3400, 3500)):
                rows.append((base + minute * 60_000_000, ticker, "KXGAME",
                             "EVT-1", "Sports", "Soccer", bid, ask))
        con.executemany("INSERT INTO l1 VALUES (?,?,?,?,?,?,?,?)", rows)
        con.execute("COPY l1 TO ? (FORMAT PARQUET)", [str(l1_dir / "part.parquet")])
        con.execute("CREATE TABLE l2(ts_utc BIGINT, market_ticker VARCHAR, delta_e4 BIGINT)")
        con.executemany("INSERT INTO l2 VALUES (?,?,?)", [
            (base, "MKT-A", -50000), (base + 1000, "MKT-A", 30000)])
        con.execute("COPY l2 TO ? (FORMAT PARQUET)", [str(l2_dir / "part.parquet")])
        con.close()
        trade_fields = ["ts_utc", "market_ticker", "series_ticker", "event_ticker",
                        "category", "subcategory", "yes_price_e4", "count_e4",
                        "taker_side", "trade_id"]
        with gzip.open(tr_dir / "part.csv.gz", "wt", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=trade_fields)
            writer.writeheader()
            writer.writerow(dict(zip(trade_fields, [base, "MKT-A", "KXGAME", "EVT-1",
                                                     "Sports", "Soccer", 3400, 10000,
                                                     "yes", "T1"])))
            writer.writerow(dict(zip(trade_fields, [base + 1000, "MKT-A", "KXGAME", "EVT-1",
                                                     "Sports", "Soccer", 3400, 100000,
                                                     "no", "T2"])))
        def outer(wall, typ, msg):
            return json.dumps({"recv_wall_ns": wall,
                               "raw": json.dumps({"type": typ, "sid": 1, "msg": msg})})
        created = {"id": "R1", "creator_id": "", "market_ticker": "MKT-A",
                   "created_ts": "2026-07-13T00:00:00Z",
                   "contracts_fp": "10.00", "target_cost_dollars": "10.000000"}
        deleted = {"id": "R1", "creator_id": "H", "market_ticker": "MKT-A",
                   "deleted_ts": "2026-07-13T00:00:02Z"}
        with (rfq_dir / "rfq_00.ndjson").open("w") as handle:
            handle.write(outer(base * 1000, "rfq_created", created) + "\n")
            handle.write(outer(base * 1000 + 2_000_000_000, "rfq_deleted", deleted) + "\n")
        object_paths = [
            l1_dir / "part.parquet", tr_dir / "part.csv.gz",
            l2_dir / "part.parquet", rfq_dir / "rfq_00.ndjson",
        ]
        objects = []
        for index, path in enumerate(object_paths, 1):
            objects.append({
                "key": path.relative_to(release).as_posix(),
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "version_id": "VERSION-%d" % index,
            })
        manifest = {
            "schema_version": "research-release-manifest-v2", "release_id": rid,
            "date": "2026-07-13", "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
            "tl1_status": "TL1", "version_binding": {"mode": "VERSION_BOUND"},
            "publication_state_sha256": "1" * 64,
            "seal": {"sha256": "2" * 64}, "objects": objects,
        }
        (release / "MANIFEST.json").write_text(json.dumps(manifest))
        if verified:
            (release / ".VERIFIED.json").write_text(json.dumps({
                "release_id": rid, "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "date": "2026-07-13", "publication_state_sha256": "1" * 64,
                "seal_sha256": "2" * 64, "tl1_status": "TL1",
                "version_binding_mode": "VERSION_BOUND"}))
        return release

    def test_cycle_builds_honest_pilot_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp_text:
            tmp = Path(tmp_text)
            release = self._release(tmp)
            mission = tmp / "MISSION.md"
            mission.write_text("pilot mission\n")
            run_root = tmp / "runs"
            env = dict(os.environ, PYTHONPYCACHEPREFIX=str(tmp / "pycache"))
            result = subprocess.run([
                sys.executable, str(RUNNER), "--release-dir", str(release),
                "--mission", str(mission), "--run-root", str(run_root),
                "--run-id", "fixture-run", "--memory-limit", "1GB",
                "--threads", "1", "--rfq-max-rows", "100",
                "--allow-degraded",
            ], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            run = run_root / "fixture-run"
            self.assertTrue((run / "REPORT" / "index.html").is_file())
            manifest = json.loads((run / "RUN_MANIFEST.json").read_text())
            self.assertEqual(manifest["mode"], "EXPLORATORY_PATH_SMOKE")
            self.assertEqual(manifest["banners"]["authorization"],
                             "NOT A LIVE-TRADING AUTHORIZATION")
            ledger = json.loads((run / "HYPOTHESIS_LEDGER.json").read_text())
            self.assertEqual(len(ledger["hypotheses"]), 5)
            by_id = {h["study_id"]: h for h in ledger["hypotheses"]}
            over = by_id["THREE-WAY-OVERROUND"]["result"]
            self.assertEqual(over["status"], "DATA_STARVED")
            self.assertIn("mutually exclusive", over["reason"])
            rfq = by_id["RFQ-FLOW-CLOB"]["result"]
            profile = rfq["diagnostic_input_profile"]
            self.assertEqual(profile["rfq_created"], 1)
            self.assertEqual(profile["rfq_deleted"], 1)
            self.assertEqual(profile["create_to_delete_seconds"]["p50"], 2.0)
            report = (run / "REPORT" / "FULL_REPORT.md").read_text()
            self.assertIn("EXPLORATORY_ONLY", report)
            self.assertNotIn("VERDICT_PASS", report)
            self.assertNotIn("PROMOTION_READY", report)

    def test_unverified_release_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_text:
            tmp = Path(tmp_text)
            release = self._release(tmp, verified=False)
            mission = tmp / "MISSION.md"
            mission.write_text("pilot mission\n")
            result = subprocess.run([
                sys.executable, str(RUNNER), "--release-dir", str(release),
                "--mission", str(mission), "--run-root", str(tmp / "runs"),
                "--run-id", "blocked",
            ], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("not locally verified", result.stderr)
            self.assertFalse((tmp / "runs" / "blocked").exists())

    def test_manifest_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_text:
            tmp = Path(tmp_text)
            release = self._release(tmp)
            (release / "facts" / "trades" / "part.csv.gz").write_bytes(b"tampered")
            mission = tmp / "MISSION.md"
            mission.write_text("pilot mission\n")
            result = subprocess.run([
                sys.executable, str(RUNNER), "--release-dir", str(release),
                "--mission", str(mission), "--run-root", str(tmp / "runs"),
                "--run-id", "tampered", "--allow-degraded",
            ], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertRegex(result.stderr, "size changed|hash changed")

    def test_duplicate_date_releases_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp_text:
            tmp = Path(tmp_text)
            release = self._release(tmp)
            second = tmp / "second" / release.name
            second.parent.mkdir()
            import shutil
            shutil.copytree(release, second)
            manifest = json.loads((second / "MANIFEST.json").read_text())
            rid2 = "2026-07-13__seal-bbbbbbbb__pub-2222222222222222"
            manifest["release_id"] = rid2
            second2 = second.with_name(rid2)
            second.rename(second2)
            (second2 / "MANIFEST.json").write_text(json.dumps(manifest))
            verified = json.loads((second2 / ".VERIFIED.json").read_text())
            verified["release_id"] = rid2
            (second2 / ".VERIFIED.json").write_text(json.dumps(verified))
            mission = tmp / "MISSION.md"
            mission.write_text("pilot mission\n")
            result = subprocess.run([
                sys.executable, str(RUNNER), "--release-dir", str(release),
                "--release-dir", str(second2), "--mission", str(mission),
                "--run-root", str(tmp / "runs"), "--run-id", "dupe-date",
                "--allow-degraded",
            ], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("one active release per data date", result.stderr)


if __name__ == "__main__":
    unittest.main()
