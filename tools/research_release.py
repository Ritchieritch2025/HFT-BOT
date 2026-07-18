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

POST-UPLOAD VERIFICATION + VERSION BINDING (fix 3 + P0-1/P0-2): after the
data upload and BEFORE the manifest is written, every actual destination
object is verified by its EXACT version: HeadObject -> VersionId A ->
GetObject --version-id A -> size + sha256 of those exact bytes -> record A.
Real S3 publication is version-bound OR IT DOES NOT HAPPEN: a missing
VersionId aborts fail-closed (VERSIONING_REQUIRED — P0-2: vaultWriter is
denied s3:GetBucketVersioning, so the bucket's versioning status is
unobservable and object-level VersionIds are the only acceptable proof;
publication stays stopped pending the operator's console confirmation).
There is NO degraded real-S3 mode. The MANIFEST itself is a write-once
conditional create (If-None-Match: *), read back by its exact VersionId and
sha256-verified. Only the offline local-directory FIXTURE destination is
versionless, and it is labeled LOCAL_FIXTURE_DEST_NO_VERSIONS — never a
real publication.

OPERATOR GATE (remediation item 7): publishing to a REAL s3:// destination
REFUSES to run without --operator-approved (depth_probe pattern; exit 2,
loud). A local directory destination is offline fixture mode and is exempt.
CREDENTIAL MODES (remediation item 6): this publisher runs on the EC2 box
with vaultWriter; it REFUSES an s3:// publish on any host holding the Mac
research read-only key file (~/.kalshi/research_s3.env.sh) — the read-only
namespace and the write namespace never share a host role.

GAP RECEIPTS (P0-3): absence of a gap file is NOT evidence, and neither is a
done-marker or a bare CSV. The ONLY affirmative gap evidence is the
scanner's own per-date receipt (capture_gap_receipt_<date>.json, written by
tools/capture_gaps.py) binding date + the EXACT scanned raw inventory
(files + bytes) + result — and that inventory must reproduce the day seal's
firehose raw inventory byte-for-byte. When orderbooks_full facts exist,
matching per-date L2 seq-quality evidence is MANDATORY.

EVIDENCE TIER (fix 5 + P0-3) is DERIVED, never assumed: SEALED_CONFIRMATION
only for a status=SEALED version-2 full_v2 seal with go_no_go_eligible=true,
an affirmative inventory-matched scan receipt, mandatory L2 evidence when L2
facts exist, and an EXPLICITLY ASSESSED capture quality —
UNASSESSED_PENDING_PIPE_W03 can NEVER earn SEALED_CONFIRMATION. Anything
else publishes as SEALED_DEGRADED_EVIDENCE with the downgrade reasons in
the manifest (evidence_tier_basis).

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

REFERENCE V3 (W-PUB-REF-01B) is an explicit, non-default command:
`publish-reference --receipt <canonical-durable-receipt-index-v1>`.  It exact-
version reads the indexed control-plane receipt, runs the same local
seal/quality/schema freeze, reconciles the staged and receipt object sets in
both directions, and conditional-creates MANIFEST.json only.  The legacy
`publish` command remains the copied-v2 path unchanged.

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
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402
import git_provenance as gp  # noqa: E402

MANIFEST_SCHEMA = "research-release-manifest-v2"
REFERENCE_MANIFEST_SCHEMA = "research-release-manifest-v3-reference"
REFERENCE_STORAGE_MODE = "CANONICAL_REFERENCE"
REFERENCE_CONTRACT_VERSION = "reference-v3-tagger-precommit-v1"
CANONICAL_RECEIPT_SCHEMA = "canonical-object-receipt-v1"
CANONICAL_RECEIPT_STATE = "DURABLE_RECEIPT_VERIFIED"
CANONICAL_RECEIPT_AUTHORITY = "CANONICAL_CONTROL_PLANE"
CANONICAL_RECEIPT_INDEX_SCHEMA = "canonical-durable-receipt-index-v1"
CANONICAL_TAGGED_PHASE = "TAGGED_ELIGIBILITY_VERIFIED"
RFQ_ELIGIBILITY_BINDING_SCHEMA = "canonical-rfq-eligibility-binding-v1"
RFQ_ELIGIBILITY_BINDING_STATE = "ELIGIBLE_SEALED_REFERENCE"
RFQ_DUAL_TAG_STATE = "DUAL_TAGGED_VERIFIED"
MAX_DURABLE_INDEX_BYTES = 1024 * 1024
MAX_CANONICAL_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_TAG_PRECOMMIT_PROOF_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_PREPARED_PLAN_BYTES = 32 * 1024 * 1024
TAG_PRECOMMIT_PROOF_SCHEMA = "canonical-eligibility-precommit-proof-v1"
TAG_PRECOMMIT_PROOF_STATE = "EXACT_VERSION_TAG_READBACK_VERIFIED"
TAG_PRECOMMIT_MAX_AGE_SECONDS = 15 * 60
TAG_PRECOMMIT_FUTURE_SKEW_SECONDS = 5 * 60
REFERENCE_PREPARED_PLAN_SCHEMA = "research-reference-prepared-plan-v1"
REFERENCE_PREPARED_PLAN_STATE = "REFERENCE_MANIFEST_PREPARED"
CANONICAL_TAGGER_ARN = (
    "arn:aws:iam::321572485933:user/canonical-eligibility-tagger")
DEST_DEFAULT = "s3://kalshi-vault-ritcardo/research"
RAW_VAULT_DEFAULT = "s3://kalshi-vault-ritcardo/ec2/raw"
DUCKDB_MEMORY_LIMIT = "4GB"  # bounded below the isolated daily cgroup ceiling
CAPTURE_DISK_RESERVE_BYTES = 100 * 1024 * 1024 * 1024
STAGE_METADATA_OVERHEAD_BYTES = 32 * 1024 * 1024


def _research_stage_root():
    path = os.environ.get(
        "RESEARCH_STAGE_ROOT", os.path.join(wc.ROOT, "work", "research_stage"))
    if not os.path.isabs(path):
        raise SystemExit("REFUSED: RESEARCH_STAGE_ROOT must be absolute")
    return os.path.realpath(path)


def _regular_tree_bytes(root):
    """Conservatively size files that this publisher will copy to staging."""
    if not os.path.isdir(root):
        return 0
    total = 0
    for base, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        for name in dirs:
            path = os.path.join(base, name)
            if os.path.islink(path):
                raise SystemExit(
                    "REFUSED: staged input directory is a symlink: %s" % path)
        for name in files:
            path = os.path.join(base, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise SystemExit(
                    "REFUSED: staged input is not a regular file: %s" % path)
            total += st.st_size
    return total


def _estimated_stage_bytes(date, *, reference_mode, include_rfq, seal,
                           warehouse_root, archive_root, quality_dir):
    """Upper-bound bytes copied into the same-disk private stage.

    Reference v3 does not copy immutable fact objects.  Mutable auxiliary
    state is copied, so it is sized before the first stage write.  Legacy copy
    publication also budgets every fact/RFQ byte because a hardlink may fall
    back to a copy.
    """
    total = STAGE_METADATA_OVERHEAD_BYTES + len(
        json.dumps(seal, sort_keys=True).encode("utf-8"))
    if not reference_mode:
        total += _regular_tree_bytes(
            os.path.join(warehouse_root, "corrections", "date=%s" % date))
        total += _regular_tree_bytes(
            os.path.join(
                warehouse_root, "dim", "snapshots", "date=%s" % date))
        total += _regular_tree_bytes(os.path.join(warehouse_root, "catalog"))
        for path in (
                os.path.join(warehouse_root, "manifest.csv"),
                os.path.join(
                    quality_dir, "capture_gap_receipt_%s.json" % date),
                os.path.join(quality_dir, "l2_gaps_%s.json" % date)):
            if os.path.exists(path):
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode):
                    raise SystemExit(
                        "REFUSED: staged input is not a regular file: %s" % path)
                total += st.st_size
        total += sum(int(row.get("size", 0))
                     for row in seal.get("archive_file_stats", []))
        if include_rfq:
            total += sum(int(row.get("size", 0))
                         for row in seal.get("raw_files", [])
                         if _RFQ_RE.match(os.path.basename(
                             str(row.get("file", "")))))
    return total


LADDER_COLUMNS = ("exchange_ts_us", "recv_wall_ns", "recv_mono_ns",
                  "local_recv_ts_us")
RFQ_FLAG_FILE = os.path.expanduser("~/.kalshi/research_include_rfq")
_RFQ_RE = re.compile(r"^rfq(?:_receipts)?_\d{2}\.ndjson(?:\.\d+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RESEARCH_RELEASE_PROVENANCE_PATHS = (
    "tools/git_provenance.py",
    "tools/warehouse_common.py",
    "tools/canonical_receipts.py",
    "tools/research_reference.py",
    "tools/research_reference_patrol.py",
    "tools/research_release.py",
)

# Reference manifests are expected to remain immediately readable.  S3 omits
# StorageClass for STANDARD objects; the other values below are the online
# (non-restore-gated) classes accepted by the publisher.  Archival and unknown
# future classes fail closed until they are explicitly reviewed.
REFERENCE_READABLE_STORAGE_CLASSES = frozenset((
    "STANDARD", "REDUCED_REDUNDANCY", "STANDARD_IA", "ONEZONE_IA",
    "INTELLIGENT_TIERING", "GLACIER_IR", "EXPRESS_ONEZONE",
))

DAY_US = 86_400_000_000


def require_clean_mutation_provenance():
    try:
        return gp.require_clean_head(
            wc.ROOT, RESEARCH_RELEASE_PROVENANCE_PATHS)
    except gp.GitProvenanceError as exc:
        raise SystemExit(
            "ABORT (fail-closed, %s): %s" % (exc.code, exc.detail))


def research_env_file():
    """The Mac research read-only key file. KALSHI_RESEARCH_ENV_FILE
    overrides for tests only."""
    return os.environ.get("KALSHI_RESEARCH_ENV_FILE") or \
        os.path.expanduser("~/.kalshi/research_s3.env.sh")


def reference_yellow_alert_path(live_dir):
    """Derive the fixed production patrol alert from the active live root."""
    if not isinstance(live_dir, (str, os.PathLike)) or not os.fspath(live_dir):
        raise SystemExit("ABORT (reference patrol): live_dir is required")
    return os.path.join(os.path.abspath(os.fspath(live_dir)),
                        "research_reference_patrol", "YELLOW.json")


def _require_reference_yellow_absent(path):
    if os.path.lexists(path):
        raise SystemExit(
            "ABORT (reference patrol YELLOW): new v3 publication is frozen "
            "while the durable local alert exists: %s" % path)


def require_reference_patrol_clear(reference_mode, alert_path=None,
                                   dest_url=None, live_dir=None):
    """Freeze every v3 publish entry while a durable yellow alert exists."""
    if not reference_mode:
        return None
    fixed = reference_yellow_alert_path(live_dir)
    if str(dest_url or "").startswith("s3://"):
        if alert_path is not None:
            raise SystemExit(
                "ABORT (reference patrol): a real S3 v3 publish cannot "
                "override the fixed YELLOW path")
        path = fixed
    else:
        path = os.path.abspath(alert_path) if alert_path is not None else fixed
    _require_reference_yellow_absent(path)
    return path


def safe_join(base, key):
    """Containment-safe join (remediation item 3): a manifest/seal-derived
    key may NEVER escape its base directory — absolute paths, drive-ish
    separators and any '..' segment are refused, and the resolved path must
    stay inside the resolved base."""
    if not key or key.startswith(("/", "\\")) or "\\" in key:
        raise SystemExit("ABORT (containment): illegal key %r" % key)
    parts = key.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise SystemExit("ABORT (containment): traversal key %r" % key)
    base_real = os.path.realpath(base)
    dst = os.path.realpath(os.path.join(base_real, *parts))
    if dst != base_real and not dst.startswith(base_real + os.sep):
        raise SystemExit("ABORT (containment): %r escapes %s" % (key, base))
    return os.path.join(base_real, *parts)


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

    def read_manifest(self, key):
        """Return the exact existing manifest bytes, never a prefix match."""
        path = os.path.join(self.root, key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SystemExit("ABORT (fail-closed): cannot safely inspect "
                             "existing manifest %s: %s" % (key, exc))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_size > MAX_REFERENCE_MANIFEST_BYTES):
                raise SystemExit("ABORT (fail-closed): existing manifest is "
                                 "not a bounded regular file: %s" % key)
            raw = b""
            while len(raw) <= MAX_REFERENCE_MANIFEST_BYTES:
                chunk = os.read(
                    fd, min(1 << 20,
                            MAX_REFERENCE_MANIFEST_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            if len(raw) != info.st_size:
                raise SystemExit("ABORT (fail-closed): existing manifest "
                                 "changed while reading: %s" % key)
            return raw, None
        finally:
            os.close(fd)

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

    def upload_manifest(self, local, key):
        """Write-once (P0-1): O_EXCL create — an existing manifest is a
        write-once violation and aborts; the written copy is read back and
        sha256-verified."""
        dst = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            with open(dst, "xb") as f, open(local, "rb") as src:
                shutil.copyfileobj(src, f)
        except FileExistsError:
            raise SystemExit("ABORT (fail-closed): MANIFEST already exists "
                             "at %s — write-once violated (concurrent or "
                             "repeated publisher?)" % key)
        if sha256_file(dst) != sha256_file(local):
            raise SystemExit("ABORT (fail-closed): manifest read-back "
                             "mismatch for %s" % key)
        return None

    def verify_object(self, key, size, sha256):
        """Post-upload verification of the ACTUAL destination object (fix 3).
        Returns the VersionId (None here — a filesystem has none; this is
        FIXTURE mode, clearly labeled, never a real publication)."""
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

    binding_mode = "LOCAL_FIXTURE_DEST_NO_VERSIONS"

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
        self.reference_patrol_alert = None

    def bind_reference_patrol(self, live_dir):
        """Bind the immutable production alert derived from publish live_dir."""
        self.reference_patrol_alert = reference_yellow_alert_path(live_dir)

    def _key(self, key):
        return "%s/%s" % (self.prefix, key) if self.prefix else key

    def exists(self, key):
        r = subprocess.run(["aws", "s3", "ls", "%s/%s" % (self.url, key)],
                           capture_output=True, text=True)
        return r.returncode == 0 and key.rsplit("/", 1)[-1] in r.stdout

    def _manifest_history(self, key):
        """Return the sole exact-key version, or ``None`` if never used.

        Current HEAD alone is not a write-once proof: a delete marker makes
        HEAD look absent while older bytes still exist.  Any historical
        delete marker, second version, null VersionId, or truncated response
        is therefore a permanent fail-closed violation.
        """
        exact_key = self._key(key)
        r = subprocess.run([
            "aws", "s3api", "list-object-versions",
            "--bucket", self.bucket, "--prefix", exact_key,
            "--max-items", "3"], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(
                "ABORT (fail-closed): cannot inspect MANIFEST version "
                "history for %s: %s" %
                (key, (r.stderr or "")[:500].strip()))
        try:
            history = json.loads(r.stdout or "{}")
        except ValueError:
            raise SystemExit("ABORT (fail-closed): unparsable MANIFEST "
                             "version history for %s" % key)
        if (not isinstance(history, dict) or history.get("NextToken")
                or history.get("IsTruncated") is True):
            raise SystemExit("ABORT (fail-closed): MANIFEST version history "
                             "is truncated or malformed for %s" % key)
        versions = [row for row in history.get("Versions", [])
                    if isinstance(row, dict) and row.get("Key") == exact_key]
        markers = [row for row in history.get("DeleteMarkers", [])
                   if isinstance(row, dict) and row.get("Key") == exact_key]
        if markers or len(versions) > 1:
            raise SystemExit(
                "ABORT (fail-closed): MANIFEST write-once history violated "
                "for %s (versions=%d delete_markers=%d)" %
                (key, len(versions), len(markers)))
        if not versions:
            return None
        row = versions[0]
        version_id = row.get("VersionId")
        if (not isinstance(version_id, str) or not version_id
                or version_id.lower() == "null"
                or row.get("IsLatest") is not True):
            raise SystemExit("ABORT (fail-closed): MANIFEST history lacks "
                             "one current non-null VersionId for %s" % key)
        return row

    def read_manifest(self, key):
        """HEAD the exact key, then read back that immutable VersionId."""
        historical = self._manifest_history(key)
        if historical is None:
            return None
        r = subprocess.run([
            "aws", "s3api", "head-object", "--bucket", self.bucket,
            "--key", self._key(key)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(
                "ABORT (fail-closed): cannot inspect exact existing "
                "manifest %s after history proof: %s" %
                (key, (r.stderr or "")[:500].strip()))
        try:
            head = json.loads(r.stdout)
        except ValueError:
            raise SystemExit("ABORT (fail-closed): unparsable manifest "
                             "HeadObject response for %s" % key)
        version_id = head.get("VersionId")
        size = head.get("ContentLength")
        if (not isinstance(version_id, str) or not version_id
                or version_id.lower() == "null"
                or version_id != historical.get("VersionId")
                or not isinstance(size, int) or isinstance(size, bool)
                or size < 1 or size > MAX_REFERENCE_MANIFEST_BYTES):
            raise SystemExit("ABORT (fail-closed): existing manifest lacks "
                             "a bounded exact VersionId: %s" % key)
        with tempfile.TemporaryDirectory(
                prefix="reference-existing-manifest-") as root:
            path = os.path.join(root, "MANIFEST.json")
            self._get_exact_version(key, version_id, path)
            if os.path.getsize(path) != size:
                raise SystemExit("ABORT (fail-closed): existing manifest "
                                 "exact-version size mismatch: %s" % key)
            with open(path, "rb") as handle:
                raw = handle.read(MAX_REFERENCE_MANIFEST_BYTES + 1)
        if len(raw) != size or len(raw) > MAX_REFERENCE_MANIFEST_BYTES:
            raise SystemExit("ABORT (fail-closed): existing manifest read "
                             "exceeded its bound: %s" % key)
        final_history = self._manifest_history(key)
        if (final_history is None
                or final_history.get("VersionId") != version_id):
            raise SystemExit("ABORT (fail-closed): MANIFEST history changed "
                             "during exact no-op verification for %s" % key)
        return raw, version_id

    def upload_tree(self, stage_dir, prefix):
        # This is the copied-v2 data mutation primitive.  Prove clean source
        # immediately before spawning the S3 sync, even for direct callers.
        require_clean_mutation_provenance()
        subprocess.run(["aws", "s3", "sync", stage_dir,
                        "%s/%s" % (self.url, prefix),
                        "--no-progress", "--exclude", "MANIFEST.json"],
                       check=True)

    binding_mode = "VERSION_BOUND"

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

    def _get_exact_version(self, key, version_id, out_path):
        r = subprocess.run(["aws", "s3api", "get-object", "--bucket",
                            self.bucket, "--key", self._key(key),
                            "--version-id", version_id, out_path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("ABORT (fail-closed): GetObject by VersionId "
                             "%s failed for %s — exact-version verification "
                             "is mandatory (P0-1). stderr: %s"
                             % (version_id, key, r.stderr.strip()[:300]))

    def verify_object(self, key, size, sha256):
        """P0-1: obtain VersionId A, GET EXACTLY VersionId A, verify
        size+sha256 of those bytes, record A. A destination that returns no
        VersionId is fail-closed NON-PUBLISHABLE (P0-2: bucket versioning
        status is unobservable to vaultWriter — GetBucketVersioning is
        AccessDenied — so VERSIONING_REQUIRED is enforced at the object
        level; there is NO degraded real-S3 mode)."""
        head = self._head(key)
        vid = head.get("VersionId")
        if not vid:
            raise SystemExit(
                "ABORT (fail-closed, VERSIONING_REQUIRED): destination "
                "returned no VersionId for %s. Real S3 publication is "
                "version-bound or it does not happen: bucket versioning "
                "must be confirmed enabled by the operator in the console "
                "(vaultWriter cannot query it — s3:GetBucketVersioning is "
                "AccessDenied). Publication stopped." % key)
        if head.get("ContentLength") != size:
            raise SystemExit("ABORT (fail-closed): S3 object size %s != "
                             "frozen %d for %s"
                             % (head.get("ContentLength"), size, key))
        tmp = os.path.join(_research_stage_root(),
                           ".verify-%d.tmp" % os.getpid())
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        try:
            self._get_exact_version(key, vid, tmp)
            if (os.stat(tmp).st_size != size
                    or sha256_file(tmp) != sha256):
                raise SystemExit("ABORT (fail-closed): bytes of %s at "
                                 "VersionId %s do not match the frozen "
                                 "size/sha256 (concurrent write?)"
                                 % (key, vid))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return vid

    def upload_manifest(self, local, key):
        """P0-1: conditional-create (If-None-Match: *) write-once manifest,
        then read back BY ITS EXACT VersionId and sha256-verify."""
        # MANIFEST is the exposure mutation for both v2 and v3.  Re-check
        # here even when an earlier data sync already passed the gate.
        require_clean_mutation_provenance()
        is_v3 = _local_manifest_is_v3(local)
        if self._manifest_history(key) is not None:
            raise SystemExit("ABORT (fail-closed): MANIFEST already has a "
                             "version history for %s" % key)
        # Close the long staging TOCTOU window: for a real v3 mutation, check
        # the fixed durable YELLOW again at the last boundary before PutObject.
        if is_v3:
            if self.reference_patrol_alert is None:
                raise SystemExit(
                    "ABORT (reference patrol): S3 v3 destination lacks its "
                    "live_dir-derived YELLOW binding")
            _require_reference_yellow_absent(self.reference_patrol_alert)
        r = subprocess.run(["aws", "s3api", "put-object", "--bucket",
                            self.bucket, "--key", self._key(key),
                            "--body", local, "--if-none-match", "*"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(
                "ABORT (fail-closed): conditional MANIFEST create failed "
                "for %s — an object already exists (write-once violated / "
                "concurrent publisher) or the write was rejected. stderr: %s"
                % (key, r.stderr.strip()[:300]))
        try:
            vid = json.loads(r.stdout or "{}").get("VersionId")
        except ValueError:
            vid = None
        if not vid:
            raise SystemExit(
                "ABORT (fail-closed, VERSIONING_REQUIRED): MANIFEST put "
                "returned no VersionId — see P0-2; publication stopped. "
                "NOTE: the manifest object now exists without version "
                "binding and must be operator-disposed.")
        historical = self._manifest_history(key)
        if historical is None or historical.get("VersionId") != vid:
            raise SystemExit("ABORT (fail-closed): MANIFEST post-create "
                             "history does not contain exactly the created "
                             "VersionId for %s" % key)
        tmp = local + ".readback"
        try:
            self._get_exact_version(key, vid, tmp)
            if sha256_file(tmp) != sha256_file(local):
                raise SystemExit("ABORT (fail-closed): MANIFEST read-back "
                                 "at VersionId %s does not match what was "
                                 "written" % vid)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return vid

    def describe(self):
        return self.url


def pick_candidate_versions(list_versions_json, expected_size):
    """Remediation item 2 (pure, unit-testable): the candidate VersionIds for
    an exact-content recovery, size-filtered, newest first. 'Latest' is never
    trusted by itself — every candidate is downloaded and sha256-verified
    against the seal before it may be pinned."""
    versions = list_versions_json.get("Versions") or []
    out = [v for v in versions if v.get("Size") == expected_size
           and v.get("VersionId")]
    out.sort(key=lambda v: v.get("LastModified") or "", reverse=True)
    return [v["VersionId"] for v in out]


class LocalVault:
    """Directory standing in for the ec2/raw vault (fixture tests). A
    filesystem has no object versions; provenance says so explicitly."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def fetch_exact(self, rel, dest, expected_sha256, expected_size):
        src = os.path.join(self.root, rel)
        if not os.path.isfile(src):
            return None
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        if (os.stat(dest).st_size != expected_size
                or sha256_file(dest) != expected_sha256):
            raise SystemExit("ABORT (fail-closed): vault copy of %s does "
                             "not match the seal sha256/size" % rel)
        return {"source": "vault_reconstructed", "source_version_id": None,
                "version_pinning": "LOCAL_FIXTURE_VAULT_NO_VERSIONS"}


class S3Vault:
    """The existing version-pinned raw vault (s3://…/ec2/raw), read-only.

    Remediation item 2: recovery pins the EXACT source object version —
    candidate versions are enumerated with list-object-versions, each
    candidate is downloaded BY VersionId and sha256-verified against the
    seal, and only the matching VersionId is pinned into provenance. An
    unversioned bucket (or a denied listing) is an explicit, loudly-declared
    degraded mode: the plain object is fetched and sha256-verified — never
    silently treated as version-pinned."""

    def __init__(self, url):
        m = re.match(r"^s3://([^/]+)/?(.*)$", url)
        self.bucket = m.group(1)
        self.prefix = m.group(2).rstrip("/")
        self.url = url.rstrip("/")

    def _key(self, rel):
        return "%s/%s" % (self.prefix, rel) if self.prefix else rel

    def fetch_exact(self, rel, dest, expected_sha256, expected_size):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        lv = subprocess.run(
            ["aws", "s3api", "list-object-versions", "--bucket", self.bucket,
             "--prefix", self._key(rel)], capture_output=True, text=True)
        candidates = []
        if lv.returncode == 0:
            try:
                candidates = pick_candidate_versions(json.loads(lv.stdout),
                                                     expected_size)
            except ValueError:
                candidates = []
        for vid in candidates:
            g = subprocess.run(
                ["aws", "s3api", "get-object", "--bucket", self.bucket,
                 "--key", self._key(rel), "--version-id", vid, dest],
                capture_output=True, text=True)
            if g.returncode != 0:
                continue
            if (os.stat(dest).st_size == expected_size
                    and sha256_file(dest) == expected_sha256):
                return {"source": "vault_reconstructed",
                        "source_version_id": vid,
                        "version_pinning": "EXACT_VERSION_SHA256_MATCHED"}
            os.remove(dest)
        if candidates:
            # versions existed but none reproduced the sealed bytes
            return None
        # unversioned bucket or listing denied: explicit degraded recovery
        print("WARNING [research_release]: vault %s returned no object "
              "versions for %s — DEGRADED unversioned recovery (content "
              "still sha256-verified against the seal)"
              % (self.url, rel))
        r = subprocess.run(["aws", "s3", "cp", "%s/%s" % (self.url, rel),
                            dest, "--no-progress"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        if (os.stat(dest).st_size != expected_size
                or sha256_file(dest) != expected_sha256):
            raise SystemExit("ABORT (fail-closed): vault copy of %s does "
                             "not match the seal sha256/size" % rel)
        return {"source": "vault_reconstructed", "source_version_id": None,
                "version_pinning": "UNVERSIONED_OR_LIST_DENIED_DEGRADED"}


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


L2_STAT_KEYS = ("lines", "parse_errors", "seq_gap_events",
                "seq_missed_total", "sids_total", "sids_with_seq_gaps")
L2_RECEIPT_SCHEMA = "l2-gap-receipt-v1"


def validate_l2_receipt(lg, seal, date):
    """P0-3 residual fix 2 (+ final micro-fix): POSITIVE per-date L2 receipt
    binding. Returns (quality_dict, None) only when the receipt carries the
    exact schema_version, binds this date, carries the scan/seq statistics
    (parse_errors REQUIRED, a non-negative integer, preserved into the
    quality block), its no_l2_files flag is a real boolean consistent with
    the sealed L2 inventory, AND its exact file inventory (paths + byte
    sizes) bidirectionally equals the day seal's l2 raw subset — no missing
    files, no extra files, no size drift. Anything less returns
    (None, reason)."""
    if lg.get("schema_version") != L2_RECEIPT_SCHEMA:
        return None, ("receipt schema_version %r is not %r"
                      % (lg.get("schema_version"), L2_RECEIPT_SCHEMA))
    if lg.get("date") != date:
        return None, "receipt does not bind this date"
    inv = lg.get("file_inventory")
    if not isinstance(inv, list):
        return None, ("receipt carries no positive file inventory with "
                      "byte sizes (date-only/header-only receipts are "
                      "invalid)")
    if any(not isinstance(lg.get(k), int) or isinstance(lg.get(k), bool)
           for k in L2_STAT_KEYS):
        return None, ("receipt is missing scan/seq statistics "
                      "(parse_errors and seq counters are required "
                      "integers)")
    if lg["parse_errors"] < 0:
        return None, "parse_errors is negative — not a real scan statistic"
    want = {r["file"]: r["size"] for r in seal.get("raw_files", [])
            if r["file"].startswith("date=%s/" % date)
            and os.path.basename(r["file"]).startswith("l2_")}
    no_l2 = lg.get("no_l2_files")
    if not isinstance(no_l2, bool):
        return None, "no_l2_files is not a boolean"
    if no_l2 != (not want):
        return None, ("no_l2_files=%r contradicts the sealed L2 inventory "
                      "(%d sealed l2 file(s))" % (no_l2, len(want)))
    got = {}
    for e in inv:
        if not isinstance(e, dict) or "file" not in e or "bytes" not in e:
            return None, "receipt file inventory entries are malformed"
        got[e["file"]] = e["bytes"]
    if got != want:
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        sized = sorted(k for k in set(want) & set(got)
                       if want[k] != got[k])
        return None, ("receipt inventory does not reproduce the sealed l2 "
                      "raw subset EXACTLY (stale: missing=%s extra=%s "
                      "byte-size=%s)" % (missing[:3], extra[:3], sized[:3]))
    return {k: lg.get(k) for k in
            ("no_l2_files",) + L2_STAT_KEYS}, None


def derive_evidence_tier(seal, gap_receipt_affirmative, gap_reason=None,
                         l2_facts_present=False, l2_evidence_ok=True,
                         l2_reason=None):
    """Fix 5 + P0-3: the tier is DERIVED, never assumed.
    SEALED_CONFIRMATION requires ALL of: a full_v2 go-eligible seal, an
    AFFIRMATIVE per-date scan receipt whose inventory reproduces the sealed
    raw inventory, matching L2 seq-quality evidence whenever orderbooks_full
    facts exist, and an EXPLICITLY ASSESSED capture quality —
    UNASSESSED_PENDING_PIPE_W03 (or any unassessed/failed state) can NEVER
    earn SEALED_CONFIRMATION. Returns (tier, basis_dict)."""
    reasons = []
    if seal.get("method") != "full_v2":
        reasons.append("seal method=%r (expected full_v2)"
                       % seal.get("method"))
    if seal.get("go_no_go_eligible") is not True:
        reasons.append("seal not go_no_go_eligible")
    if not gap_receipt_affirmative:
        reasons.append("no affirmative per-date gap-scan receipt bound to "
                       "the sealed raw inventory (%s)"
                       % (gap_reason or "absence is not evidence"))
    if l2_facts_present and not l2_evidence_ok:
        reasons.append("orderbooks_full facts present but no VALID "
                       "matching per-date L2 seq-quality receipt (%s) "
                       "(mandatory, P0-3)" % (l2_reason or "absent"))
    cq = str(seal.get("capture_quality_status") or "")
    if (not cq or "UNASSESSED" in cq.upper() or any(
            w in cq.upper()
            for w in ("FAIL", "BAD", "DEGRADED", "REJECT"))):
        reasons.append("capture_quality_status=%r can never earn "
                       "SEALED_CONFIRMATION (P0-3: unassessed quality is "
                       "not confirmation)" % cq)
    tier = "SEALED_CONFIRMATION" if not reasons \
        else "SEALED_DEGRADED_EVIDENCE"
    basis = {
        "seal_status": seal.get("status"),
        "seal_version": seal.get("version"),
        "method": seal.get("method"),
        "go_no_go_eligible": seal.get("go_no_go_eligible"),
        "capture_quality_status": seal.get("capture_quality_status"),
        "gap_receipt_affirmative": bool(gap_receipt_affirmative),
        "l2_facts_present": bool(l2_facts_present),
        "l2_evidence_ok": bool(l2_evidence_ok),
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


def _strict_manifest_json(raw):
    if (not isinstance(raw, bytes) or not raw
            or len(raw) > MAX_REFERENCE_MANIFEST_BYTES):
        _reference_abort("existing reference manifest exceeds its byte bound")

    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key %r" % key)
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode("utf-8"),
                           object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        _reference_abort("existing reference manifest is invalid JSON: %s" %
                         exc)
    if not isinstance(value, dict):
        _reference_abort("existing reference manifest root is not an object")
    return value


def _local_manifest_is_v3(path):
    """Read one stable bounded local manifest and detect any v3 marker."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _reference_abort("cannot safely read staged manifest: %s" % exc)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size < 1
                or before.st_size > MAX_REFERENCE_MANIFEST_BYTES):
            _reference_abort("staged manifest is not a bounded regular file")
        raw = b""
        while len(raw) <= MAX_REFERENCE_MANIFEST_BYTES:
            chunk = os.read(
                fd, min(1 << 20,
                        MAX_REFERENCE_MANIFEST_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(fd)
        if (len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)):
            _reference_abort("staged manifest changed while reading")
    finally:
        os.close(fd)
    manifest = _strict_manifest_json(raw)
    return (manifest.get("schema") == REFERENCE_MANIFEST_SCHEMA
            or manifest.get("schema_version") == 3
            or manifest.get("storage_mode") == REFERENCE_STORAGE_MODE)


def _assert_existing_reference_equivalent(raw, expected):
    """Prove an existing deterministic release is the same publication.

    Publication time and publisher commit are historical metadata, not part
    of release identity.  Every other manifest byte-level semantic must match
    the newly reconstructed expected manifest exactly; extra/missing fields
    and damaged objects fail closed rather than being reported as a no-op.
    """
    existing = _strict_manifest_json(raw)
    for label, value in (("published_at_utc",
                          existing.get("published_at_utc")),
                         ("corrections.cutoff_utc",
                          (existing.get("corrections") or {}).get(
                              "cutoff_utc"))):
        _canonical_modified(value, "existing manifest %s" % label)
    commit = existing.get("publisher_commit")
    if (not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None):
        _reference_abort("existing reference manifest publisher commit "
                         "is invalid")

    def stable(value):
        value = json.loads(json.dumps(value, sort_keys=True))
        value.pop("published_at_utc", None)
        value.pop("publisher_commit", None)
        corrections = value.get("corrections")
        if isinstance(corrections, dict):
            corrections.pop("cutoff_utc", None)
        enforcement = value.get("eligibility_enforcement")
        if isinstance(enforcement, dict):
            # A no-op rerun proves tags afresh but does not rewrite the
            # historical proof recorded by the immutable manifest.
            enforcement.pop("tagger_precommit_proof", None)
        return value

    if stable(existing) != stable(expected):
        _reference_abort(
            "existing reference manifest conflicts with reconstructed "
            "publication state")
    return existing


def _reference_abort(detail):
    raise SystemExit("ABORT (reference receipt): %s" % detail)


def _safe_reference_rel(value, label):
    if (not isinstance(value, str) or not value
            or value.startswith(("/", "\\")) or "\\" in value
            or "\x00" in value):
        _reference_abort("unsafe %s %r" % (label, value))
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _reference_abort("unsafe %s %r" % (label, value))
    return "/".join(parts)


def _valid_sha256(value):
    return isinstance(value, str) and _SHA256_RE.match(value) is not None


def _read_local_index_nofollow(path):
    """Read the small local durable index through one pinned regular-file fd.

    The path is an operator input, not receipt authority.  Nevertheless it
    must not be a symlink or race a pre-read stat/open pair, and a malicious
    sparse/large file must be rejected before an unbounded allocation.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        _reference_abort("O_NOFOLLOW is required for the durable index")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = None
    try:
        fd = os.open(os.fspath(path), flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _reference_abort("durable receipt index is not a regular file")
        if before.st_size > MAX_DURABLE_INDEX_BYTES:
            _reference_abort("durable receipt index exceeds size cap")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(1 << 20,
                                    MAX_DURABLE_INDEX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DURABLE_INDEX_BYTES:
                _reference_abort("durable receipt index exceeds size cap")
        after = os.fstat(fd)
        path_after = os.stat(os.fspath(path), follow_symlinks=False)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)
        if (stat.S_ISLNK(path_after.st_mode)
                or not stat.S_ISREG(path_after.st_mode)
                or identity(before) != identity(after)
                or (path_after.st_dev, path_after.st_ino)
                != (after.st_dev, after.st_ino)
                or total != after.st_size):
            _reference_abort("durable receipt index changed while reading")
        return b"".join(chunks)
    except SystemExit:
        raise
    except OSError as exc:
        _reference_abort("cannot safely read durable receipt index %s: %s" %
                         (path, exc))
    finally:
        if fd is not None:
            os.close(fd)


def _prepared_plan_bytes(value):
    try:
        return (json.dumps(value, indent=2, sort_keys=True,
                           ensure_ascii=True, allow_nan=False) + "\n").encode(
                               "utf-8")
    except (TypeError, ValueError) as exc:
        _reference_abort("prepared plan cannot be canonicalized: %s" % exc)


def _write_prepared_reference_plan(root, value):
    """Atomically retain one content-addressed prepare result for commit."""
    root_value = os.fspath(root)
    if not os.path.isabs(root_value):
        _reference_abort("prepared plan output root must be absolute")
    root = os.path.abspath(root_value)
    os.makedirs(root, mode=0o700, exist_ok=True)
    root_stat = os.stat(root, follow_symlinks=False)
    if (not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode)
            or root_stat.st_uid not in {0, os.geteuid()}
            or root_stat.st_mode & 0o022):
        _reference_abort("prepared plan output root is not a trusted directory")
    raw = _prepared_plan_bytes(value)
    if len(raw) > MAX_REFERENCE_PREPARED_PLAN_BYTES:
        _reference_abort("prepared plan exceeds its fixed byte bound")
    digest = hashlib.sha256(raw).hexdigest()
    path = os.path.join(root, "PREPARED-%s.json" % digest)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_CLOEXEC", 0), 0o600)
    except FileExistsError:
        existing, existing_sha = _read_prepared_reference_plan(path)
        if existing_sha != digest or _prepared_plan_bytes(existing) != raw:
            _reference_abort("prepared plan content-address conflict")
        return path, digest, len(raw)
    except OSError as exc:
        _reference_abort("cannot create prepared plan: %s" % exc)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    return path, digest, len(raw)


def _read_prepared_reference_plan(path):
    """Freeze and authenticate a bounded PREPARED-<sha256>.json artifact."""
    if not hasattr(os, "O_NOFOLLOW"):
        _reference_abort("O_NOFOLLOW is required for the prepared plan")
    path = os.path.abspath(os.fspath(path))
    match = re.fullmatch(r"PREPARED-([0-9a-f]{64})\.json",
                         os.path.basename(path))
    if match is None:
        _reference_abort("prepared plan filename is not content-addressed")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0))
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size < 1
                or before.st_size > MAX_REFERENCE_PREPARED_PLAN_BYTES
                or before.st_uid not in {0, os.geteuid()}
                or before.st_mode & 0o022):
            _reference_abort("prepared plan is not a bounded regular file")
        chunks = []
        total = 0
        while total <= MAX_REFERENCE_PREPARED_PLAN_BYTES:
            chunk = os.read(
                fd, min(1 << 20,
                        MAX_REFERENCE_PREPARED_PLAN_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        fingerprint = lambda value: (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)
        if (len(raw) != before.st_size
                or fingerprint(before) != fingerprint(after)
                or stat.S_ISLNK(named.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)):
            _reference_abort("prepared plan changed while reading")
    except SystemExit:
        raise
    except OSError as exc:
        _reference_abort("cannot safely read prepared plan: %s" % exc)
    finally:
        if fd is not None:
            os.close(fd)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != match.group(1):
        _reference_abort("prepared plan content-address hash mismatch")

    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key %r" % key)
            value[key] = item
        return value

    try:
        plan = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        _reference_abort("prepared plan is invalid JSON: %s" % exc)
    if not isinstance(plan, dict):
        _reference_abort("prepared plan root is not an object")
    return plan, digest


def _read_tag_precommit_proof(path):
    """Freeze one bounded, content-addressed local tagger proof."""
    if not hasattr(os, "O_NOFOLLOW"):
        _reference_abort("O_NOFOLLOW is required for the precommit proof")
    path = os.path.abspath(os.fspath(path))
    match = re.fullmatch(r"PRECOMMIT-([0-9a-f]{64})\.json",
                         os.path.basename(path))
    if match is None:
        _reference_abort("precommit proof filename is not content-addressed")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0))
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size < 1
                or before.st_size > MAX_TAG_PRECOMMIT_PROOF_BYTES):
            _reference_abort("precommit proof is not a bounded regular file")
        chunks = []
        total = 0
        while total <= MAX_TAG_PRECOMMIT_PROOF_BYTES:
            chunk = os.read(
                fd, min(1 << 20,
                        MAX_TAG_PRECOMMIT_PROOF_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        path_after = os.stat(path, follow_symlinks=False)
        fingerprint = lambda value: (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)
        if (len(raw) != before.st_size
                or fingerprint(before) != fingerprint(after)
                or stat.S_ISLNK(path_after.st_mode)
                or not stat.S_ISREG(path_after.st_mode)
                or (path_after.st_dev, path_after.st_ino)
                != (after.st_dev, after.st_ino)):
            _reference_abort("precommit proof changed while reading")
    except SystemExit:
        raise
    except OSError as exc:
        _reference_abort("cannot safely read precommit proof: %s" % exc)
    finally:
        if fd is not None:
            os.close(fd)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != match.group(1):
        _reference_abort("precommit proof content-address hash mismatch")

    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key %r" % key)
            value[key] = item
        return value

    try:
        proof = json.loads(raw.decode("utf-8"),
                           object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        _reference_abort("precommit proof is invalid JSON: %s" % exc)
    if not isinstance(proof, dict):
        _reference_abort("precommit proof root is not an object")
    return proof, digest


def _validate_tag_precommit_proof(path, *, date, receipt,
                                  receipt_binding, references,
                                  expected_tagger_arn=None, now=None):
    """Bind a fresh tagger readback proof to every manifest reference."""
    proof, proof_sha = _read_tag_precommit_proof(path)
    root_fields = {
        "schema_version", "state", "date",
        "tagged_receipt_set_sha256",
        "byte_attestation_receipt_set_sha256",
        "eligibility_single_writer_audit_sha256",
        "tagged_receipt_object", "target_set_sha256", "target_count",
        "research_candidate_count", "targets", "tagger_sts_caller_arn",
        "tagger_sts_account", "tagger_sts_user_id", "generated_at_utc",
        "rfq", "tag_puts",
    }
    if set(proof) != root_fields:
        _reference_abort("precommit proof root differs from the fixed schema")
    fixed = {
        "schema_version": TAG_PRECOMMIT_PROOF_SCHEMA,
        "state": TAG_PRECOMMIT_PROOF_STATE,
        "date": date,
        "tagged_receipt_set_sha256": receipt.get("receipt_set_sha256"),
        "byte_attestation_receipt_set_sha256":
            receipt.get("byte_attestation_receipt_set_sha256"),
        "eligibility_single_writer_audit_sha256":
            receipt.get("eligibility_single_writer_audit_sha256"),
        "rfq": "OFF",
        "tag_puts": 0,
    }
    if any(proof.get(field) != expected for field, expected in fixed.items()):
        _reference_abort("precommit proof receipt/lineage binding mismatch")
    generated = _canonical_modified(
        proof.get("generated_at_utc"), "precommit proof generated_at_utc")
    try:
        generated_dt = datetime.datetime.fromisoformat(
            generated.replace("Z", "+00:00"))
    except ValueError as exc:
        _reference_abort("precommit proof time is invalid: %s" % exc)
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    if now_dt.tzinfo is None:
        _reference_abort("precommit proof validation time is not timezone-aware")
    age = (now_dt.astimezone(datetime.timezone.utc) - generated_dt).total_seconds()
    if age < -TAG_PRECOMMIT_FUTURE_SKEW_SECONDS:
        _reference_abort("precommit proof is from the future")
    if age > TAG_PRECOMMIT_MAX_AGE_SECONDS:
        _reference_abort("precommit proof is stale")

    arn = proof.get("tagger_sts_caller_arn")
    account = proof.get("tagger_sts_account")
    user_id = proof.get("tagger_sts_user_id")
    arn_match = (re.fullmatch(
        r"arn:aws:iam::([0-9]{12}):user/[A-Za-z0-9+=,.@_/-]+", arn)
        if isinstance(arn, str) else None)
    if (arn_match is None or account != arn_match.group(1)
            or not isinstance(user_id, str) or not user_id.strip()
            or (expected_tagger_arn is not None and arn != expected_tagger_arn)):
        _reference_abort("precommit proof tagger STS identity is invalid")

    tagged_object = proof.get("tagged_receipt_object")
    if not isinstance(tagged_object, dict):
        _reference_abort("precommit proof tagged receipt object is missing")
    expected_receipt = {
        "bucket": receipt_binding["bucket"],
        "key": receipt_binding["key"],
        "VersionId": receipt_binding["version_id"],
        "size": receipt_binding["size"],
        "sha256": receipt_binding["sha256"],
    }
    if any(tagged_object.get(field) != value
           for field, value in expected_receipt.items()):
        _reference_abort("precommit proof tagged receipt binding mismatch")

    target_fields = {
        "role", "logical_key", "source_bucket", "source_key",
        "source_version_id", "size", "sha256", "required_tags",
    }
    targets = proof.get("targets")
    if not isinstance(targets, list) or not targets:
        _reference_abort("precommit proof target set is empty")
    normalized = []
    for number, row in enumerate(targets):
        if not isinstance(row, dict) or set(row) != target_fields:
            _reference_abort("precommit proof target %d is malformed" % number)
        if (row.get("role") not in
                ("TAGGED_RECEIPT", "RESEARCH_CANDIDATE")
                or row.get("required_tags") != {"research-eligible": "true"}
                or not _valid_sha256(row.get("sha256"))
                or not isinstance(row.get("size"), int)
                or isinstance(row.get("size"), bool) or row["size"] < 0):
            _reference_abort("precommit proof target %d is invalid" % number)
        for field in ("logical_key", "source_bucket", "source_key",
                      "source_version_id"):
            if not isinstance(row.get(field), str) or not row[field]:
                _reference_abort("precommit proof target %d is invalid" % number)
        normalized.append(dict(row))
    normalized.sort(key=lambda row: (
        row["role"], row["logical_key"], row["source_bucket"],
        row["source_key"], row["source_version_id"]))
    if targets != normalized:
        _reference_abort("precommit proof target set is not canonically ordered")
    identities = [(row["source_bucket"], row["source_key"],
                   row["source_version_id"]) for row in normalized]
    if len(set(identities)) != len(identities):
        _reference_abort("precommit proof target set contains duplicates")
    if (proof.get("target_count") != len(normalized)
            or proof.get("target_set_sha256")
            != canonical_digest(normalized)):
        _reference_abort("precommit proof target-set digest mismatch")

    expected = [{
        "role": "TAGGED_RECEIPT",
        "logical_key": receipt_binding["key"],
        "source_bucket": receipt_binding["bucket"],
        "source_key": receipt_binding["key"],
        "source_version_id": receipt_binding["version_id"],
        "size": receipt_binding["size"],
        "sha256": receipt_binding["sha256"],
        "required_tags": {"research-eligible": "true"},
    }]
    for ref in references:
        if ref.get("kind") == "rfq" or ref.get("channel") == "rfq":
            _reference_abort("precommit proof path is structurally RFQ-off")
        expected.append({
            "role": "RESEARCH_CANDIDATE",
            "logical_key": ref["logical_key"],
            "source_bucket": ref["source_bucket"],
            "source_key": ref["source_key"],
            "source_version_id": ref["source_version_id"],
            "size": ref["size"],
            "sha256": ref["sha256"],
            "required_tags": {"research-eligible": "true"},
        })
    expected.sort(key=lambda row: (
        row["role"], row["logical_key"], row["source_bucket"],
        row["source_key"], row["source_version_id"]))
    if normalized != expected:
        _reference_abort(
            "precommit proof and complete manifest reference set differ")
    if proof.get("research_candidate_count") != len(references):
        _reference_abort("precommit proof candidate count is incomplete")
    return {
        "schema_version": TAG_PRECOMMIT_PROOF_SCHEMA,
        "proof_sha256": proof_sha,
        "generated_at_utc": generated,
        "tagger_sts_caller_arn": arn,
        "target_set_sha256": proof["target_set_sha256"],
        "target_count": proof["target_count"],
        "research_candidate_count": proof["research_candidate_count"],
        "rfq": "OFF",
    }


def _reference_manifest_result(dest, manifest_key, raw, version_id,
                               release_id, state, *, prepared_plan_sha256=None):
    """Emit a machine-readable exact MANIFEST receipt as the final line."""
    digest = hashlib.sha256(raw).hexdigest()
    if isinstance(dest, S3Dest):
        if (not isinstance(version_id, str) or not version_id
                or version_id.lower() == "null"):
            _reference_abort("S3 MANIFEST result has no exact VersionId")
        binding = {
            "bucket": dest.bucket,
            "key": dest._key(manifest_key),
            "VersionId": version_id,
            "size": len(raw),
            "sha256": digest,
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        }
    else:
        binding = {
            "bucket": None,
            "key": manifest_key,
            "VersionId": None,
            "size": len(raw),
            "sha256": digest,
            "verification_state": "LOCAL_FIXTURE_FULL_SHA256",
        }
    result = {
        "schema_version": "research-reference-manifest-commit-result-v1",
        "state": state,
        "release_id": release_id,
        "manifest_object": binding,
        "data_uploads": 0,
        "rfq": "OFF",
    }
    if prepared_plan_sha256 is not None:
        result["prepared_plan_sha256"] = prepared_plan_sha256
    print(json.dumps(result, sort_keys=True))
    return result


def _commit_prepared_reference(*, date, dest_url, live_dir, receipt_path,
                               receipt_reader, tag_precommit_proof,
                               prepared_plan_path, include_rfq):
    """Validate one frozen plan and perform only the small MANIFEST commit.

    No facts/catalog/dim files are reopened here.  The publisher re-authenticates
    the small tagged receipt, proves that its exact candidate set equals the
    content-addressed plan, validates a fresh tagger proof, and then performs
    the conditional MANIFEST create.  The publisher process never receives
    tagger credentials.
    """
    if include_rfq:
        _reference_abort(
            "prepared v3 publication is RFQ-off; RFQ remains independent")
    plan, plan_sha = _read_prepared_reference_plan(prepared_plan_path)
    plan_fields = {
        "schema_version", "state", "prepared_at_utc", "date",
        "destination", "live_dir", "receipt_index", "include_rfq",
        "publisher_commit", "release_id", "manifest_key",
        "reference_set_sha256", "manifest_template",
    }
    if set(plan) != plan_fields:
        _reference_abort("prepared plan root differs from the fixed schema")
    fixed = {
        "schema_version": REFERENCE_PREPARED_PLAN_SCHEMA,
        "state": REFERENCE_PREPARED_PLAN_STATE,
        "date": date,
        "destination": dest_url,
        "live_dir": os.path.abspath(os.fspath(live_dir)),
        "receipt_index": os.path.abspath(os.fspath(receipt_path)),
        "include_rfq": False,
    }
    if any(plan.get(field) != expected for field, expected in fixed.items()):
        _reference_abort("prepared plan invocation binding mismatch")
    _canonical_modified(plan.get("prepared_at_utc"),
                        "prepared plan prepared_at_utc")
    commit = plan.get("publisher_commit")
    if (not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None
            or commit != code_commit()):
        _reference_abort("prepared plan publisher commit is not current")
    if not _valid_sha256(plan.get("reference_set_sha256")):
        _reference_abort("prepared plan reference set digest is invalid")

    template = plan.get("manifest_template")
    if not isinstance(template, dict):
        _reference_abort("prepared plan manifest template is missing")
    manifest = json.loads(json.dumps(template, sort_keys=True))
    release_id = plan.get("release_id")
    manifest_key = plan.get("manifest_key")
    expected_key = "releases/%s/MANIFEST.json" % release_id
    if (not isinstance(release_id, str)
            or not isinstance(manifest_key, str)
            or manifest_key != expected_key
            or manifest.get("release_id") != release_id
            or manifest.get("date") != date
            or manifest.get("published_at_utc") is not None
            or (manifest.get("eligibility_enforcement") or {}).get(
                "tagger_precommit_proof") is not None
            or manifest.get("reference_set_sha256")
            != plan["reference_set_sha256"]):
        _reference_abort("prepared plan manifest template binding mismatch")
    source_seal = manifest.get("source_seal")
    if (not isinstance(source_seal, dict)
            or not _valid_sha256(source_seal.get("sha256"))
            or not isinstance(source_seal.get("size"), int)
            or isinstance(source_seal.get("size"), bool)
            or source_seal["size"] <= 0):
        _reference_abort("prepared plan source seal binding is invalid")

    exact_reader = receipt_reader or _make_canonical_receipt_reader()
    receipt, receipt_binding = _load_authoritative_receipt(
        receipt_path, date, source_seal["sha256"], source_seal["size"],
        exact_reader, verify_candidates=False)
    selected = _receipt_reference_objects(
        receipt, False, receipt.get("seal"), manifest.get("evidence_tier"),
        source_seal["sha256"])
    references = [ref for _logical, ref in selected]
    if references != manifest.get("objects"):
        _reference_abort(
            "prepared plan and current authoritative receipt reference sets differ")
    projection = [{key: ref[key] for key in (
        "logical_key", "source_bucket", "source_key", "source_version_id",
        "size", "sha256")} for ref in references]
    if canonical_digest(projection) != plan["reference_set_sha256"]:
        _reference_abort("prepared plan reference set does not recompute")
    canonical_receipt = manifest.get("canonical_receipt")
    if (not isinstance(canonical_receipt, dict)
            or canonical_receipt.get("receipt_set_sha256")
            != receipt.get("receipt_set_sha256")
            or canonical_receipt.get("receipt_object") != receipt_binding):
        _reference_abort("prepared plan canonical receipt binding mismatch")

    dest = make_dest(dest_url)
    if isinstance(dest, S3Dest):
        dest.bind_reference_patrol(live_dir)
    expected_tagger = (CANONICAL_TAGGER_ARN
                       if isinstance(dest, S3Dest) else None)
    proof_binding = _validate_tag_precommit_proof(
        tag_precommit_proof, date=date, receipt=receipt,
        receipt_binding=receipt_binding, references=references,
        expected_tagger_arn=expected_tagger)
    prepared_at = datetime.datetime.fromisoformat(
        plan["prepared_at_utc"].replace("Z", "+00:00"))
    proof_at = datetime.datetime.fromisoformat(
        proof_binding["generated_at_utc"].replace("Z", "+00:00"))
    if proof_at < prepared_at:
        _reference_abort(
            "tagger precommit proof predates the prepared manifest plan")
    manifest["eligibility_enforcement"][
        "tagger_precommit_proof"] = proof_binding
    manifest["published_at_utc"] = datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        import research_reference as reference_contract
        reference_contract.validate_manifest(manifest, release_id)
    except Exception as exc:
        _reference_abort("prepared manifest contract validation failed: %s" % exc)

    existing = dest.read_manifest(manifest_key)
    if existing is not None:
        raw, existing_version = existing
        _assert_existing_reference_equivalent(raw, manifest)
        _reference_manifest_result(
            dest, manifest_key, raw, existing_version, release_id,
            "REFERENCE_MANIFEST_ALREADY_COMMITTED",
            prepared_plan_sha256=plan_sha)
        return 0

    stage_root = _research_stage_root()
    os.makedirs(stage_root, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix=".reference-commit-", dir=stage_root) as stage_dir:
        manifest_path = os.path.join(stage_dir, "MANIFEST.json")
        raw = _prepared_plan_bytes(manifest)
        with open(manifest_path, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        final_proof = _validate_tag_precommit_proof(
            tag_precommit_proof, date=date, receipt=receipt,
            receipt_binding=receipt_binding, references=references,
            expected_tagger_arn=expected_tagger)
        if final_proof != proof_binding:
            _reference_abort("precommit proof changed before manifest commit")
        manifest_version = dest.upload_manifest(manifest_path, manifest_key)
    _reference_manifest_result(
        dest, manifest_key, raw, manifest_version, release_id,
        "REFERENCE_MANIFEST_COMMITTED", prepared_plan_sha256=plan_sha)
    return 0


def _canonical_modified(value, label):
    try:
        import canonical_receipts as cr
        normalized = cr._canonical_utc(value, label)
    except Exception as exc:
        _reference_abort("%s is invalid: %s" % (label, exc))
    if not isinstance(value, str) or value != normalized:
        _reference_abort("%s is not canonical" % label)
    return normalized


def _require_exact_head(reader, obj, label):
    """Revalidate the exact S3 version's identity and online storage class."""
    try:
        head = reader.head(obj["bucket"], obj["key"], obj["VersionId"])
    except SystemExit:
        raise
    except Exception as exc:
        _reference_abort("%s exact HEAD failed: %s" % (label, exc))
    if not isinstance(head, dict):
        _reference_abort("%s exact HEAD is not an object" % label)
    if (head.get("VersionId") != obj["VersionId"]
            or head.get("ContentLength") != obj["size"]
            or head.get("DeleteMarker") is True):
        _reference_abort("%s exact HEAD identity mismatch" % label)
    indexed_modified = _canonical_modified(
        obj.get("last_modified_utc"), "%s indexed LastModified" % label)
    try:
        import canonical_receipts as cr
        got_modified = cr._canonical_utc(
            head.get("LastModified"), "%s HEAD LastModified" % label)
    except Exception as exc:
        _reference_abort("%s HEAD LastModified is invalid: %s" % (label, exc))
    if got_modified != indexed_modified:
        _reference_abort("%s exact HEAD LastModified mismatch" % label)
    storage_class = head.get("StorageClass") or "STANDARD"
    if (not isinstance(storage_class, str)
            or storage_class not in REFERENCE_READABLE_STORAGE_CLASSES
            or head.get("ArchiveStatus") not in (None, "")):
        _reference_abort("%s storage class is not immediately readable: %r" %
                         (label, storage_class))
    return head


def _is_rfq_receipt_object(obj):
    key = str(obj.get("key") or "")
    logical = str(obj.get("logical_source_key") or "")
    kind = str(obj.get("source_kind") or "")
    return (obj.get("channel") == "rfq" or kind.startswith("raw_rfq")
            or ("/raw/" in ("/" + key)
                and _RFQ_RE.match(os.path.basename(key)) is not None)
            or (logical.startswith("raw/")
                and _RFQ_RE.match(os.path.basename(logical)) is not None))


def _is_forward_version_binding(value):
    if not (isinstance(value, dict)
            and set(value) == {
                "source_evidence", "forward_version_binding_sha256",
                "resolver_evidence_sha256", "resolution"}
            and _valid_sha256(value.get("forward_version_binding_sha256"))
            and _valid_sha256(value.get("resolver_evidence_sha256"))):
        return False
    source = value.get("source_evidence")
    provenance = source.get("provenance") if isinstance(source, dict) else None
    resolution = value.get("resolution")
    return ((provenance == "RETAINED_IMMUTABLE_AUX_SNAPSHOT"
             and resolution in {
                 "CURRENT_EXACT_SHA256_MATCH",
                 "HISTORICAL_EXACT_SHA256_MATCH"})
            or (provenance == "FULL_V2_SEAL_AUTHENTICATED_S3_CUTOFF"
                and resolution
                == "S3_AUTHENTICATED_CUTOFF_EXACT_VERSION")
            or (provenance in {
                    "PRODUCER_EXACT_GENERATION_WITNESS",
                    "LEGACY_MIGRATION_GENERATION_WITNESS"}
                and resolution
                == "PRODUCER_GENERATION_WITNESS_EXACT_VERSION"))


def _is_forward_dim_source_evidence(value):
    if not isinstance(value, dict):
        return False
    provenance = value.get("provenance")
    old = (set(value) == {
        "provenance", "dim_set_sha256", "dim_generation_id",
        "catalog_generation_id"}
        and provenance in {
            "RETAINED_IMMUTABLE_AUX_SNAPSHOT",
            "FULL_V2_SEAL_AUTHENTICATED_S3_CUTOFF"}
        and _valid_sha256(value.get("dim_set_sha256"))
        and isinstance(value.get("dim_generation_id"), str)
        and bool(value["dim_generation_id"])
        and isinstance(value.get("catalog_generation_id"), str)
        and bool(value["catalog_generation_id"]))
    if old:
        return True
    producer = (set(value) == {
        "provenance", "dim_set_sha256", "dim_generation_id",
        "catalog_generation_id", "catalog_dim_coherence_claim"}
        and provenance == "PRODUCER_EXACT_GENERATION_WITNESS"
        and value.get("catalog_dim_coherence_claim") is True
        and _valid_sha256(value.get("dim_set_sha256"))
        and _valid_sha256(value.get("dim_generation_id"))
        and _valid_sha256(value.get("catalog_generation_id")))
    if producer:
        return True
    return (set(value) == {
        "provenance", "dim_set_sha256", "dim_generation_id",
        "catalog_dim_coherence_claim", "dim_source_catalog_generation_id",
        "selected_catalog_generation_id", "relationship"}
        and provenance == "LEGACY_MIGRATION_GENERATION_WITNESS"
        and value.get("catalog_dim_coherence_claim") is False
        and value.get("relationship")
        == "LEGACY_MIGRATION_COMBINATION_NOT_COHERENT"
        and _valid_sha256(value.get("dim_set_sha256"))
        and _valid_sha256(value.get("dim_generation_id"))
        and _valid_sha256(value.get("dim_source_catalog_generation_id"))
        and _valid_sha256(value.get("selected_catalog_generation_id"))
        and value["dim_source_catalog_generation_id"]
        != value["selected_catalog_generation_id"])


def _make_canonical_receipt_reader():
    """Production exact-version reader; tests must inject their fixture."""
    try:
        import canonical_receipts as cr
        return cr.AwsCliS3Client()
    except Exception as exc:
        _reference_abort("cannot initialize canonical receipt reader: %s" % exc)


def _validate_receipt_binding(binding, label, max_size):
    if not isinstance(binding, dict):
        _reference_abort("%s binding is missing" % label)
    bucket = binding.get("bucket")
    key = _safe_reference_rel(binding.get("key"), "%s key" % label)
    version_id = binding.get("VersionId")
    size = binding.get("size")
    digest = binding.get("sha256")
    if (not isinstance(bucket, str) or not bucket or "/" in bucket
            or not isinstance(version_id, str) or not version_id.strip()
            or version_id.lower() == "null"
            or not isinstance(size, int) or isinstance(size, bool)
            or size <= 0 or size > max_size
            or not _valid_sha256(digest)
            or binding.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"):
        _reference_abort("%s exact-version binding is invalid" % label)
    _canonical_modified(
        binding.get("last_modified_utc"), "%s LastModified" % label)
    return bucket, key, version_id, size, digest


def _read_exact_bound_json(reader, binding, label, max_size):
    bucket, key, version_id, size, digest = _validate_receipt_binding(
        binding, label, max_size)
    _require_exact_head(reader, binding, label)
    try:
        with tempfile.TemporaryDirectory(
                prefix="reference-control-readback-") as temp_dir:
            path = os.path.join(temp_dir, "object.json")
            reader.get_exact(bucket, key, version_id, path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0))
            try:
                opened = os.fstat(fd)
                if (not stat.S_ISREG(opened.st_mode)
                        or opened.st_size != size
                        or opened.st_size > max_size):
                    _reference_abort("%s exact body size mismatch" % label)
                chunks = []
                total = 0
                while True:
                    chunk = os.read(fd, min(1 << 20, max_size + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_size:
                        _reference_abort("%s exact body exceeds size cap" % label)
                after = os.fstat(fd)
                if (total != size or after.st_size != opened.st_size
                        or (after.st_dev, after.st_ino, after.st_mtime_ns,
                            after.st_ctime_ns)
                        != (opened.st_dev, opened.st_ino, opened.st_mtime_ns,
                            opened.st_ctime_ns)):
                    _reference_abort("%s exact body changed while reading" % label)
                payload = b"".join(chunks)
            finally:
                os.close(fd)
    except SystemExit:
        raise
    except Exception as exc:
        _reference_abort("%s exact read failed: %s" % (label, exc))
    if hashlib.sha256(payload).hexdigest() != digest:
        _reference_abort("%s exact body SHA mismatch" % label)
    try:
        parsed = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        _reference_abort("%s body is invalid JSON: %s" % (label, exc))
    if not isinstance(parsed, dict):
        _reference_abort("%s JSON root is not an object" % label)
    return parsed, payload


def _rfq_exact_set_sha256(objects):
    projection = [{
        "logical_source_key": obj.get("logical_source_key"),
        "bucket": obj.get("bucket"),
        "key": obj.get("key"),
        "VersionId": obj.get("VersionId"),
        "size": obj.get("size"),
        "sha256": obj.get("sha256"),
        "last_modified_utc": obj.get("last_modified_utc"),
    } for obj in objects]
    projection.sort(key=lambda row: (
        str(row["logical_source_key"]), str(row["bucket"]),
        str(row["key"]), str(row["VersionId"])))
    return canonical_digest(projection)


def _validate_parent_lineage(receipt, parent, parent_sha):
    """Prove the tagged receipt preserves its byte-attestation parent."""
    fixed = {
        "schema_version": CANONICAL_RECEIPT_SCHEMA,
        "state": CANONICAL_RECEIPT_STATE,
        "authority": CANONICAL_RECEIPT_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": receipt["date"],
        "receipt_set_sha256": parent_sha,
    }
    if any(parent.get(field) != expected for field, expected in fixed.items()):
        _reference_abort("byte-attestation parent authority/identity mismatch")
    if parent.get("receipt_phase") == CANONICAL_TAGGED_PHASE:
        _reference_abort("byte-attestation parent is itself a tagged receipt")
    parent_objects = parent.get("objects")
    parent_families = parent.get("families")
    parent_seal = parent.get("seal")
    if (not isinstance(parent_objects, list) or not parent_objects
            or not isinstance(parent_families, list)
            or not isinstance(parent_seal, dict)):
        _reference_abort("byte-attestation parent inventory is malformed")
    try:
        import canonical_receipts as cr
        parent_binding = dict(parent_seal)
        parent_binding["families"] = parent_families
        if (cr.receipt_set_sha256(
                parent["date"], parent_binding, parent_objects) != parent_sha
                or parent.get("durability_set_sha256")
                != cr.scoped_object_set_sha256(
                    parent_objects, "durability_scope")
                or parent.get("research_candidate_set_sha256")
                != cr.scoped_object_set_sha256(
                    parent_objects, "research_candidate")):
            _reference_abort("byte-attestation parent stable digest mismatch")
    except SystemExit:
        raise
    except Exception as exc:
        _reference_abort("byte-attestation parent is invalid: %s" % exc)
    if parent_seal != receipt.get("seal") or parent_families != receipt.get(
            "families"):
        _reference_abort("tagged receipt changed parent seal/families")
    parent_by_logical = {}
    for obj in parent_objects:
        if not isinstance(obj, dict):
            _reference_abort("byte-attestation parent object is malformed")
        logical = obj.get("logical_source_key")
        if logical in parent_by_logical:
            _reference_abort("byte-attestation parent has duplicate logical key")
        if (obj.get("research_eligible") is not False
                or obj.get("eligibility_tag_state") == "TAGGED_VERIFIED"
                or obj.get("eligibility_tag_state") == RFQ_DUAL_TAG_STATE):
            _reference_abort("byte-attestation parent falsely claims eligibility")
        parent_by_logical[logical] = obj
    current_by_logical = {
        obj.get("logical_source_key"): obj for obj in receipt["objects"]}
    if set(parent_by_logical) != set(current_by_logical):
        _reference_abort("tagged receipt changed parent object set")
    immutable_fields = (
        "bucket", "key", "VersionId", "size", "sha256",
        "last_modified_utc", "logical_source_key", "source_kind", "family",
        "table", "channel", "date", "seal_binding",
        "durability_verified", "mutable_source", "attestation_class",
        "durability_scope", "version_resolution", "canonical_source",
        "verification_state", "verified_at_utc", "publisher_code_commit",
    )
    for logical, parent_obj in parent_by_logical.items():
        current = current_by_logical[logical]
        if any(parent_obj.get(field) != current.get(field)
               for field in immutable_fields):
            _reference_abort(
                "tagged receipt changed parent byte attestation at %s" % logical)
        current_is_rfq_candidate = (
            current.get("research_candidate") is True
            and _is_rfq_receipt_object(current))
        if current_is_rfq_candidate and (
                parent_obj.get("research_candidate") is not False
                or parent_obj.get("research_eligible") is not False
                or parent_obj.get("eligibility_tag_state")
                != "SHADOW_NOT_TAGGED"
                or parent_obj.get("required") is not False
                or parent_obj.get("exposure_policy")
                != "FORBIDDEN_RFQ_DEFAULT"
                or parent_obj.get("evidence_binding") != "seal.raw_files"):
            _reference_abort(
                "RFQ eligibility transition has an invalid byte parent at %s"
                % logical)
        if (not current_is_rfq_candidate
                and parent_obj.get("evidence_binding")
                != current.get("evidence_binding")):
            _reference_abort(
                "tagged receipt changed parent evidence at %s" % logical)
        if (not current_is_rfq_candidate
                and (parent_obj.get("research_candidate")
                     != current.get("research_candidate")
                     or parent_obj.get("required")
                     != current.get("required"))):
            _reference_abort(
                "tagged receipt changed parent publication scope at %s" %
                logical)
        if current.get("research_candidate") is not True:
            policy_fields = (
                "research_candidate", "research_eligible",
                "eligibility_tag_state", "exposure_policy", "required")
            if any(parent_obj.get(field) != current.get(field)
                   for field in policy_fields):
                _reference_abort(
                    "tagged receipt changed non-candidate policy at %s" %
                    logical)


def _load_authoritative_receipt(path, date, seal_sha, seal_size, reader,
                                verify_candidates=True):
    """Authenticate a durable index, exact-GET its receipt, then validate it.

    The receipt digest contract is owned by canonical_receipts.py.  Reusing
    that projection here prevents the reference publisher and receipt writer
    from silently disagreeing about which object semantics are authoritative.
    A self-declared local receipt is never authority: the local input is only
    the durable index written after S3 read-back, and the receipt body is read
    again by the index's exact receipt-object VersionId.  The expensive prepare
    phase HEADs every candidate.  A prepared commit may set
    ``verify_candidates=False`` because it re-reads the immutable exact receipt
    and compares its full reference set to the content-addressed prepared plan;
    this keeps the proof-to-commit window bounded to small control reads.
    """
    try:
        index_payload = _read_local_index_nofollow(path)
        index = json.loads(index_payload)
    except SystemExit:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        _reference_abort("cannot read durable receipt index %s: %s" %
                         (path, exc))
    if not isinstance(index, dict):
        _reference_abort("durable receipt index root is not an object")
    index_fixed = {
        "schema_version": CANONICAL_RECEIPT_INDEX_SCHEMA,
        "state": CANONICAL_RECEIPT_STATE,
        "date": date,
        "complete": True,
        "completed": True,
        "prune_eligible": False,
        "receipt_phase": CANONICAL_TAGGED_PHASE,
        "receipt_object_eligibility_tag_state": "TAGGED_VERIFIED",
    }
    for field, expected in index_fixed.items():
        if index.get(field) != expected:
            _reference_abort("durable index %s=%r, expected %r" %
                             (field, index.get(field), expected))
    index_set_sha = index.get("receipt_set_sha256")
    if not _valid_sha256(index_set_sha):
        _reference_abort("durable index has invalid receipt_set_sha256")
    parent_sha = index.get("byte_attestation_receipt_set_sha256")
    audit_sha = index.get("eligibility_single_writer_audit_sha256")
    if (not _valid_sha256(parent_sha) or not _valid_sha256(audit_sha)
            or parent_sha == index_set_sha):
        _reference_abort("durable index has invalid audit/parent lineage")
    receipt_object = index.get("receipt_object")
    (receipt_bucket, receipt_key, receipt_version_id, receipt_size,
     receipt_digest) = _validate_receipt_binding(
        receipt_object, "durable receipt object", MAX_CANONICAL_RECEIPT_BYTES)
    if (index.get("receipt_payload_size") != receipt_size
            or index.get("receipt_payload_sha256") != receipt_digest):
        _reference_abort("durable index payload binding differs from receipt_object")
    expected_suffix = (
        "control/canonical-receipts/v1/date=%s/receipt-%s.json" %
        (date, index_set_sha))
    if receipt_key != "ec2/" + expected_suffix:
        _reference_abort("durable receipt object key is outside its digest path")
    if reader is None:
        _reference_abort("an exact-version durable receipt reader is required")
    receipt, payload = _read_exact_bound_json(
        reader, receipt_object, "durable receipt",
        MAX_CANONICAL_RECEIPT_BYTES)
    fixed = {
        "schema_version": CANONICAL_RECEIPT_SCHEMA,
        "state": CANONICAL_RECEIPT_STATE,
        "authority": CANONICAL_RECEIPT_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": date,
    }
    for field, expected in fixed.items():
        if receipt.get(field) != expected:
            _reference_abort("receipt %s=%r, expected %r" %
                             (field, receipt.get(field), expected))
    body_lineage = {
        "receipt_phase": CANONICAL_TAGGED_PHASE,
        "byte_attestation_receipt_set_sha256": parent_sha,
        "eligibility_single_writer_audit_sha256": audit_sha,
    }
    for field, expected in body_lineage.items():
        if receipt.get(field) != expected:
            _reference_abort("tagged receipt %s does not match final index" %
                             field)
    verified_at = receipt.get("eligibility_verified_at_utc")
    _canonical_modified(verified_at, "tagged receipt eligibility time")
    tagger_commit = receipt.get("eligibility_tagger_code_commit")
    if not isinstance(tagger_commit, str) or not tagger_commit.strip():
        _reference_abort("tagged receipt has no eligibility tagger commit")

    byte_receipt_object = index.get("byte_receipt_object")
    (parent_bucket, parent_key, _parent_version, _parent_size,
     _parent_digest) = _validate_receipt_binding(
        byte_receipt_object, "byte-attestation parent receipt",
        MAX_CANONICAL_RECEIPT_BYTES)
    parent_expected_key = (
        "ec2/control/canonical-receipts/v1/date=%s/receipt-%s.json" %
        (date, parent_sha))
    if (parent_bucket != receipt_bucket or parent_key != parent_expected_key):
        _reference_abort("byte-attestation parent receipt leaves canonical scope")
    parent, _parent_payload = _read_exact_bound_json(
        reader, byte_receipt_object, "byte-attestation parent receipt",
        MAX_CANONICAL_RECEIPT_BYTES)

    objects = receipt.get("objects")
    families = receipt.get("families")
    seal = receipt.get("seal")
    if not isinstance(objects, list) or not objects:
        _reference_abort("receipt object inventory is empty or malformed")
    if not isinstance(families, list) or not families:
        _reference_abort("receipt family inventory is empty or malformed")
    if not isinstance(seal, dict):
        _reference_abort("receipt seal binding is missing")

    try:
        import canonical_receipts as cr
        binding = dict(seal)
        binding["families"] = families
        expected_set = cr.receipt_set_sha256(date, binding, objects)
        expected_durability = cr.scoped_object_set_sha256(
            objects, "durability_scope")
        expected_research = cr.scoped_object_set_sha256(
            objects, "research_candidate")
    except Exception as exc:
        _reference_abort("receipt stable projection is invalid: %s" % exc)
    for field, expected in (
            ("receipt_set_sha256", expected_set),
            ("durability_set_sha256", expected_durability),
            ("research_candidate_set_sha256", expected_research)):
        observed = receipt.get(field)
        if not _valid_sha256(observed) or observed != expected:
            _reference_abort("%s mismatch (got %r, recomputed %s)" %
                             (field, observed, expected))
    if receipt.get("receipt_set_sha256") != index_set_sha:
        _reference_abort("durable index and receipt set digests differ")

    seen_physical, seen_logical = set(), set()
    objects_by_family = {}
    seal_objects = []
    research_candidates = []
    for index, obj in enumerate(objects):
        if not isinstance(obj, dict):
            _reference_abort("receipt object %d is not an object" % index)
        label = "receipt object %d" % index
        bucket = obj.get("bucket")
        if not isinstance(bucket, str) or not bucket or "/" in bucket:
            _reference_abort("%s has invalid bucket %r" % (label, bucket))
        key = _safe_reference_rel(obj.get("key"), "%s key" % label)
        logical = _safe_reference_rel(
            obj.get("logical_source_key"), "%s logical key" % label)
        source_kind = obj.get("source_kind")
        family_name = obj.get("family")
        if not isinstance(source_kind, str) or not source_kind:
            _reference_abort("%s has no source_kind" % label)
        if not isinstance(family_name, str) or not family_name:
            _reference_abort("%s has no family" % label)
        objects_by_family.setdefault(family_name, []).append(obj)
        object_date = obj.get("date")
        try:
            datetime.date.fromisoformat(object_date)
        except (TypeError, ValueError):
            _reference_abort("%s has invalid date %r" % (label, object_date))
        if not isinstance(obj.get("required"), bool):
            _reference_abort("%s required is not boolean" % label)
        try:
            json.dumps(obj.get("seal_binding"), sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
            json.dumps(obj.get("evidence_binding"), sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            _reference_abort("%s has a non-canonical binding" % label)
        version_id = obj.get("VersionId")
        if (not isinstance(version_id, str) or not version_id.strip()
                or version_id.lower() == "null"):
            _reference_abort("%s has no exact VersionId" % label)
        size = obj.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            _reference_abort("%s has invalid size %r" % (label, size))
        if not _valid_sha256(obj.get("sha256")):
            _reference_abort("%s has invalid sha256" % label)
        identity = (bucket, key, version_id)
        if identity in seen_physical:
            _reference_abort("duplicate physical exact version %r" %
                             (identity,))
        if logical in seen_logical:
            _reference_abort("duplicate logical key %s" % logical)
        seen_physical.add(identity)
        seen_logical.add(logical)
        if obj.get("durability_verified") is not True:
            _reference_abort("%s is not durability_verified" % label)
        if obj.get("verification_state") != "EXACT_VERSION_FULL_SHA256":
            _reference_abort("%s lacks exact-version SHA verification" % label)
        if not isinstance(obj.get("research_candidate"), bool):
            _reference_abort("%s research_candidate is not boolean" % label)
        if obj.get("research_candidate") is True:
            if obj.get("research_eligible") is not True:
                _reference_abort("research candidate %s is not eligible" % logical)
            is_rfq = _is_rfq_receipt_object(obj)
            expected_tag_state = (RFQ_DUAL_TAG_STATE if is_rfq
                                  else "TAGGED_VERIFIED")
            if obj.get("eligibility_tag_state") != expected_tag_state:
                _reference_abort(
                    "research candidate %s is not %s" %
                    (logical, expected_tag_state))
            if (obj.get("eligibility_verified_at_utc") != verified_at
                    or obj.get("eligibility_tagger_code_commit")
                    != tagger_commit):
                _reference_abort(
                    "research candidate %s has inconsistent tag attestation" %
                    logical)
            exposure = str(obj.get("exposure_policy") or "")
            if exposure.startswith("FORBIDDEN"):
                _reference_abort("research candidate %s has forbidden policy %s"
                                 % (logical, exposure))
            if ((is_rfq and exposure != "RESEARCH_ELIGIBLE_SEALED_RFQ")
                    or (not is_rfq and exposure != "RESEARCH_ELIGIBLE")):
                _reference_abort(
                    "research candidate %s has invalid exposure policy %s" %
                    (logical, exposure))
            research_candidates.append(obj)
        elif (obj.get("research_eligible") is not False
              or obj.get("eligibility_tag_state") in
              ("TAGGED_VERIFIED", RFQ_DUAL_TAG_STATE)):
            _reference_abort("non-candidate %s falsely claims eligibility" %
                             logical)
        if source_kind == "seal":
            seal_objects.append(obj)

    family_names = set()
    for family in families:
        if not isinstance(family, dict) or not isinstance(family.get("name"), str):
            _reference_abort("receipt family entry is malformed")
        if family["name"] in family_names:
            _reference_abort("duplicate receipt family %s" % family["name"])
        family_names.add(family["name"])
        if family.get("policy") not in (
                "REQUIRED_CORE", "REQUIRED_RESEARCH", "CONDITIONAL"):
            _reference_abort("receipt family %s has unknown policy" %
                             family["name"])
        rows = objects_by_family.get(family["name"], [])
        observed = family.get("observed_count")
        expected = family.get("expected_count")
        if (not isinstance(observed, int) or isinstance(observed, bool)
                or not isinstance(expected, int) or isinstance(expected, bool)
                or observed < 0 or expected < 0 or observed != len(rows)):
            _reference_abort("receipt family %s count mismatch" %
                             family["name"])
        projection = [{
            "bucket": row["bucket"], "key": row["key"],
            "size": row["size"], "sha256": row["sha256"],
        } for row in rows]
        projection.sort(key=lambda row: (row["bucket"], row["key"]))
        expected_objects_digest = cr.canonical_sha256(projection)
        if family.get("objects_digest") != expected_objects_digest:
            _reference_abort("receipt family %s objects_digest mismatch" %
                             family["name"])
        state = family.get("state")
        if state not in ("PRESENT_VERIFIED", "NOT_APPLICABLE"):
            _reference_abort("receipt family %s is %r" %
                             (family["name"], state))
        if ((state == "PRESENT_VERIFIED" and expected != observed)
                or (state == "NOT_APPLICABLE"
                    and (expected != 0 or observed != 0))):
            _reference_abort("receipt family %s state/count disagree" %
                             family["name"])
        if (family.get("policy") in ("REQUIRED_CORE", "REQUIRED_RESEARCH")
                and state != "PRESENT_VERIFIED"):
            _reference_abort("required receipt family %s is not verified" %
                             family["name"])
    unknown_families = sorted(set(objects_by_family) - family_names)
    if unknown_families:
        _reference_abort("objects name unregistered receipt families %s" %
                         unknown_families)

    if len(seal_objects) != 1:
        _reference_abort("receipt must contain exactly one seal object")
    if (seal.get("date") != date or seal.get("status") != "SEALED"
            or seal.get("version") != 2 or seal.get("method") != "full_v2"
            or not _valid_sha256(seal.get("manifest_date_sha256"))):
        _reference_abort("receipt seal does not bind the requested sealed day")
    seal_obj = seal_objects[0]
    for field in ("bucket", "key", "VersionId", "size", "sha256"):
        if seal.get(field) != seal_obj.get(field):
            _reference_abort("receipt seal %s differs from its object entry" %
                             field)
    if seal_obj.get("sha256") != seal_sha or seal_obj.get("size") != seal_size:
        _reference_abort("receipt seal bytes differ from the verified local seal")
    seal_suffix = "warehouse/seals/date=%s.json" % date
    seal_key = seal_obj["key"]
    if not seal_key.endswith(seal_suffix):
        _reference_abort("receipt seal key is outside the canonical prefix")
    canonical_prefix = seal_key[:-len(seal_suffix)].rstrip("/")
    expected_receipt_key = (canonical_prefix + "/" + expected_suffix)
    if (receipt_bucket != seal_obj["bucket"]
            or receipt_key != expected_receipt_key):
        _reference_abort(
            "durable receipt object is outside the seal authority "
            "(got %s/%s, expected %s/%s)" %
            (receipt_bucket, receipt_key, seal_obj["bucket"],
             expected_receipt_key))

    _validate_parent_lineage(receipt, parent, parent_sha)

    rfq_candidates = [obj for obj in research_candidates
                      if _is_rfq_receipt_object(obj)]
    if rfq_candidates:
        rfq_set_sha = _rfq_exact_set_sha256(rfq_candidates)
        required_binding = {
            "schema_version": RFQ_ELIGIBILITY_BINDING_SCHEMA,
            "state": RFQ_ELIGIBILITY_BINDING_STATE,
            "source": "seal.raw_files",
            "seal_sha256": seal_sha,
            "rfq_exact_set_sha256": rfq_set_sha,
            "evidence_tier": "SEALED_CONFIRMATION",
            "integrity_state": "PASS",
            "quarantine_state": "CLEAR",
            "repair_branch_state": "CLOSED_NO_REPAIR",
        }
        allowed_fields = set(required_binding) | {
            "eligibility_evidence_sha256"}
        for obj in rfq_candidates:
            binding = obj.get("evidence_binding")
            logical = obj["logical_source_key"]
            if (not isinstance(binding, dict)
                    or set(binding) != allowed_fields
                    or any(binding.get(field) != expected
                           for field, expected in required_binding.items())
                    or not _valid_sha256(
                        binding.get("eligibility_evidence_sha256"))):
                _reference_abort(
                    "RFQ eligibility binding is invalid for %s" % logical)

    for obj in research_candidates:
        logical = obj["logical_source_key"]
        is_rfq = _is_rfq_receipt_object(obj)
        if is_rfq:
            if not obj["key"].startswith(canonical_prefix + "/raw/"):
                _reference_abort("RFQ candidate leaves canonical raw scope: %s"
                                 % logical)
        else:
            if not (obj["key"].startswith(canonical_prefix + "/warehouse/")
                    or obj["key"].startswith(
                        canonical_prefix + "/control/")):
                _reference_abort(
                    "research candidate leaves warehouse/control scope: %s" %
                    logical)
        if verify_candidates:
            _require_exact_head(reader, obj,
                                "research candidate %s" % logical)
    receipt_binding = {
        "bucket": receipt_bucket,
        "key": receipt_key,
        "version_id": receipt_version_id,
        "size": receipt_size,
        "sha256": receipt_digest,
    }
    return receipt, receipt_binding


def _stage_reference_inventory(stage_objects, stage_dir, date):
    """Map the existing v2 local freeze onto canonical logical keys.

    The complete live warehouse manifest is a v2 transport artifact.
    Reference releases instead bind the canonical date-only manifest plus
    both affirmative gap controls: the receipt and, when present, its exact
    date-only capture-gaps projection.
    """
    by_logical = {}

    def add(logical, size, digest):
        logical = _safe_reference_rel(logical, "staged logical key")
        if logical in by_logical:
            _reference_abort("duplicate staged logical key %s" % logical)
        by_logical[logical] = {"size": size, "sha256": digest}

    for obj in stage_objects:
        key = obj["key"]
        logical = None
        if key == "warehouse_manifest/manifest.csv":
            continue
        if key.startswith("facts/"):
            logical = "warehouse/" + key
        elif key == "seal/date=%s.json" % date:
            logical = "warehouse/seals/date=%s.json" % date
        elif key.startswith("dim/") or key.startswith("catalog/"):
            logical = "warehouse/" + key
        elif key == "corrections/ledger_day.ndjson":
            logical = ("warehouse/corrections/date=%s/ledger_day.ndjson" %
                       date)
        elif key.startswith("corrections/"):
            logical = "warehouse/" + key
        elif key == "quality/gap_receipt_%s.json" % date:
            logical = ("control/quality/v1/date=%s/"
                       "capture_gap_receipt.json") % date
        elif key == "quality/capture_gaps_%s.csv" % date:
            logical = "control/quality/v1/date=%s/capture_gaps.csv" % date
        elif key == "quality/l2_gaps.json":
            logical = "control/quality/v1/date=%s/l2_gaps.json" % date
        elif key.startswith("raw_rfq/"):
            logical = "raw/" + key[len("raw_rfq/"):]
        else:
            _reference_abort("unmapped staged object %s" % key)
        add(logical, obj["size"], obj["sha256"])

    manifest_path = os.path.join(stage_dir, "warehouse_manifest",
                                 "manifest.csv")
    try:
        with open(manifest_path, "rb") as f:
            full_manifest = f.read()
        import canonical_receipts as cr
        day_manifest, _semantic_digest, _rows = cr._manifest_day_csv(
            full_manifest, date)
    except Exception as exc:
        _reference_abort("cannot freeze date-only manifest projection: %s" % exc)
    add("warehouse/manifest.csv", len(day_manifest),
        hashlib.sha256(day_manifest).hexdigest())
    return by_logical


def _receipt_reference_objects(receipt, include_rfq, seal, evidence_tier,
                               seal_sha):
    """Select eligible references and translate them to the frozen v3 schema."""
    seal_bucket = receipt["seal"]["bucket"]
    seal_suffix = "warehouse/seals/date=%s.json" % receipt["date"]
    seal_key = receipt["seal"]["key"]
    if not seal_key.endswith(seal_suffix):
        _reference_abort("canonical seal key is outside the fixed warehouse path")
    canonical_prefix = seal_key[:-len(seal_suffix)].rstrip("/")
    if not canonical_prefix:
        _reference_abort("canonical prefix cannot be empty")
    sealed_rfq = {row["file"]: row for row in seal.get("raw_files", [])
                  if _RFQ_RE.match(os.path.basename(row.get("file", "")))}
    selected = []
    for obj in receipt["objects"]:
        if obj.get("research_candidate") is not True:
            continue
        is_rfq = _is_rfq_receipt_object(obj)
        if is_rfq and not include_rfq:
            continue
        key = obj["key"]
        if obj["bucket"] != seal_bucket:
            _reference_abort("research reference crosses canonical buckets: %s"
                             % key)
        manifest_logical = obj["logical_source_key"]
        receipt_logical = manifest_logical
        if is_rfq:
            prefix = canonical_prefix + "/raw/"
            if not key.startswith(prefix):
                _reference_abort("RFQ reference is outside canonical raw: %s" % key)
            rel = key[len(prefix):]
            _safe_reference_rel(rel, "RFQ seal-relative path")
            match = re.match(r"^date=(\d{4}-\d{2}-\d{2})/([^/]+)$", rel)
            if match is None:
                _reference_abort("RFQ reference path is not date/basename: %s"
                                 % rel)
            try:
                datetime.date.fromisoformat(match.group(1))
            except ValueError:
                _reference_abort("RFQ reference carries an invalid date: %s"
                                 % rel)
            if (_RFQ_RE.match(match.group(2)) is None
                    or rel not in sealed_rfq):
                _reference_abort("RFQ reference is not named by the seal: %s" % rel)
            proof = sealed_rfq[rel]
            if (proof.get("size") != obj["size"]
                    or proof.get("sha256") != obj["sha256"]):
                _reference_abort("RFQ receipt differs from sealed bytes: %s" % rel)
            if (obj.get("source_kind") not in
                    ("raw_rfq", "raw_rfq_receipts")
                    or obj.get("logical_source_key") != "raw/" + rel
                    or obj.get("channel") != "rfq"
                    or obj.get("date") != match.group(1)
                    or obj.get("required") is not False
                    or obj.get("seal_binding") != seal_sha
                    or obj.get("evidence_binding") in (None, "", [], {})
                    or evidence_tier != "SEALED_CONFIRMATION"):
                _reference_abort("RFQ reference is not sealed-only eligible: %s" % rel)
            state_text = json.dumps(obj, sort_keys=True).upper()
            if any(marker in state_text for marker in (
                    "DATA_INTEGRITY_BLOCKED", "QUARANTINED",
                    "QUARANTINE_ACTIVE")):
                _reference_abort("RFQ reference is blocked/quarantined: %s" % rel)
            manifest_logical = "raw_rfq/%s" % rel
            kind = "rfq"
        else:
            if key.startswith(canonical_prefix + "/raw/"):
                _reference_abort("non-RFQ raw cannot be a research reference: %s"
                                 % key)
            kind = obj.get("source_kind")
            channel = obj.get("channel")
            if obj.get("date") != receipt["date"]:
                _reference_abort("reference date differs from release date: %s"
                                 % manifest_logical)
            if obj.get("required") is not True:
                _reference_abort("non-RFQ reference is not required: %s" %
                                 manifest_logical)
            direct_key = canonical_prefix + "/" + manifest_logical
            valid = False
            if manifest_logical.startswith("warehouse/facts/"):
                rel = manifest_logical[len("warehouse/facts/"):]
                table = rel.split("/", 1)[0]
                valid = (kind == "facts" and channel == table
                         and table in
                         ("orderbooks_l1", "orderbooks_full", "trades")
                         and ("date=%s/" % receipt["date"]) in "/" + rel
                         and key == direct_key)
            elif manifest_logical.startswith(
                    "warehouse/dim/snapshots/"):
                dim_binding_valid = (
                    (obj.get("seal_binding") is None
                     and obj.get("evidence_binding") is None)
                    or (obj.get("seal_binding") == seal_sha
                        and _is_forward_version_binding(
                            obj.get("evidence_binding"))
                        and _is_forward_dim_source_evidence(
                            obj["evidence_binding"].get("source_evidence"))))
                valid = (manifest_logical in {
                    "warehouse/dim/snapshots/date=%s/series.csv" % receipt["date"],
                    "warehouse/dim/snapshots/date=%s/events.csv" % receipt["date"],
                    "warehouse/dim/snapshots/date=%s/markets.csv" % receipt["date"],
                } and kind == "dim_snapshot" and channel in (None, "dim")
                         and key == direct_key
                         and dim_binding_valid)
            elif manifest_logical.startswith("warehouse/catalog/"):
                rel = manifest_logical[len("warehouse/catalog/"):]
                valid = (rel in {
                    "series/part-00000.parquet",
                    "events/part-00000.parquet",
                    "markets/part-00000.parquet",
                    "settlements/part-00000.parquet",
                    "series_classified/part-00000.parquet",
                } and kind == "catalog" and channel in (None, "catalog")
                         and key == direct_key)
            elif manifest_logical == (
                    "warehouse/seals/date=%s.json" % receipt["date"]):
                valid = (kind == "seal" and channel in (None, "seal")
                         and key == direct_key)
            elif manifest_logical == "warehouse/manifest.csv":
                expected = (canonical_prefix +
                            "/warehouse/publication-snapshots/v1/date=%s/"
                            "manifest/sha256=%s/manifest.csv" %
                            (receipt["date"], obj["sha256"]))
                valid = (kind == "warehouse_manifest_day"
                         and channel is None and key == expected)
            elif manifest_logical.startswith("warehouse/corrections/"):
                late = ("warehouse/corrections/date=%s/late_rows.ndjson" %
                        receipt["date"])
                ledger = ("warehouse/corrections/date=%s/ledger_day.ndjson" %
                          receipt["date"])
                if manifest_logical == late:
                    expected = (canonical_prefix +
                                "/warehouse/corrections/date=%s/"
                                "late_rows.ndjson" % receipt["date"])
                    valid = (kind == "correction" and channel is None
                             and key == expected)
                elif manifest_logical == ledger:
                    expected = (canonical_prefix +
                                "/warehouse/publication-snapshots/v1/date=%s/"
                                "corrections/ledger_day/sha256=%s/"
                                "ledger_day.ndjson" %
                                (receipt["date"], obj["sha256"]))
                    valid = (kind == "corrections_ledger_day"
                             and channel is None and key == expected)
            else:
                quality_root = ("control/quality/v1/date=%s/" %
                                receipt["date"])
                expected_quality = {
                    quality_root + "capture_gap_receipt.json":
                        ("capture_gap_receipt", "orderbooks_l1",
                         "capture_gap_receipt", "capture_gap_receipt.json"),
                    quality_root + "capture_gaps.csv":
                        ("capture_gaps_projection", "orderbooks_l1",
                         "capture_gaps", "capture_gaps.csv"),
                    quality_root + "l2_gaps.json":
                        ("l2_quality_receipt", "orderbooks_full",
                         "l2_gaps", "l2_gaps.json"),
                }
                expected = expected_quality.get(manifest_logical)
                expected_key = ((canonical_prefix + "/" + quality_root +
                                 expected[2] + "/sha256=" + obj["sha256"] +
                                 "/" + expected[3]) if expected else None)
                valid = (expected is not None and kind == expected[0]
                         and channel in (None, expected[1])
                         and key == expected_key)
            if not valid:
                _reference_abort("reference is outside the fixed canonical "
                                 "contract: %s -> %s" %
                                 (manifest_logical, key))
            if (kind != "dim_snapshot"
                    and obj.get("seal_binding") != seal_sha):
                _reference_abort("reference has a different seal binding: %s"
                                 % manifest_logical)
            if (kind != "dim_snapshot"
                    and obj.get("evidence_binding") in (None, "", [], {})):
                _reference_abort("reference has no evidence binding: %s" %
                                 manifest_logical)
        ref = {
            "logical_key": manifest_logical,
            "source_bucket": obj["bucket"],
            "source_key": key,
            "source_version_id": obj["VersionId"],
            "size": obj["size"],
            "sha256": obj["sha256"],
            "kind": kind,
            "channel": obj.get("channel"),
            "date": obj.get("date"),
            "required": obj.get("required"),
            "seal_binding": obj.get("seal_binding"),
            "evidence_binding": obj.get("evidence_binding"),
        }
        # Receipt implementations may additionally attest S3 LastModified.
        # Carry it when present, but the plan's stable six-field reference
        # identity deliberately does not depend on a timestamp.
        last_modified = (obj.get("last_modified_utc")
                         or obj.get("source_last_modified_utc"))
        if last_modified is not None:
            ref["source_last_modified_utc"] = last_modified
        selected.append((receipt_logical, ref))
    selected.sort(key=lambda pair: pair[1]["logical_key"])
    logicals = [pair[1]["logical_key"] for pair in selected]
    if len(logicals) != len(set(logicals)):
        _reference_abort("v3 reference logical keys are not unique")
    return selected


def _publish_reference_manifest(*, date, dest_url, stage_dir, stage_objects,
                                receipt_path, receipt_reader,
                                tag_precommit_proof, include_rfq,
                                prepare_only, prepare_output_root,
                                seal, seal_sha,
                                seal_size,
                                publication_components, tables, tl1_status,
                                tier, tier_basis, channels, corr_objs,
                                ledger_lines, l2_quality, live_dir):
    if include_rfq:
        _reference_abort(
            "v3 tagger-precommit publication is RFQ-off; RFQ remains an "
            "independent research capability")
    receipt, receipt_binding = _load_authoritative_receipt(
        receipt_path, date, seal_sha, seal_size, receipt_reader)
    selected = _receipt_reference_objects(
        receipt, include_rfq, seal, tier, seal_sha)
    references = [ref for _logical, ref in selected]
    expected_tagger = (CANONICAL_TAGGER_ARN
                       if dest_url.startswith("s3://") else None)
    proof_binding = None
    if not prepare_only:
        proof_binding = _validate_tag_precommit_proof(
            tag_precommit_proof, date=date, receipt=receipt,
            receipt_binding=receipt_binding, references=references,
            expected_tagger_arn=expected_tagger)
    reference_projection = [{
        key: ref[key] for key in (
            "logical_key", "source_bucket", "source_key",
            "source_version_id", "size", "sha256")
    } for ref in references]
    reference_set_sha = canonical_digest(reference_projection)
    semantics_projection = [{
        key: ref.get(key) for key in (
            "logical_key", "kind", "channel", "date", "required",
            "seal_binding", "evidence_binding")
    } for ref in references]
    object_semantics_sha = canonical_digest(semantics_projection)
    def receipt_projection(kinds):
        return [{key: ref.get(key) for key in (
            "logical_key", "source_bucket", "source_key",
            "source_version_id", "size", "sha256", "kind", "channel",
            "evidence_binding")}
                for ref in references if ref.get("kind") in kinds]

    correction_refs = receipt_projection({
        "correction", "corrections_ledger_day"})
    gap_refs = receipt_projection({
        "capture_gap_receipt", "capture_gaps_projection"})
    l2_refs = receipt_projection({"l2_quality_receipt"})
    facts_l2 = any(ref.get("kind") == "facts"
                   and ref.get("channel") == "orderbooks_full"
                   for ref in references)
    gap_affirmative = any(
        ref.get("kind") == "capture_gap_receipt" for ref in references)
    l2_affirmative = bool(l2_refs)
    tier, tier_basis = derive_evidence_tier(
        seal, gap_affirmative,
        None if gap_affirmative else "canonical gap receipt absent",
        l2_facts_present=facts_l2,
        l2_evidence_ok=l2_affirmative,
        l2_reason=(None if l2_affirmative
                   else "canonical L2 quality receipt absent"))
    publication_components = {
        "source": "IMMUTABLE_CANONICAL_RECEIPT_EXACT_VERSIONS",
        "seal_sha256": seal_sha,
        "corrections": {"objects": correction_refs},
        "gap_evidence": {
            "affirmative_receipt": gap_affirmative,
            "objects": gap_refs,
        },
        "l2_quality": {
            "affirmative_receipt": l2_affirmative,
            "objects": l2_refs,
        },
        "rfq": {
            "included": any(ref.get("kind") == "rfq"
                            for ref in references),
        },
    }
    l2_quality = publication_components["l2_quality"]
    tier_basis_sha = canonical_digest(tier_basis)
    corrections_digest = canonical_digest(correction_refs)
    gap_digest = canonical_digest(gap_refs)
    l2_digest = canonical_digest(l2_refs)
    rfq_included = any(ref["kind"] == "rfq" for ref in references)
    receipt_binding_sha = canonical_digest(receipt_binding)
    publication_state = {
        "schema": REFERENCE_MANIFEST_SCHEMA,
        "storage_mode": REFERENCE_STORAGE_MODE,
        "manifest_contract_version": REFERENCE_CONTRACT_VERSION,
        "date": date,
        "source_seal_binding_sha256": seal_sha,
        "reference_set_sha256": reference_set_sha,
        "object_semantics_sha256": object_semantics_sha,
        "evidence_tier": tier,
        "evidence_basis_sha256": tier_basis_sha,
        "corrections_digest": corrections_digest,
        "gap_evidence_digest": gap_digest,
        "l2_quality_digest": l2_digest,
        "tl1_status": tl1_status,
        "rfq_policy": "OPTIONAL_SEALED_ONLY",
        "rfq_included": rfq_included,
        "canonical_receipt_set_sha256": receipt["receipt_set_sha256"],
        "canonical_receipt_binding_sha256": receipt_binding_sha,
    }
    state_sha = canonical_digest(publication_state)
    release_id = "%s__v3ref__seal-%s__pub-%s" % (
        date, seal_sha[:8], state_sha[:16])
    rel_prefix = "releases/%s" % release_id
    dest = make_dest(dest_url)
    if isinstance(dest, S3Dest):
        dest.bind_reference_patrol(live_dir)

    expected_seal_logical = "warehouse/seals/date=%s.json" % date
    seal_refs = [ref for ref in references
                 if ref["logical_key"] == expected_seal_logical]
    if len(seal_refs) != 1:
        _reference_abort("reference set must contain one source seal")
    seal_ref = seal_refs[0]
    source_seal = {
        "bucket": seal_ref["source_bucket"],
        "key": seal_ref["source_key"],
        "version_id": seal_ref["source_version_id"],
        "size": seal_ref["size"],
        "sha256": seal_ref["sha256"],
        "verification_state": "PASS",
    }
    now = datetime.datetime.now(datetime.timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    channels_out = json.loads(json.dumps(channels))
    channels_out["orderbooks_l1"]["gap_receipt_affirmative"] = \
        gap_affirmative
    channels_out["orderbooks_l1"]["gap_receipt"] = (
        "CANONICAL_EXACT_VERSION" if gap_affirmative
        else "NO_AFFIRMATIVE_GAP_RECEIPT")
    channels_out["orderbooks_l2"]["seq_quality"] = l2_quality
    channels_out["rfq"]["status"] = (
        "INCLUDED_SEALED_REFERENCE" if rfq_included
        else "EXCLUDED_EXPLICIT_OPT_IN_REQUIRED")
    corrections = {
        "included_files": len(correction_refs),
        "ledger_day_entries": None,
        "cutoff_utc": now,
        "digest": corrections_digest,
        "note": "derived exclusively from canonical exact-version receipt; "
                "current mutable producer paths were not reconstructed",
    }
    publication_components = json.loads(json.dumps(
        publication_components, sort_keys=True))
    publisher_commit = code_commit()
    if re.fullmatch(r"[0-9a-f]{40}", publisher_commit or "") is None:
        _reference_abort(
            "v3 publication requires an exact 40-hex git commit")
    manifest = {
        "schema": REFERENCE_MANIFEST_SCHEMA,
        "schema_version": 3,
        "manifest_contract_version": REFERENCE_CONTRACT_VERSION,
        "storage_mode": REFERENCE_STORAGE_MODE,
        "release_id": release_id,
        "date": date,
        "publication_status": "PUBLISHED",
        "rfq_policy": "OPTIONAL_SEALED_ONLY",
        "rfq_included": rfq_included,
        "published_at_utc": None if prepare_only else now,
        "publisher_commit": publisher_commit,
        "canonical_receipt": {
            "schema_version": CANONICAL_RECEIPT_SCHEMA,
            "state": CANONICAL_RECEIPT_STATE,
            "authority": CANONICAL_RECEIPT_AUTHORITY,
            "authoritative": True,
            "s3_published": True,
            "prune_eligible": False,
            "receipt_set_sha256": receipt["receipt_set_sha256"],
            "receipt_object": receipt_binding,
        },
        "eligibility_enforcement": {
            "publisher_exact_tag_inspection": "NOT_AUTHORIZED_BY_DESIGN",
            "publisher_tag_mutation": "ACCESS_DENIED",
            "tagger_precommit_proof": proof_binding,
            "tagger_exact_set_readback":
                "CONTENT_ADDRESSED_PRECOMMIT_PROOF",
            "consumer_exact_tag_revalidation":
                "W09_S3_EXISTING_OBJECT_TAG_RESEARCH_ELIGIBLE_TRUE",
            "consumer_enforcement_scope":
                "EACH_CANONICAL_SOURCE_EXACT_VERSION",
        },
        "source_seal": source_seal,
        "reference_set_sha256": reference_set_sha,
        "object_semantics_sha256": object_semantics_sha,
        "publication_state": publication_state,
        "publication_state_sha256": state_sha,
        "publication_components": publication_components,
        "l2_quality": l2_quality,
        "evidence": {"tier": tier, "basis": tier_basis},
        "evidence_tier": tier,
        "evidence_tier_basis": tier_basis,
        "seal": {
            "sha256": seal_sha,
            "manifest_date_sha256": seal["manifest_date_sha256"],
            "sealed_at": seal.get("sealed_at"),
            "status": seal.get("status"),
            "archive_files": seal.get("archive_files"),
            "archive_rows": seal.get("archive_rows"),
            "capture_quality_status": seal.get("capture_quality_status"),
        },
        "tables": tables,
        "tl1_status": tl1_status,
        "channels": channels_out,
        "corrections": corrections,
        "post_upload_verification": {
            "method": "authoritative canonical receipt exact-version binding",
            "objects_verified": len(references),
            "data_objects_uploaded": 0,
        },
        "version_binding": {
            "mode": REFERENCE_STORAGE_MODE,
            "bindings_sha256": reference_set_sha,
            "versioning_requirement": "VERSIONING_REQUIRED",
        },
        "objects": references,
    }
    manifest_key = "%s/MANIFEST.json" % rel_prefix
    if prepare_only:
        if not prepare_output_root:
            _reference_abort("--prepare-output-root is required for prepare-only")
        prepared = {
            "schema_version": REFERENCE_PREPARED_PLAN_SCHEMA,
            "state": REFERENCE_PREPARED_PLAN_STATE,
            "prepared_at_utc": now,
            "date": date,
            "destination": dest_url,
            "live_dir": os.path.abspath(os.fspath(live_dir)),
            "receipt_index": os.path.abspath(os.fspath(receipt_path)),
            "include_rfq": False,
            "publisher_commit": publisher_commit,
            "release_id": release_id,
            "manifest_key": manifest_key,
            "reference_set_sha256": reference_set_sha,
            "manifest_template": manifest,
        }
        prepared_path, prepared_sha, prepared_size = \
            _write_prepared_reference_plan(prepare_output_root, prepared)
        print(json.dumps({
            "schema_version": REFERENCE_PREPARED_PLAN_SCHEMA,
            "state": REFERENCE_PREPARED_PLAN_STATE,
            "date": date,
            "release_id": release_id,
            "prepared_plan": prepared_path,
            "prepared_plan_sha256": prepared_sha,
            "prepared_plan_size": prepared_size,
            "reference_set_sha256": reference_set_sha,
            "s3_writes": 0,
            "rfq": "OFF",
        }, sort_keys=True))
        return 0
    existing = dest.read_manifest(manifest_key)
    if existing is not None:
        raw, existing_version = existing
        _assert_existing_reference_equivalent(raw, manifest)
        print("[research_release] reference state %s already published at "
              "%s/%s — exact manifest verified, no-op (data uploads=0, "
              "manifest_version=%s)" %
              (state_sha[:16], dest.describe(), rel_prefix,
               existing_version or "LOCAL_FIXTURE"))
        _reference_manifest_result(
            dest, manifest_key, raw, existing_version, release_id,
            "REFERENCE_MANIFEST_ALREADY_COMMITTED")
        return 0
    mpath = os.path.join(stage_dir, "MANIFEST.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    # This is the final authorization boundary before MANIFEST PutObject.
    # Re-read the content-addressed proof so tamper/staleness during staging
    # cannot inherit the earlier validation.
    final_proof_binding = _validate_tag_precommit_proof(
        tag_precommit_proof, date=date, receipt=receipt,
        receipt_binding=receipt_binding, references=references,
        expected_tagger_arn=expected_tagger)
    if final_proof_binding != proof_binding:
        _reference_abort("precommit proof changed before manifest commit")
    manifest_vid = dest.upload_manifest(mpath, manifest_key)
    print("[research_release] published reference %s: %d exact objects, "
          "data uploads=0, manifest_version=%s -> %s/%s" %
          (release_id, len(references), manifest_vid, dest.describe(), rel_prefix))
    with open(mpath, "rb") as handle:
        manifest_raw = handle.read(MAX_REFERENCE_MANIFEST_BYTES + 1)
    _reference_manifest_result(
        dest, manifest_key, manifest_raw, manifest_vid, release_id,
        "REFERENCE_MANIFEST_COMMITTED")
    return 0


def refuse_gate(msg):
    sys.stderr.write(
        "\n================================================================\n"
        "REFUSED: %s\n"
        "  research_release.py WRITES to the research/ S3 prefix. A real\n"
        "  s3:// publish requires the operator's explicit approval\n"
        "  (--operator-approved; on the box the supervisor passes it only\n"
        "  while the operator's arm-file ~/.kalshi/research_publish_approved\n"
        "  exists). Local directory destinations are offline fixture mode.\n"
        "================================================================\n"
        % msg)
    return 2


def publish(date, dest_url, include_rfq, quality_dir, raw_vault_url,
            live_dir, operator_approved, reference_receipt=None,
            reference_receipt_reader=None,
            reference_tag_precommit_proof=None,
            reference_yellow_alert=None, reference_prepare_only=False,
            reference_prepare_output_root=None,
            reference_prepared_plan=None):
    reference_mode = reference_receipt is not None
    require_reference_patrol_clear(
        reference_mode, reference_yellow_alert, dest_url, live_dir)
    if (reference_prepare_only or reference_prepared_plan) and not reference_mode:
        _reference_abort("reference phase controls require a receipt index")
    if reference_prepare_only and reference_prepared_plan:
        _reference_abort("prepare-only and prepared-plan are mutually exclusive")
    if reference_prepare_only and not reference_prepare_output_root:
        _reference_abort("prepare-only requires --prepare-output-root")
    if (not reference_prepare_only and reference_prepare_output_root):
        _reference_abort("prepare-output-root is valid only with prepare-only")
    if reference_prepare_only and reference_tag_precommit_proof:
        _reference_abort("prepare-only must run before a tagger proof exists")
    if (reference_mode and not reference_prepare_only
            and not reference_tag_precommit_proof):
        _reference_abort(
            "a fresh content-addressed tagger precommit proof is required")
    if (reference_mode and dest_url.startswith("s3://")
            and not reference_prepare_only and not reference_prepared_plan):
        _reference_abort(
            "real S3 v3 publication requires prepare-only -> tagger proof -> "
            "prepared-plan commit")
    # ---- remediation items 6+7: write gate + credential namespace ------------
    if dest_url.startswith("s3://"):
        if not operator_approved:
            return refuse_gate("--operator-approved flag is missing")
        if os.path.isfile(research_env_file()):
            return refuse_gate(
                "this host holds the Mac research READ-ONLY key (%s); the "
                "publisher runs only on the EC2 box with vaultWriter — the "
                "read namespace and the write namespace never share a host "
                "role" % research_env_file())

    if reference_prepared_plan:
        exact_reader = (reference_receipt_reader or
                        _make_canonical_receipt_reader())
        return _commit_prepared_reference(
            date=date, dest_url=dest_url, live_dir=live_dir,
            receipt_path=reference_receipt, receipt_reader=exact_reader,
            tag_precommit_proof=reference_tag_precommit_proof,
            prepared_plan_path=reference_prepared_plan,
            include_rfq=bool(include_rfq))

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

    stage_root = _research_stage_root()
    os.makedirs(stage_root, exist_ok=True)
    now = time.time()
    for name in os.listdir(stage_root):
        if not (re.fullmatch(r"\.pending-\d+", name)
                or re.fullmatch(
                    r"\.reference-pending-\d{4}-\d{2}-\d{2}-\d+", name)):
            continue
        stale = os.path.join(stage_root, name)
        try:
            if now - os.stat(stale).st_mtime > 24 * 60 * 60:
                shutil.rmtree(stale)
        except FileNotFoundError:
            pass
    estimated_stage_bytes = _estimated_stage_bytes(
        date, reference_mode=reference_mode, include_rfq=rfq_effective,
        seal=seal, warehouse_root=warehouse_root, archive_root=archive_root,
        quality_dir=quality_dir)
    shares_capture_disk = (
        os.stat(stage_root).st_dev == os.stat(raw_root).st_dev)
    # Local fixture destinations do not share a production capture volume.
    # Every real S3 publication on the production same-disk stage preserves a
    # full 100 GiB independently of the bytes this release expects to copy.
    capture_reserve = (CAPTURE_DISK_RESERVE_BYTES
                       if shares_capture_disk and dest_url.startswith("s3://")
                       else 0)
    required_free = estimated_stage_bytes + capture_reserve
    actual_free = shutil.disk_usage(stage_root).free
    if actual_free < required_free:
        raise SystemExit(
            "REFUSED: research stage disk budget failed "
            "(free=%d expected_copy=%d capture_reserve=%d same_disk=%s)" %
            (actual_free, estimated_stage_bytes, capture_reserve,
             str(shares_capture_disk).lower()))
    pending = os.path.join(stage_root, ".pending-%d" % os.getpid())
    shutil.rmtree(pending, ignore_errors=True)
    os.makedirs(pending, exist_ok=True)
    # the stage base starts as the digest-pending dir and is atomically
    # renamed to the release-id dir once the publication state is known
    stage = {"dir": pending}
    objects = []  # {key, size, sha256[, version_id]} — frozen from STAGED bytes

    def stage_copy(key, src):
        """Fix 2: mutable auxiliary files are COPIED first and hashed from
        the staged copy — a live writer can never invalidate the freeze.
        Keys are containment-checked (remediation item 3)."""
        dst = safe_join(stage["dir"], key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        objects.append({"key": key, "size": os.stat(dst).st_size,
                        "sha256": sha256_file(dst)})
        return objects[-1]

    def stage_link_attested(key, src, frozen):
        """Seal-attested write-once files (facts, sealed rfq raw): verified
        against the seal, then hardlinked (copy fallback)."""
        dst = safe_join(stage["dir"], key)
        if not reference_mode:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
        objects.append({"key": key, "size": frozen[1], "sha256": frozen[0]})
        return src if reference_mode else dst

    def stage_bytes(key, payload):
        dst = safe_join(stage["dir"], key)
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
        if not reference_mode and os.path.isdir(corr_dir):
            for base, _d, files in os.walk(corr_dir):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    key = "corrections/date=%s/%s" % (
                        date, os.path.relpath(src, corr_dir))
                    corr_objs.append(stage_copy(key, src))
        ledger_lines = ([] if reference_mode
                        else day_ledger_lines(warehouse_root, date))
        ledger_obj = None
        if ledger_lines:
            ledger_obj = stage_bytes(
                "corrections/ledger_day.ndjson",
                ("\n".join(ledger_lines) + "\n").encode("utf-8"))

        # P0-3: ABSENCE IS NOT EVIDENCE. The ONLY affirmative gap evidence
        # is the scanner's own per-date receipt
        # (capture_gap_receipt_<date>.json, written by tools/capture_gaps.py)
        # binding date + exact scanned raw inventory + result — and that
        # inventory must reproduce the day seal's firehose raw inventory
        # byte-for-byte. Done-markers and bare/header-only CSVs prove
        # nothing (they are shipped only as auxiliary record snapshots).
        gap_record = os.path.join(quality_dir, "capture_gaps.csv")
        gaps_csv = (None if reference_mode
                    else day_gap_intervals(gap_record, date))
        gap_obj = None
        if gaps_csv is not None:
            buf = ["start_us,end_us"]
            buf += ["%d,%d" % (g["start_us"], g["end_us"])
                    for g in gaps_csv]
            gap_obj = stage_bytes("quality/capture_gaps_%s.csv" % date,
                                  ("\n".join(buf) + "\n").encode("utf-8"))
        receipt_src = os.path.join(quality_dir,
                                   "capture_gap_receipt_%s.json" % date)
        gap_receipt_obj = None
        gap_reason = None
        receipt_gaps = None
        if reference_mode:
            gap_reason = "derived later from immutable canonical receipt"
        elif not os.path.isfile(receipt_src):
            gap_reason = ("no per-date scan receipt "
                          "(capture_gap_receipt_%s.json)" % date)
        else:
            gap_receipt_obj = stage_copy(
                "quality/gap_receipt_%s.json" % date, receipt_src)
            with open(os.path.join(stage["dir"], "quality",
                                   "gap_receipt_%s.json" % date)) as f:
                receipt = json.load(f)
            if (receipt.get("schema_version")
                    != "capture-gap-scan-receipt-v1"
                    or receipt.get("date") != date):
                gap_reason = "scan receipt does not bind this date"
            else:
                want = {r["file"]: r["size"]
                        for r in seal.get("raw_files", [])
                        if r["file"].startswith("date=%s/" % date)
                        and os.path.basename(r["file"])
                        .startswith("firehose_")}
                got = {f["file"]: f["bytes"]
                       for f in receipt.get("files", [])}
                # P0-3 residual fix 1: EXACT bidirectional binding — the
                # scanned inventory must equal the sealed firehose subset:
                # identical paths and byte sizes, no missing OR extra files.
                if got != want:
                    missing = sorted(set(want) - set(got))
                    extra = sorted(set(got) - set(want))
                    sized = sorted(k for k in set(want) & set(got)
                                   if want[k] != got[k])
                    gap_reason = ("scan receipt inventory does not "
                                  "reproduce the sealed firehose raw "
                                  "inventory EXACTLY (stale/partial: "
                                  "missing=%s extra=%s byte-size=%s)"
                                  % (missing[:3], extra[:3], sized[:3]))
                elif receipt.get("unreadable"):
                    gap_reason = "scan receipt marks the day unreadable"
                else:
                    receipt_gaps = receipt.get("gaps", [])
        gap_affirmative = gap_reason is None and receipt_gaps is not None
        if not gap_affirmative:
            print("WARNING [research_release]: no AFFIRMATIVE gap-scan "
                  "receipt for %s (%s) — publishing with a DEGRADED "
                  "evidence tier; absence of a gap file is not evidence "
                  "(P0-3)" % (date, gap_reason))
        # P0-3 residual fix 2: L2 gap evidence is a POSITIVE per-date
        # receipt — schema/date binding, the EXACT scanned L2 inventory
        # with byte sizes (bidirectionally equal to the seal's l2 raw
        # subset), and the scan/seq statistics. Date-only, header-only,
        # stale, missing-file or extra-file receipts are ALL invalid.
        l2_gaps_src = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
        l2_gap_obj = None
        l2_quality = None
        l2_reason = "no per-date L2 receipt (l2_gaps_%s.json)" % date
        if not reference_mode and os.path.isfile(l2_gaps_src):
            l2_gap_obj = stage_copy("quality/l2_gaps.json", l2_gaps_src)
            with open(os.path.join(stage["dir"],
                                   "quality/l2_gaps.json")) as f:
                lg = json.load(f)
            l2_quality, l2_reason = validate_l2_receipt(lg, seal, date)
        if l2_quality is None:
            print("WARNING [research_release]: no VALID per-date L2 "
                  "receipt for %s (%s)" % (date, l2_reason))

        publication_state = {
            "seal_sha256": seal_sha,
            "corrections": {
                "files": [{"key": o["key"], "size": o["size"],
                           "sha256": o["sha256"]} for o in corr_objs],
                "ledger_day_sha256":
                    ledger_obj["sha256"] if ledger_obj else None,
            },
            "gap_evidence": {
                "affirmative_receipt": gap_affirmative,
                "receipt_inventory_matched_seal": gap_affirmative,
                "non_affirmative_reason": gap_reason,
                "gap_receipt_sha256":
                    gap_receipt_obj["sha256"] if gap_receipt_obj else None,
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
        if reference_mode:
            # v3 identity also binds the complete reference/object semantics
            # and is therefore computed only after the full local freeze.
            release_id = ".reference-pending-%s-%d" % (date, os.getpid())
            rel_prefix = None
            dest = None
        else:
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
        if not reference_mode:
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
            sampled = stage_link_attested(
                "facts/%s" % entry["file"], src, frozen)
            table_sample.setdefault(entry["table"], sampled)

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
        if not reference_mode and os.path.isdir(dim_snap):
            for base, _d, files in os.walk(dim_snap):
                for fn in sorted(files):
                    src = os.path.join(base, fn)
                    stage_copy("dim/snapshots/date=%s/%s"
                               % (date, os.path.relpath(src, dim_snap)), src)
        catalog_dir = os.path.join(warehouse_root, "catalog")
        if not reference_mode and os.path.isdir(catalog_dir):
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
                         "source_version_id": None,
                         "version_pinning":
                             "LOCAL_SEAL_ATTESTED_NO_VAULT_NEEDED"})
                else:
                    dst = safe_join(stage_dir, key)
                    prov = vault.fetch_exact(rel, dst, entry["sha256"],
                                             entry["size"])
                    if prov is None:
                        raise SystemExit(
                            "ABORT (fail-closed): sealed rfq file %s is "
                            "pruned locally AND no vault object version "
                            "reproduces the sealed sha256 (%s)"
                            % (rel, raw_vault_url))
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

        # ---- evidence tier: DERIVED (fix 5 + P0-3) -----------------------------
        l2_facts_present = "orderbooks_full" in tables
        tier, tier_basis = derive_evidence_tier(
            seal, gap_affirmative, gap_reason,
            l2_facts_present=l2_facts_present,
            l2_evidence_ok=l2_quality is not None,
            l2_reason=l2_reason)

        # ---- channel completeness labels (amendment 4 + fix 4) -----------------
        l2_included = l2_facts_present
        channels = {
            "orderbooks_l1": {
                "status": ("INCLUDED" if "orderbooks_l1" in tables
                           else "ABSENT_FROM_THIS_RELEASE"),
                "completeness": "CONFLATED_CHANGE_STREAM_NEVER_LOSSLESS",
                "gap_receipt": ("quality/gap_receipt_%s.json" % date
                                if gap_affirmative
                                else "NO_AFFIRMATIVE_GAP_RECEIPT"),
                "gap_receipt_affirmative": gap_affirmative,
                "gap_evidence": ("quality/capture_gaps_%s.csv" % date
                                 if gaps_csv is not None
                                 else "ABSENT_NO_CAPTURE_GAP_RECORD"),
                "gap_intervals_for_date":
                    len(receipt_gaps) if gap_affirmative else None,
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

        if reference_mode:
            exact_reader = (reference_receipt_reader or
                            _make_canonical_receipt_reader())
            return _publish_reference_manifest(
                date=date, dest_url=dest_url, stage_dir=stage_dir,
                stage_objects=objects, receipt_path=reference_receipt,
                receipt_reader=exact_reader,
                tag_precommit_proof=reference_tag_precommit_proof,
                prepare_only=bool(reference_prepare_only),
                prepare_output_root=reference_prepare_output_root,
                include_rfq=bool(include_rfq), seal=seal, seal_sha=seal_sha,
                seal_size=len(seal_bytes),
                publication_components=publication_state, tables=tables,
                tl1_status=tl1_status, tier=tier, tier_basis=tier_basis,
                channels=channels, corr_objs=corr_objs,
                ledger_lines=ledger_lines, l2_quality=l2_quality,
                live_dir=live_dir)

        # ---- upload: data first ------------------------------------------------
        dest.upload_tree(stage_dir, rel_prefix)

        # ---- fix 3 + P0-1: verify EVERY actual destination object by its
        # EXACT VersionId (HEAD -> VersionId A -> GET exactly A -> size +
        # sha256 -> record A) BEFORE the manifest exists. Real S3 without
        # VersionIds is fail-closed NON-PUBLISHABLE (P0-2:
        # VERSIONING_REQUIRED — vaultWriter cannot even query the bucket's
        # versioning status, s3:GetBucketVersioning is AccessDenied, so
        # object-level VersionIds are the only observable proof) -----------------
        for o in objects:
            o["version_id"] = dest.verify_object(
                "%s/%s" % (rel_prefix, o["key"]), o["size"], o["sha256"])
        vids = [o["version_id"] for o in objects]
        binding_mode = dest.binding_mode
        if binding_mode == "VERSION_BOUND" and any(v is None for v in vids):
            raise SystemExit("ABORT (fail-closed): VERSION_BOUND "
                             "destination yielded a null VersionId")
        if binding_mode != "VERSION_BOUND":
            print("NOTE [research_release]: fixture destination has no "
                  "object versions (%s) — offline FIXTURE mode only; a "
                  "real S3 publication FAILS CLOSED without VersionIds "
                  "(P0-1/P0-2 VERSIONING_REQUIRED)" % binding_mode)
        bindings_sha = canonical_digest(
            {o["key"]: o["version_id"] for o in objects})

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
            "version_binding": {
                "mode": binding_mode,
                "bindings_sha256": bindings_sha,
                "versioning_requirement": "VERSIONING_REQUIRED",
                "note": ("publication is bound to the recorded per-object "
                         "VersionIds (verified by exact-version GET); "
                         "consumers MUST fetch those exact versions"
                         if binding_mode == "VERSION_BOUND" else
                         "OFFLINE FIXTURE destination (no object versions "
                         "exist on a filesystem); NEVER a real "
                         "publication — real S3 publishing FAILS CLOSED "
                         "without VersionIds (P0-2: bucket versioning "
                         "status is unobservable to vaultWriter, "
                         "s3:GetBucketVersioning AccessDenied; "
                         "VERSIONING_REQUIRED pending operator console "
                         "confirmation)"),
            },
            "objects": objects,
        }
        mpath = os.path.join(stage_dir, "MANIFEST.json")
        with open(mpath, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        # P0-1: write-once conditional create, read back by exact VersionId
        manifest_vid = dest.upload_manifest(mpath,
                                            "%s/MANIFEST.json" % rel_prefix)
        total = sum(o["size"] for o in objects)
        print("[research_release] published %s: %d objects, %.2f MB, "
              "tier=%s, tl1=%s, l2=%s, rfq=%s, binding=%s, "
              "manifest_version=%s, verified=%d -> %s/%s"
              % (release_id, len(objects), total / 1e6, tier, tl1_status,
                 channels["orderbooks_l2"]["status"],
                 channels["rfq"]["status"], binding_mode, manifest_vid,
                 len(objects), dest.describe(), rel_prefix))
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
                   default=os.path.join(wc.ROOT, "work", "live"),
                   help="location of the per-day completed-scan markers "
                        "(gaps_<date>.done) backing the affirmative gap "
                        "receipts (remediation item 4)")
    p.add_argument("--raw-vault",
                   default=os.environ.get("RESEARCH_RAW_VAULT",
                                          RAW_VAULT_DEFAULT),
                   help="raw vault source for exact-version rfq "
                        "reconstruction after local pruning (fix 6 + "
                        "remediation item 2)")
    p.add_argument("--operator-approved", action="store_true",
                   help="operator's explicit approval for a REAL s3:// "
                        "publish (remediation item 7; local fixture "
                        "destinations do not need it)")
    ref = sub.add_parser(
        "publish-reference",
        help="publish a manifest-only v3 canonical-reference release")
    ref.add_argument("--date", required=True)
    ref.add_argument("--receipt", required=True,
                     help="local DURABLE receipt index whose receipt_object "
                          "is exact-version read back before publication")
    ref.add_argument(
        "--tag-precommit-proof",
        help="fresh content-addressed verify-only proof from the dedicated "
             "tagger identity")
    phase = ref.add_mutually_exclusive_group()
    phase.add_argument(
        "--prepare-only", action="store_true",
        help="run the expensive immutable freeze and emit a content-addressed "
             "prepared plan without writing MANIFEST")
    phase.add_argument(
        "--prepared-plan",
        help="commit one content-addressed prepared plan after a fresh tagger "
             "proof; skips all expensive fact/schema rebuilding")
    ref.add_argument(
        "--prepare-output-root",
        help="absolute local directory for content-addressed prepared plans")
    ref.add_argument("--dest", default=os.environ.get("RESEARCH_DEST",
                                                      DEST_DEFAULT))
    rg = ref.add_mutually_exclusive_group()
    rg.add_argument("--include-rfq", action="store_true", default=False,
                    help="explicitly include eligible sealed RFQ references")
    rg.add_argument("--no-rfq", dest="include_rfq", action="store_false",
                    help="exclude RFQ references (the default)")
    ref.add_argument("--quality-dir",
                     default=os.path.join(wc.ROOT, "work", "event_packs"))
    ref.add_argument("--live-dir",
                     default=os.path.join(wc.ROOT, "work", "live"))
    ref.add_argument("--raw-vault",
                     default=os.environ.get("RESEARCH_RAW_VAULT",
                                            RAW_VAULT_DEFAULT))
    ref.add_argument("--operator-approved", action="store_true",
                     help="operator approval for the MANIFEST-only S3 write")
    args = ap.parse_args(argv[1:])
    include_rfq = ((rfq_switch_enabled() if args.include_rfq is None
                    else args.include_rfq) if args.cmd == "publish"
                   else bool(args.include_rfq))
    return publish(args.date, args.dest, include_rfq, args.quality_dir,
                   args.raw_vault, args.live_dir, args.operator_approved,
                   reference_receipt=(args.receipt
                                      if args.cmd == "publish-reference"
                                      else None),
                   reference_tag_precommit_proof=(
                       args.tag_precommit_proof
                       if args.cmd == "publish-reference" else None),
                   reference_prepare_only=(
                       bool(args.prepare_only)
                       if args.cmd == "publish-reference" else False),
                   reference_prepare_output_root=(
                       args.prepare_output_root
                       if args.cmd == "publish-reference" else None),
                   reference_prepared_plan=(
                       args.prepared_plan
                       if args.cmd == "publish-reference" else None))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
