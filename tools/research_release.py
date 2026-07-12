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


def research_env_file():
    """The Mac research read-only key file. KALSHI_RESEARCH_ENV_FILE
    overrides for tests only."""
    return os.environ.get("KALSHI_RESEARCH_ENV_FILE") or \
        os.path.expanduser("~/.kalshi/research_s3.env.sh")


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
        tmp = os.path.join(wc.ROOT, "work", "research_stage",
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


def derive_evidence_tier(seal, gap_receipt_affirmative, gap_reason=None,
                         l2_facts_present=False, l2_evidence_ok=True):
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
        reasons.append("orderbooks_full facts present but no matching L2 "
                       "seq-quality evidence for this date (mandatory, "
                       "P0-3)")
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
            live_dir, operator_approved):
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
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        objects.append({"key": key, "size": frozen[1], "sha256": frozen[0]})
        return objects[-1]

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

        # P0-3: ABSENCE IS NOT EVIDENCE. The ONLY affirmative gap evidence
        # is the scanner's own per-date receipt
        # (capture_gap_receipt_<date>.json, written by tools/capture_gaps.py)
        # binding date + exact scanned raw inventory + result — and that
        # inventory must reproduce the day seal's firehose raw inventory
        # byte-for-byte. Done-markers and bare/header-only CSVs prove
        # nothing (they are shipped only as auxiliary record snapshots).
        gap_record = os.path.join(quality_dir, "capture_gaps.csv")
        gaps_csv = day_gap_intervals(gap_record, date)
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
        if not os.path.isfile(receipt_src):
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
                missing = sorted(k for k, v in want.items()
                                 if got.get(k) != v)
                if missing:
                    gap_reason = ("scan receipt inventory does not "
                                  "reproduce the sealed firehose raw "
                                  "inventory (stale/partial: %s)"
                                  % ", ".join(missing[:3]))
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
        l2_gaps_src = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
        l2_gap_obj = None
        l2_quality = None
        if os.path.isfile(l2_gaps_src):
            l2_gap_obj = stage_copy("quality/l2_gaps.json", l2_gaps_src)
            with open(os.path.join(stage["dir"],
                                   "quality/l2_gaps.json")) as f:
                lg = json.load(f)
            if lg.get("date") == date:
                l2_quality = {k: lg.get(k) for k in
                              ("no_l2_files", "seq_gap_events",
                               "seq_missed_total", "sids_total",
                               "sids_with_seq_gaps", "lines")}
            else:
                print("WARNING [research_release]: l2_gaps_%s.json does "
                      "not bind this date — treated as ABSENT L2 evidence"
                      % date)

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
            l2_evidence_ok=l2_quality is not None)

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
    args = ap.parse_args(argv[1:])
    include_rfq = (rfq_switch_enabled() if args.include_rfq is None
                   else args.include_rfq)
    return publish(args.date, args.dest, include_rfq, args.quality_dir,
                   args.raw_vault, args.live_dir, args.operator_approved)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
