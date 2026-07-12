#!/usr/bin/env python3
"""PIPE-W05 Phase A: immutable research release publisher (runs ON the EC2 box).

Publishes one SEALED day's research-ready set as an immutable release under
    <dest>/releases/<release_id>/            (dest default: the research/ S3 prefix)
with `MANIFEST.json` uploaded LAST — the manifest object is the exposure
marker research consumers key on, so a torn publish is never presented as a
finished release.

RELEASE IDENTITY (operator correction order 2026-07-12, fix 1):
    release_id = "<date>__seal-<seal sha256[:8]>__pub-<state digest[:16]>"
where the publication-state digest is sha256 over the canonical JSON of the
COMPLETE frozen publication state: the seal digest, the corrections partition
content (file hashes + day-filtered ledger entries), the day's capture-gap
evidence, the l2 seq-quality record, and the RFQ inclusion decision with the
exact seal-listed rfq file set. Any later correction, new gap evidence or a
flipped RFQ switch therefore produces a DISTINCT release id; published
releases are never mutated or deleted (D1). Idempotency is state-aware: the
publisher itself probes <release_id>/MANIFEST.json and no-ops when this exact
state is already published — there is no date-only done-file.

STAGING (fix 2): mutable auxiliary files (warehouse manifest, catalog, dim
snapshots, corrections, quality records) are COPIED into the staging area
first and hashed FROM THE COPY — a concurrent writer can never invalidate a
hash after it is frozen. Only seal-attested write-once files (facts archives,
sealed rfq raw) are hardlinked, and those are byte-verified against the day
seal before staging.

POST-UPLOAD VERIFICATION (fix 3): after the data upload and BEFORE the
manifest is written, every actual destination object is re-verified — size
via HeadObject and content sha256 via a streamed GetObject — and its
VersionId (null when the bucket is unversioned) is recorded per object in the
manifest, so the Mac fetch can request that exact version. Any mismatch or a
denied read aborts with no manifest (fail-closed).

EVIDENCE TIER (fix 5) is DERIVED, never assumed: SEALED_CONFIRMATION only for
a status=SEALED version-2 full_v2 seal with go_no_go_eligible=true, a present
capture-gap record and no explicit capture-quality failure; anything else
publishes as SEALED_DEGRADED_EVIDENCE with the downgrade reasons in the
manifest (evidence_tier_basis).

L2 (fix 4): orderbooks_full sealed facts are detected from the seal file list
and exposed as INCLUDED_SEALED_FACTS when present; otherwise the channel is
ABSENT_FROM_THIS_RELEASE for that day — no claims beyond this release.

RFQ (fix 6): rfq files are enumerated from the EXACT seal raw_files list
(including cross-day receipt-hour files living under the next day's raw
directory). A file already pruned from local disk is reconstructed from the
raw vault source (default s3://…/ec2/raw), byte-verified against the seal
sha256/size, with the source VersionId recorded. Inclusion is the operator
cost switch DEFAULTING OFF (+$21-23/month compounding, operator decision
pending): env RESEARCH_INCLUDE_RFQ=1 or flag file
~/.kalshi/research_include_rfq; `--include-rfq` / `--no-rfq` override one
run. Only rfq_<HH>/rfq_receipts_<HH> families are ever admitted (amendment
1); firehose/l2_<HH> raw never leaves the hourly vault path.

stdlib + duckdb (schema freeze; explicit memory_limit on every connection) +
the aws CLI for s3:// destinations. Local directory destinations/vaults use
pure python (fixture tests).
"""
import argparse
import csv
import datetime
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

MANIFEST_SCHEMA = "research-release-manifest-v2"
DEST_DEFAULT = "s3://kalshi-vault-ritcardo/research"
RAW_VAULT_DEFAULT = "s3://kalshi-vault-ritcardo/ec2/raw"
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
# Destination / vault backends
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

    def verify_object(self, key, size, sha256):
        """Post-upload verification of the ACTUAL destination object (fix 3).
        Returns the VersionId (None here — a filesystem has none)."""
        path = os.path.join(self.root, key)
        if not os.path.isfile(path):
            raise SystemExit("ABORT (fail-closed): uploaded object missing "
                             "at destination: %s" % key)
        if os.stat(path).st_size != size:
            raise SystemExit("ABORT (fail-closed): destination size mismatch "
                             "for %s" % key)
        if sha256_file(path) != sha256:
            raise SystemExit("ABORT (fail-closed): destination sha256 "
                             "mismatch for %s" % key)
        return None

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

    def _head(self, key):
        r = subprocess.run(["aws", "s3api", "head-object", "--bucket",
                            self.bucket, "--key", self._key(key)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(
                "ABORT (fail-closed): HeadObject denied/failed for %s — "
                "post-upload verification is mandatory (operator correction "
                "order fix 3). If this is a permission gap, vaultWriter "
                "needs read (HeadObject/GetObject) on the research/ prefix. "
                "stderr: %s" % (key, r.stderr.strip()[:300]))
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise SystemExit("ABORT (fail-closed): unparsable HeadObject "
                             "response for %s" % key)

    def _streamed_sha256(self, key):
        p = subprocess.Popen(["aws", "s3", "cp",
                              "%s/%s" % (self.url, key), "-",
                              "--no-progress"],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        h = hashlib.sha256()
        n = 0
        for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
        p.stdout.close()
        err = p.stderr.read().decode("utf-8", "replace")
        p.stderr.close()
        if p.wait() != 0:
            raise SystemExit("ABORT (fail-closed): GetObject stream failed "
                             "for %s — post-upload verification is "
                             "mandatory. stderr: %s" % (key, err[:300]))
        return h.hexdigest(), n

    def verify_object(self, key, size, sha256):
        """Fix 3: verify the ACTUAL S3 object (HeadObject size + streamed
        GetObject sha256) and return its VersionId (None if unversioned)."""
        head = self._head(key)
        if head.get("ContentLength") != size:
            raise SystemExit("ABORT (fail-closed): S3 object size %s != "
                             "frozen %d for %s"
                             % (head.get("ContentLength"), size, key))
        digest, n = self._streamed_sha256(key)
        if n != size or digest != sha256:
            raise SystemExit("ABORT (fail-closed): S3 object sha256/size "
                             "mismatch for %s" % key)
        return head.get("VersionId")

    def describe(self):
        return self.url


class LocalVault:
    """Directory standing in for the ec2/raw vault (fixture tests)."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def fetch(self, rel, dest):
        src = os.path.join(self.root, rel)
        if not os.path.isfile(src):
            return None
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        return {"source": "vault_reconstructed", "source_version_id": None}


class S3Vault:
    """The existing version-pinned raw vault (s3://…/ec2/raw), read-only."""

    def __init__(self, url):
        m = re.match(r"^s3://([^/]+)/?(.*)$", url)
        self.bucket = m.group(1)
        self.prefix = m.group(2).rstrip("/")
        self.url = url.rstrip("/")

    def fetch(self, rel, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        r = subprocess.run(["aws", "s3", "cp", "%s/%s" % (self.url, rel),
                            dest, "--no-progress"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        key = "%s/%s" % (self.prefix, rel) if self.prefix else rel
        h = subprocess.run(["aws", "s3api", "head-object", "--bucket",
                            self.bucket, "--key", key],
                           capture_output=True, text=True)
        vid = None
        if h.returncode == 0:
            try:
                vid = json.loads(h.stdout).get("VersionId")
            except ValueError:
                vid = None
        return {"source": "vault_reconstructed", "source_version_id": vid}


def make_dest(url):
    return S3Dest(url) if url.startswith("s3://") else LocalDest(url)


def make_vault(url):
    return S3Vault(url) if url.startswith("s3://") else LocalVault(url)


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


def day_ledger_lines(warehouse_root, date):
    """Corrections-ledger entries for this exchange date (raw lines)."""
    path = os.path.join(warehouse_root, "corrections", "ledger.ndjson")
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise SystemExit("ABORT (fail-closed): corrupt corrections "
                                 "ledger line")
            if row.get("exchange_date") == date or row.get("date") == date:
                out.append(line)
    return out


def rfq_switch_enabled():
    return (os.environ.get("RESEARCH_INCLUDE_RFQ") == "1"
            or os.path.isfile(RFQ_FLAG_FILE))


def derive_evidence_tier(seal, gap_record_present):
    """Fix 5: the tier is DERIVED from the seal + required evidence, never
    assumed. Returns (tier, basis_dict)."""
    reasons = []
    if seal.get("method") != "full_v2":
        reasons.append("seal method=%r (expected full_v2)"
                       % seal.get("method"))
    if seal.get("go_no_go_eligible") is not True:
        reasons.append("seal not go_no_go_eligible")
    if not gap_record_present:
        reasons.append("required capture-gap evidence absent for the date")
    cq = str(seal.get("capture_quality_status") or "")
    if cq and cq != "UNASSESSED_PENDING_PIPE_W03" and any(
            w in cq.upper() for w in ("FAIL", "BAD", "DEGRADED", "REJECT")):
        reasons.append("capture_quality_status=%s" % cq)
    tier = "SEALED_CONFIRMATION" if not reasons \
        else "SEALED_DEGRADED_EVIDENCE"
    basis = {
        "seal_status": seal.get("status"),
        "seal_version": seal.get("version"),
        "method": seal.get("method"),
        "go_no_go_eligible": seal.get("go_no_go_eligible"),
        "capture_quality_status": seal.get("capture_quality_status"),
        "gap_record_present": bool(gap_record_present),
        "downgrade_reasons": reasons,
    }
    return tier, basis


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


def code_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wc.ROOT,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "UNKNOWN"
    except OSError:
        return "UNKNOWN"


def canonical_digest(obj):
    return hashlib.sha256(json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")).hexdigest()


def publish(date, dest_url, include_rfq, quality_dir, raw_vault_url):
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

    # ---- RFQ decision from the EXACT seal file list (fix 6) --------------------
    rfq_entries = sorted(
        (r for r in seal.get("raw_files", [])
         if _RFQ_RE.match(os.path.basename(r.get("file", "")))),
        key=lambda r: r["file"])
    rfq_effective = bool(include_rfq and rfq_entries)

    stage_root = os.path.join(wc.ROOT, "work", "research_stage")
    pending = os.path.join(stage_root, ".pending-%d" % os.getpid())
    shutil.rmtree(pending, ignore_errors=True)
    os.makedirs(pending, exist_ok=True)
    # the stage base starts as the digest-pending dir and is atomically
    # renamed to the release-id dir once the publication state is known
    stage = {"dir": pending}
    objects = []  # {key, size, sha256[, version_id]} — frozen from STAGED bytes

    def stage_copy(key, src):
        """Fix 2: mutable auxiliary files are COPIED first and hashed from
        the staged copy — a live writer can never invalidate the freeze."""
        dst = os.path.join(stage["dir"], key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        objects.append({"key": key, "size": os.stat(dst).st_size,
                        "sha256": sha256_file(dst)})
        return objects[-1]

    def stage_link_attested(key, src, frozen):
        """Seal-attested write-once files (facts, sealed rfq raw): verified
        against the seal, then hardlinked (copy fallback)."""
        dst = os.path.join(stage["dir"], key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        objects.append({"key": key, "size": frozen[1], "sha256": frozen[0]})
        return objects[-1]

    def stage_bytes(key, payload):
        dst = os.path.join(stage["dir"], key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(payload)
        objects.append({"key": key, "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()})
        return objects[-1]

    try:
        # ---- publication-state components (corrections/gaps/rfq) — staged
        # copies first, digest computed FROM the frozen copies (fixes 1+2) ----
        corr_dir = os.path.join(warehouse_root, "corrections",
                                "date=%s" % date)
        corr_objs = []
        if os.path.isdir(corr_dir):
            for base, _d, files in os.walk(corr_dir):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    key = "corrections/date=%s/%s" % (
                        date, os.path.relpath(src, corr_dir))
                    corr_objs.append(stage_copy(key, src))
        ledger_lines = day_ledger_lines(warehouse_root, date)
        ledger_obj = None
        if ledger_lines:
            ledger_obj = stage_bytes(
                "corrections/ledger_day.ndjson",
                ("\n".join(ledger_lines) + "\n").encode("utf-8"))

        gaps = day_gap_intervals(
            os.path.join(quality_dir, "capture_gaps.csv"), date)
        gap_obj = None
        if gaps is not None:
            buf = ["start_us,end_us"]
            buf += ["%d,%d" % (g["start_us"], g["end_us"]) for g in gaps]
            gap_obj = stage_bytes("quality/capture_gaps_%s.csv" % date,
                                  ("\n".join(buf) + "\n").encode("utf-8"))
        l2_gaps_src = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
        l2_gap_obj = None
        l2_quality = None
        if os.path.isfile(l2_gaps_src):
            l2_gap_obj = stage_copy("quality/l2_gaps.json", l2_gaps_src)
            with open(os.path.join(pending, "quality/l2_gaps.json")) as f:
                lg = json.load(f)
            l2_quality = {k: lg.get(k) for k in
                          ("no_l2_files", "seq_gap_events",
                           "seq_missed_total", "sids_total",
                           "sids_with_seq_gaps", "lines")}

        publication_state = {
            "seal_sha256": seal_sha,
            "corrections": {
                "files": [{"key": o["key"], "size": o["size"],
                           "sha256": o["sha256"]} for o in corr_objs],
                "ledger_day_sha256":
                    ledger_obj["sha256"] if ledger_obj else None,
            },
            "gap_evidence": {
                "capture_gaps_sha256":
                    gap_obj["sha256"] if gap_obj else None,
                "l2_gaps_sha256":
                    l2_gap_obj["sha256"] if l2_gap_obj else None,
            },
            "rfq": {
                "included": rfq_effective,
                "files": [{"file": r["file"], "size": r["size"],
                           "sha256": r["sha256"]} for r in rfq_entries]
                if rfq_effective else [],
            },
        }
        state_digest = canonical_digest(publication_state)
        release_id = "%s__seal-%s__pub-%s" % (date, seal_sha[:8],
                                              state_digest[:16])
        rel_prefix = "releases/%s" % release_id

        dest = make_dest(dest_url)
        if dest.exists("%s/MANIFEST.json" % rel_prefix):
            print("[research_release] %s publication state %s already "
                  "published at %s/%s — no-op"
                  % (date, state_digest[:16], dest.describe(), rel_prefix))
            return 0

        stage_dir = os.path.join(stage_root, release_id)
        shutil.rmtree(stage_dir, ignore_errors=True)
        os.replace(pending, stage_dir)
        stage["dir"] = stage_dir
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise

    try:
        # ---- manifest.csv date rows must still match the seal (copy first,
        # fix 2: drift check runs on the frozen staged copy) --------------------
        wm_obj = stage_copy("warehouse_manifest/manifest.csv",
                            os.path.join(warehouse_root, "manifest.csv"))
        staged_wm = os.path.join(stage_dir, "warehouse_manifest",
                                 "manifest.csv")
        manifest_sha, _rows = wc.manifest_date_sha256(staged_wm, date)
        if manifest_sha != seal.get("manifest_date_sha256"):
            raise SystemExit("ABORT (fail-closed): manifest_date_sha256 "
                             "drift for %s (seal %s != staged copy %s)"
                             % (date, seal.get("manifest_date_sha256"),
                                manifest_sha))

        # ---- facts: exact sealed set, re-verified byte-for-byte ---------------
        table_sample = {}
        for entry in seal["archive_file_stats"]:
            src = os.path.join(archive_root, entry["file"])
            if not os.path.isfile(src):
                raise SystemExit("ABORT (fail-closed): sealed archive file "
                                 "missing locally: %s" % src)
            frozen = verify_against(entry, src, "facts")
            stage_link_attested("facts/%s" % entry["file"], src, frozen)
            table_sample.setdefault(entry["table"],
                                    os.path.join(stage_dir, "facts",
                                                 entry["file"]))

        # ---- schema freeze (duckdb on the STAGED copies) -----------------------
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

        # ---- seal (from the exact bytes the digest froze) ----------------------
        stage_bytes("seal/date=%s.json" % date, seal_bytes)

        # ---- dim snapshot / catalog (mutable aux: copy first, fix 2) ----------
        dim_snap = os.path.join(warehouse_root, "dim", "snapshots",
                                "date=%s" % date)
        if os.path.isdir(dim_snap):
            for base, _d, files in os.walk(dim_snap):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    stage_copy("dim/snapshots/date=%s/%s"
                               % (date, os.path.relpath(src, dim_snap)), src)
        catalog_dir = os.path.join(warehouse_root, "catalog")
        if os.path.isdir(catalog_dir):
            for base, _d, files in os.walk(catalog_dir):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    stage_copy("catalog/%s"
                               % os.path.relpath(src, catalog_dir), src)

        # ---- sealed RFQ raw: exact seal list, cross-day included,
        # vault reconstruction after pruning (fix 6) -----------------------------
        rfq_files_meta = []
        if rfq_effective:
            vault = make_vault(raw_vault_url)
            for entry in rfq_entries:
                rel = entry["file"]
                key = "raw_rfq/%s" % rel
                local = os.path.join(raw_root, rel)
                if os.path.isfile(local):
                    frozen = verify_against(entry, local, "rfq raw")
                    stage_link_attested(key, local, frozen)
                    rfq_files_meta.append(
                        {"file": rel, "size": entry["size"],
                         "sha256": entry["sha256"], "source": "local",
                         "source_version_id": None})
                else:
                    dst = os.path.join(stage_dir, key)
                    prov = vault.fetch(rel, dst)
                    if prov is None:
                        raise SystemExit(
                            "ABORT (fail-closed): sealed rfq file %s is "
                            "pruned locally AND unavailable from the raw "
                            "vault %s" % (rel, raw_vault_url))
                    frozen = verify_against(entry, dst, "rfq raw (vault)")
                    objects.append({"key": key, "size": frozen[1],
                                    "sha256": frozen[0]})
                    rfq_files_meta.append(
                        {"file": rel, "size": entry["size"],
                         "sha256": entry["sha256"], **prov})

        rfq_channel = {
            "status": ("INCLUDED_SEALED_RAW" if rfq_effective
                       else "NO_RFQ_CAPTURE_THIS_DAY" if not rfq_entries
                       else "EXCLUDED_PENDING_OPERATOR_COST_ACK"),
            "note": "sealed rfq_<HH> + rfq_receipts_<HH> raw are the "
                    "research-ready RFQ form for this release; enumeration "
                    "is the exact day-seal raw_files list incl. cross-day "
                    "receipt hours. Inclusion costs ~$21-23/month "
                    "compounding and is an operator decision. Switch: "
                    "RESEARCH_INCLUDE_RFQ=1 or flag file %s on the EC2 box."
                    % RFQ_FLAG_FILE,
            "sealed_rfq_files_in_day_seal": len(rfq_entries),
        }
        if rfq_effective:
            rfq_channel["files"] = rfq_files_meta

        # ---- evidence tier: DERIVED (fix 5) ------------------------------------
        tier, tier_basis = derive_evidence_tier(seal, gaps is not None)

        # ---- channel completeness labels (amendment 4 + fix 4) -----------------
        l2_included = "orderbooks_full" in tables
        channels = {
            "orderbooks_l1": {
                "status": ("INCLUDED" if "orderbooks_l1" in tables
                           else "ABSENT_FROM_THIS_RELEASE"),
                "completeness": "CONFLATED_CHANGE_STREAM_NEVER_LOSSLESS",
                "gap_evidence": ("quality/capture_gaps_%s.csv" % date
                                 if gaps is not None
                                 else "ABSENT_NO_CAPTURE_GAP_RECORD"),
                "gap_intervals_for_date":
                    len(gaps) if gaps is not None else None,
            },
            "trades": {
                "status": ("INCLUDED" if "trades" in tables
                           else "ABSENT_FROM_THIS_RELEASE"),
                "identity": "trade_id (consumers must collapse duplicate "
                            "ids; conflicting bodies excluded downstream)",
            },
            "orderbooks_l2": {
                "status": ("INCLUDED_SEALED_FACTS" if l2_included
                           else "ABSENT_FROM_THIS_RELEASE"),
                "note": ("orderbooks_full sealed facts present in this "
                         "day's archive (snapshot+delta; subscribed markets "
                         "only)" if l2_included else
                         "no orderbooks_full facts in this day's sealed "
                         "archive; the per-day L2 seq-quality record is "
                         "carried when present (quality/l2_gaps.json)"),
                "seq_quality": l2_quality,
            },
            "rfq": rfq_channel,
        }

        # ---- upload: data first ------------------------------------------------
        dest.upload_tree(stage_dir, rel_prefix)

        # ---- fix 3: verify EVERY actual destination object (size + streamed
        # sha256) and record its VersionId BEFORE the manifest exists ------------
        any_version = False
        for o in objects:
            vid = dest.verify_object("%s/%s" % (rel_prefix, o["key"]),
                                     o["size"], o["sha256"])
            o["version_id"] = vid
            any_version = any_version or vid is not None

        now = datetime.datetime.now(datetime.timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "release_id": release_id,
            "date": date,
            "generated_at_utc": now,
            "code_commit": code_commit(),
            "evidence_tier": tier,
            "evidence_tier_basis": tier_basis,
            "publication_state": publication_state,
            "publication_state_sha256": state_digest,
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
                "included_files": len(corr_objs),
                "ledger_day_entries": len(ledger_lines),
                "cutoff_utc": now,
                "note": "sealed archives are write-once; post-seal late "
                        "rows land in corrections/ and any change to that "
                        "state publishes a DISTINCT release id (the "
                        "publication-state digest covers corrections, gap "
                        "evidence and rfq inclusion)",
            },
            "post_upload_verification": {
                "method": "HeadObject size + streamed GetObject sha256 per "
                          "object, before the manifest was written",
                "objects_verified": len(objects),
            },
            "s3_versioning": {
                "bucket_versioning": ("VERSIONED" if any_version
                                      else "UNVERSIONED_OR_UNKNOWN"),
                "caveat": "per-object version_id is null when the bucket "
                          "returned none; the authoritative freeze is byte "
                          "size + sha256 per object (this manifest); "
                          "consumers fetch the recorded version_id when "
                          "present",
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
              "tier=%s, tl1=%s, l2=%s, rfq=%s, verified=%d -> %s/%s"
              % (release_id, len(objects), total / 1e6, tier, tl1_status,
                 channels["orderbooks_l2"]["status"],
                 channels["rfq"]["status"], len(objects), dest.describe(),
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
    p.add_argument("--raw-vault",
                   default=os.environ.get("RESEARCH_RAW_VAULT",
                                          RAW_VAULT_DEFAULT),
                   help="raw vault source for rfq reconstruction after "
                        "local pruning (fix 6)")
    args = ap.parse_args(argv[1:])
    include_rfq = (rfq_switch_enabled() if args.include_rfq is None
                   else args.include_rfq)
    return publish(args.date, args.dest, include_rfq, args.quality_dir,
                   args.raw_vault)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
