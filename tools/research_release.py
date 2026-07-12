#!/usr/bin/env python3
"""PIPE-W05 Phase A: immutable research release publisher (runs ON the EC2 box).

Publishes one SEALED day's research-ready set as an immutable release under
    <dest>/releases/<release_id>/            (dest default: the research/ S3 prefix)
with `MANIFEST.json` uploaded LAST — the manifest object is the exposure
marker research consumers key on, so a torn publish is never presented as a
finished release.

release_id = "<date>__seal-<sha256(seal file bytes)[:12]>" (suffix "-rfq"
when sealed rfq raw is included): a rebuilt (operator-invalidated) seal
produces a NEW release id; published releases are never mutated or deleted
(D1). Because releases are immutable, flipping the RFQ switch ON later never
mutates an existing release — future days publish with rfq automatically,
and a past day gains rfq by explicitly re-running
`publish --date D --include-rfq`, which creates the sibling "-rfq" release.

Frozen in MANIFEST.json: every object key + byte size + sha256, the seal
digest + manifest_date_sha256, per-table schema (duckdb DESCRIBE) with
TL1-ladder / ws_sid / ws_seq presence, channel completeness labels
(W05 addendum amendment 4), correction cutoff, capture-gap evidence for the
date, evidence tier (SEALED_CONFIRMATION — Phase A only), and the S3
versioning caveat (bucket versioning is UNKNOWN to vaultWriter: the freeze is
size+sha256; VersionIds are recorded when the bucket returns them).

Fail-closed: every staged file is re-verified against the day seal BEFORE
anything is uploaded; any mismatch aborts with nothing published. Publishing
an already-published release id is an idempotent no-op.

Sealed RFQ raw (cross-validation note 1, option a): rfq_<HH>.ndjson* +
rfq_receipts_<HH>.ndjson* — the only raw families admitted to research/
(amendment 1) — are included ONLY when the operator cost switch is on
(+$21-23/month compounding, operator decision pending):
    env RESEARCH_INCLUDE_RFQ=1   or   flag file ~/.kalshi/research_include_rfq
`--include-rfq` / `--no-rfq` override the switch for one run. Default OFF.
Generic raw (firehose, l2_<HH>) is NEVER published here; L2 is
NOT RESEARCH-EXPOSABLE in Phase A (no L2 facts extraction exists yet).

stdlib + duckdb (schema freeze; explicit memory_limit on every connection) +
the aws CLI for s3:// destinations. A local directory destination uses pure
python (fixture tests).
"""
import argparse
import csv
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

MANIFEST_SCHEMA = "research-release-manifest-v1"
EVIDENCE_TIER = "SEALED_CONFIRMATION"  # Phase A: the only tier
DEST_DEFAULT = "s3://kalshi-vault-ritcardo/research"
DUCKDB_MEMORY_LIMIT = "8GB"  # spec HYGIENE: explicit on every connection
LADDER_COLUMNS = ("exchange_ts_us", "recv_wall_ns", "recv_mono_ns",
                  "local_recv_ts_us")
RFQ_FLAG_FILE = os.path.expanduser("~/.kalshi/research_include_rfq")
_RFQ_RE = re.compile(r"^rfq(?:_receipts)?_\d{2}\.ndjson(?:\.\d+)?$")

DAY_US = 86_400_000_000


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def duckdb_connect():
    import duckdb
    conn = duckdb.connect()
    conn.execute("SET memory_limit='%s'" % DUCKDB_MEMORY_LIMIT)
    return conn


def describe_columns(conn, path):
    if path.endswith(".parquet"):
        q = "DESCRIBE SELECT * FROM read_parquet(?)"
    else:
        q = "DESCRIBE SELECT * FROM read_csv_auto(?)"
    return [r[0] for r in conn.execute(q, [path]).fetchall()]


# --------------------------------------------------------------------------
# Destination backends
# --------------------------------------------------------------------------

class LocalDest:
    """Directory destination (fixture tests). Pure python."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def exists(self, key):
        return os.path.isfile(os.path.join(self.root, key))

    def upload_tree(self, stage_dir, prefix):
        """Everything except MANIFEST.json."""
        for base, _dirs, files in os.walk(stage_dir):
            for fn in files:
                if os.path.join(base, fn) == os.path.join(stage_dir,
                                                          "MANIFEST.json"):
                    continue
                src = os.path.join(base, fn)
                rel = os.path.relpath(src, stage_dir)
                dst = os.path.join(self.root, prefix, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)

    def upload_file(self, local, key):
        dst = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(local, dst)

    def head_version(self, key):
        return None  # VersionId is an S3 concept

    def describe(self):
        return self.root


class S3Dest:
    """s3://bucket/prefix destination via the aws CLI (on-box creds)."""

    def __init__(self, url):
        m = re.match(r"^s3://([^/]+)/?(.*)$", url)
        if not m:
            raise SystemExit("bad s3 destination: %s" % url)
        self.bucket = m.group(1)
        self.prefix = m.group(2).rstrip("/")
        self.url = "s3://%s/%s" % (self.bucket, self.prefix) if self.prefix \
            else "s3://%s" % self.bucket

    def _key(self, key):
        return "%s/%s" % (self.prefix, key) if self.prefix else key

    def exists(self, key):
        r = subprocess.run(["aws", "s3", "ls", "%s/%s" % (self.url, key)],
                           capture_output=True, text=True)
        return r.returncode == 0 and key.rsplit("/", 1)[-1] in r.stdout

    def upload_tree(self, stage_dir, prefix):
        subprocess.run(["aws", "s3", "sync", stage_dir,
                        "%s/%s" % (self.url, prefix),
                        "--no-progress", "--exclude", "MANIFEST.json"],
                       check=True)

    def upload_file(self, local, key):
        subprocess.run(["aws", "s3", "cp", local, "%s/%s" % (self.url, key),
                        "--no-progress"], check=True)

    def head_version(self, key):
        """VersionId if the bucket is versioned AND the writer may head-object;
        None otherwise (tolerated — the manifest carries the caveat)."""
        r = subprocess.run(["aws", "s3api", "head-object", "--bucket",
                            self.bucket, "--key", self._key(key)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout).get("VersionId")
        except ValueError:
            return None

    def describe(self):
        return self.url


def make_dest(url):
    return S3Dest(url) if url.startswith("s3://") else LocalDest(url)


# --------------------------------------------------------------------------
# Evidence assembly
# --------------------------------------------------------------------------

def day_gap_intervals(quality_csv, date):
    """Capture-gap intervals (µs epochs) overlapping the UTC date."""
    if not os.path.isfile(quality_csv):
        return None  # record absent — carried as such, never fabricated
    start, end = wc.day_start_us(date), wc.day_start_us(date) + DAY_US
    out = []
    with open(quality_csv, newline="") as f:
        for row in csv.DictReader(f):
            try:
                s, e = int(row["start_us"]), int(row["end_us"])
            except (KeyError, ValueError):
                continue
            if s < end and e > start:
                out.append({"start_us": s, "end_us": e})
    return out


def rfq_switch_enabled():
    return (os.environ.get("RESEARCH_INCLUDE_RFQ") == "1"
            or os.path.isfile(RFQ_FLAG_FILE))


def verify_against(stats, path, label):
    st = os.stat(path)
    if st.st_size != stats["size"]:
        raise SystemExit("ABORT (fail-closed): %s size %d != sealed %d for %s"
                         % (label, st.st_size, stats["size"], path))
    digest = sha256_file(path)
    if digest != stats["sha256"]:
        raise SystemExit("ABORT (fail-closed): %s sha256 mismatch for %s"
                         % (label, path))
    return digest, st.st_size


def stage_file(stage_dir, key, src, hardlink=True):
    dst = os.path.join(stage_dir, key)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if hardlink:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copyfile(src, dst)


def code_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wc.ROOT,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "UNKNOWN"
    except OSError:
        return "UNKNOWN"


def publish(date, dest_url, include_rfq, quality_dir, live_dir):
    cfg = wc.load_config()
    warehouse_root = cfg["warehouse_root"]
    archive_root = cfg["archive_root"]
    raw_root = cfg["raw_root"]

    # ---- gate: SEALED v2 only ------------------------------------------------
    if not wc.day_sealed(warehouse_root, date):
        raise SystemExit("REFUSED: %s is not a SEALED v2 day (seal-gated)"
                         % date)
    seal_file = wc.seal_path(warehouse_root, date)
    with open(seal_file, "rb") as f:
        seal_bytes = f.read()
    seal = json.loads(seal_bytes)
    seal_sha = hashlib.sha256(seal_bytes).hexdigest()
    has_sealed_rfq = any("/rfq" in r.get("file", "")
                         for r in seal.get("raw_files", []))
    rfq_effective = bool(include_rfq and has_sealed_rfq)
    release_id = "%s__seal-%s%s" % (date, seal_sha[:12],
                                    "-rfq" if rfq_effective else "")
    rel_prefix = "releases/%s" % release_id

    dest = make_dest(dest_url)
    if dest.exists("%s/MANIFEST.json" % rel_prefix):
        print("[research_release] %s already published at %s/%s — no-op"
              % (date, dest.describe(), rel_prefix))
        return 0

    # ---- manifest.csv date rows must still match the seal ---------------------
    warehouse_manifest = os.path.join(warehouse_root, "manifest.csv")
    manifest_sha, _rows = wc.manifest_date_sha256(warehouse_manifest, date)
    if manifest_sha != seal.get("manifest_date_sha256"):
        raise SystemExit("ABORT (fail-closed): manifest_date_sha256 drift for "
                         "%s (seal %s != current %s)"
                         % (date, seal.get("manifest_date_sha256"),
                            manifest_sha))

    stage_root = os.path.join(wc.ROOT, "work", "research_stage")
    stage_dir = os.path.join(stage_root, release_id)
    shutil.rmtree(stage_dir, ignore_errors=True)
    os.makedirs(stage_dir, exist_ok=True)
    objects = []  # {key, size, sha256}

    def add(key, src, frozen=None):
        digest, size = frozen if frozen else (sha256_file(src),
                                              os.stat(src).st_size)
        stage_file(stage_dir, key, src)
        objects.append({"key": key, "size": size, "sha256": digest})

    try:
        # ---- facts: exact sealed set, re-verified byte-for-byte ---------------
        table_sample = {}
        for entry in seal["archive_file_stats"]:
            src = os.path.join(archive_root, entry["file"])
            if not os.path.isfile(src):
                raise SystemExit("ABORT (fail-closed): sealed archive file "
                                 "missing locally: %s" % src)
            frozen = verify_against(entry, src, "facts")
            add("facts/%s" % entry["file"], src, frozen)
            table_sample.setdefault(entry["table"], src)

        # ---- schema freeze (duckdb, explicit memory_limit) --------------------
        conn = duckdb_connect()
        tables = {}
        for table, sample in sorted(table_sample.items()):
            cols = describe_columns(conn, sample)
            tables[table] = {
                "columns": cols,
                "sampled_file": os.path.basename(sample),
                "tl1_ladder_columns_present":
                    all(c in cols for c in LADDER_COLUMNS),
                "ws_sid_present": "ws_sid" in cols,
                "ws_seq_present": "ws_seq" in cols,
            }
        conn.close()
        tl1_tables = [t for t, v in tables.items()
                      if v["tl1_ladder_columns_present"]]
        tl1_status = ("TL1" if len(tl1_tables) == len(tables) and tables
                      else "PRE-TL1" if not tl1_tables else "MIXED")

        # ---- seal + warehouse manifest ----------------------------------------
        add("seal/date=%s.json" % date, seal_file,
            (seal_sha, len(seal_bytes)))
        add("warehouse_manifest/manifest.csv", warehouse_manifest)

        # ---- dim snapshot / catalog (state at freeze) --------------------------
        dim_snap = os.path.join(warehouse_root, "dim", "snapshots",
                                "date=%s" % date)
        dim_included = os.path.isdir(dim_snap)
        if dim_included:
            for base, _d, files in os.walk(dim_snap):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    add("dim/snapshots/date=%s/%s"
                        % (date, os.path.relpath(src, dim_snap)), src)
        catalog_dir = os.path.join(warehouse_root, "catalog")
        catalog_included = os.path.isdir(catalog_dir)
        if catalog_included:
            for base, _d, files in os.walk(catalog_dir):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    add("catalog/%s" % os.path.relpath(src, catalog_dir), src)

        # ---- corrections (late rows land here; seals are write-once) ----------
        corr_dir = os.path.join(warehouse_root, "corrections", "date=%s" % date)
        corr_files = 0
        if os.path.isdir(corr_dir):
            for base, _d, files in os.walk(corr_dir):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    add("corrections/date=%s/%s"
                        % (date, os.path.relpath(src, corr_dir)), src)
                    corr_files += 1
        corr_ledger = os.path.join(warehouse_root, "corrections",
                                   "ledger.ndjson")
        if os.path.isfile(corr_ledger):
            add("corrections/ledger.ndjson", corr_ledger)

        # ---- capture-gap evidence (carried alongside, never fabricated) -------
        gaps = day_gap_intervals(
            os.path.join(quality_dir, "capture_gaps.csv"), date)
        if gaps is not None:
            gp = os.path.join(stage_dir, "quality", "capture_gaps_%s.csv"
                              % date)
            os.makedirs(os.path.dirname(gp), exist_ok=True)
            with open(gp, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["start_us", "end_us"])
                for g in gaps:
                    w.writerow([g["start_us"], g["end_us"]])
            objects.append({"key": "quality/capture_gaps_%s.csv" % date,
                            "size": os.stat(gp).st_size,
                            "sha256": sha256_file(gp)})
        l2_gaps_src = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
        l2_quality = None
        if os.path.isfile(l2_gaps_src):
            add("quality/l2_gaps.json", l2_gaps_src)
            with open(l2_gaps_src) as f:
                lg = json.load(f)
            l2_quality = {k: lg.get(k) for k in
                          ("no_l2_files", "seq_gap_events", "seq_missed_total",
                           "sids_total", "sids_with_seq_gaps", "lines")}

        # ---- sealed RFQ raw (operator cost switch, amendment 1 scope only) ----
        raw_by_rel = {r["file"]: r for r in seal.get("raw_files", [])}
        day_dir = wc.raw_day_dir(raw_root, date)
        rfq_local = sorted(
            p for p in glob.glob(os.path.join(day_dir, "rfq*"))
            if _RFQ_RE.match(os.path.basename(p)))
        rfq_channel = {
            "status": "EXCLUDED_PENDING_OPERATOR_COST_ACK",
            "note": "sealed rfq_<HH> + rfq_receipts_<HH> raw are the "
                    "research-ready RFQ form (no rfq facts extraction in "
                    "Phase A); inclusion costs ~$21-23/month compounding and "
                    "is an operator decision. Switch: RESEARCH_INCLUDE_RFQ=1 "
                    "or flag file %s on the EC2 box." % RFQ_FLAG_FILE,
            "sealed_rfq_files_in_day_seal": sum(
                1 for k in raw_by_rel if "/rfq" in k or k.startswith("rfq")),
        }
        if rfq_effective:
            if not rfq_local and rfq_channel["sealed_rfq_files_in_day_seal"]:
                raise SystemExit("ABORT (fail-closed): rfq inclusion is ON "
                                 "but the sealed rfq raw is no longer on "
                                 "local disk (pruned?) for %s" % date)
            for src in rfq_local:
                rel = "date=%s/%s" % (date, os.path.basename(src))
                stats = raw_by_rel.get(rel)
                if stats is None:
                    raise SystemExit("ABORT (fail-closed): rfq file not "
                                     "listed in the day seal: %s" % rel)
                frozen = verify_against(stats, src, "rfq raw")
                add("raw_rfq/%s" % os.path.basename(src), src, frozen)
            rfq_channel["status"] = ("INCLUDED_SEALED_RAW" if rfq_local
                                     else "NO_RFQ_CAPTURE_THIS_DAY")
            rfq_channel["files_included"] = len(rfq_local)
        elif not rfq_channel["sealed_rfq_files_in_day_seal"]:
            rfq_channel["status"] = "NO_RFQ_CAPTURE_THIS_DAY"

        # ---- channel completeness labels (amendment 4) -------------------------
        channels = {
            "orderbooks_l1": {
                "status": ("INCLUDED" if "orderbooks_l1" in tables
                           else "ABSENT"),
                "completeness": "CONFLATED_CHANGE_STREAM_NEVER_LOSSLESS",
                "gap_evidence": ("quality/capture_gaps_%s.csv" % date
                                 if gaps is not None
                                 else "ABSENT_NO_CAPTURE_GAP_RECORD"),
                "gap_intervals_for_date":
                    len(gaps) if gaps is not None else None,
            },
            "trades": {
                "status": "INCLUDED" if "trades" in tables else "ABSENT",
                "identity": "trade_id (consumers must collapse duplicate ids; "
                            "conflicting bodies excluded downstream)",
            },
            "orderbooks_l2": {
                "status": "NOT_RESEARCH_EXPOSABLE_PHASE_A",
                "reason": "no L2 facts extraction exists in the warehouse; "
                          "generic raw (incl. l2_<HH>) is excluded from "
                          "research/ by W05 addendum amendment 1. The per-day "
                          "L2 seq-quality record is carried when present "
                          "(quality/l2_gaps.json).",
                "seq_quality": l2_quality,
            },
            "rfq": rfq_channel,
        }

        # ---- upload: data first, MANIFEST.json LAST ----------------------------
        dest.upload_tree(stage_dir, rel_prefix)

        version_ids = None
        probe = dest.head_version("%s/seal/date=%s.json" % (rel_prefix, date))
        if probe:
            version_ids = {}
            for o in objects:
                version_ids[o["key"]] = dest.head_version(
                    "%s/%s" % (rel_prefix, o["key"]))

        now = datetime.datetime.now(datetime.timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "release_id": release_id,
            "date": date,
            "generated_at_utc": now,
            "code_commit": code_commit(),
            "evidence_tier": EVIDENCE_TIER,
            "seal": {
                "sha256": seal_sha,
                "manifest_date_sha256": seal["manifest_date_sha256"],
                "sealed_at": seal.get("sealed_at"),
                "status": seal.get("status"),
                "archive_files": seal.get("archive_files"),
                "archive_rows": seal.get("archive_rows"),
                "capture_quality_status":
                    seal.get("capture_quality_status"),
            },
            "tables": tables,
            "tl1_status": tl1_status,
            "channels": channels,
            "corrections": {
                "included_files": corr_files,
                "cutoff_utc": now,
                "note": "sealed archives are write-once; post-seal late rows "
                        "land in corrections/ and a corrected day would ship "
                        "as a NEW release id — this release freezes the "
                        "corrections state as of cutoff_utc",
            },
            "s3_versioning": {
                "bucket_versioning": ("VERSIONED" if version_ids
                                      else "UNKNOWN_TO_WRITER"),
                "version_ids": version_ids,
                "caveat": "bucket versioning status is unknown to the "
                          "publishing credential; the authoritative freeze "
                          "is byte size + sha256 per object (this manifest); "
                          "VersionIds are recorded when the bucket returns "
                          "them",
            },
            "objects": objects,
        }
        mpath = os.path.join(stage_dir, "MANIFEST.json")
        with open(mpath, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        dest.upload_file(mpath, "%s/MANIFEST.json" % rel_prefix)
        total = sum(o["size"] for o in objects)
        print("[research_release] published %s: %d objects, %.2f MB, "
              "tier=%s, tl1=%s, rfq=%s -> %s/%s"
              % (release_id, len(objects), total / 1e6, EVIDENCE_TIER,
                 tl1_status, channels["rfq"]["status"], dest.describe(),
                 rel_prefix))
        return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish", help="publish one sealed day")
    p.add_argument("--date", required=True)
    p.add_argument("--dest", default=os.environ.get("RESEARCH_DEST",
                                                    DEST_DEFAULT))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--include-rfq", action="store_true", default=None,
                   help="force sealed-rfq inclusion for this run")
    g.add_argument("--no-rfq", dest="include_rfq", action="store_false",
                   help="force exclusion for this run")
    p.add_argument("--quality-dir",
                   default=os.path.join(wc.ROOT, "work", "event_packs"))
    p.add_argument("--live-dir",
                   default=os.path.join(wc.ROOT, "work", "live"))
    args = ap.parse_args(argv[1:])
    include_rfq = (rfq_switch_enabled() if args.include_rfq is None
                   else args.include_rfq)
    return publish(args.date, args.dest, include_rfq, args.quality_dir,
                   args.live_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
