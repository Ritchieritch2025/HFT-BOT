#!/usr/bin/env python3
"""Research data CLI — inventory | fetch | verify | view.

Reads immutable release manifests from the dedicated research/ S3 prefix.
Historical v2 releases name copied objects below that prefix; zero-copy v3
releases contain only exact-version references to the canonical warehouse.
Both are published by tools/research_release.py as
releases/<release_id>/MANIFEST.json LAST.
Release-id addressing only; a release without its MANIFEST.json is treated as
unpublished (torn) and is never exposed. release_id embeds the full
publication-state digest (<date>__seal-<8>__pub-<16>): a later correction,
new gap evidence or a flipped rfq switch is a DISTINCT release. Object
fetches request the exact VersionId recorded by the publisher.  V3 refuses a
missing/null VersionId and verifies every cached object by size and SHA-256.

    python3 tools/research_data.py inventory
    python3 tools/research_data.py fetch  --release <release_id> [--with-rfq]
    python3 tools/research_data.py verify --release <release_id>
    python3 tools/research_data.py view

No aws CLI and no boto3 required: S3 access is stdlib SigV4 (ListObjectsV2 +
exact-version GetObject), read-only by construction. On the Mac, credentials
come from
    ~/.kalshi/research_s3.env.sh          (chmod 600; values never printed)
containing exactly:
    export AWS_ACCESS_KEY_ID=...          # the W05 research read-only key
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_DEFAULT_REGION=us-east-2   # optional (default us-east-2)
    export KALSHI_RESEARCH_S3_ROOT=...    # optional root override
The matching least-privilege IAM policy for the operator console is
docs/plan_releases/pipeline/W05_RESEARCH_READONLY_IAM_POLICY.json.
W09 uses the instance-profile-only wrapper in deploy/w09 instead of a static
key.  The root may also be a local directory (fixture tests / offline use).

verify (a failed release is NOT exposed — loud warning, exit 2):
  * every manifest object present in the local cache with exact byte size +
    sha256 (raw_rfq/* may be honestly NOT_FETCHED — it is an operator-switch,
    cost-bearing extra; see research_release.py);
  * the cached day seal's sha256 equals the manifest's frozen seal digest and
    the seal is a well-formed status=SEALED version-2 seal for the date;
  * warehouse manifest date rows reproduce the seal's manifest_date_sha256;
  * per-table schema re-DESCRIBEd (duckdb, explicit memory_limit on every
    connection) equals the frozen schema; TL1-ladder / ws_sid / ws_seq
    presence re-recorded;
  * capture-gap interval evidence re-counted against the manifest.
Success writes .VERIFIED.json into the cached release and refreshes the
merged warehouse-shaped `view/` (symlinks, VERIFIED releases only) that the
Event Intelligence dashboard consumes via --data-root.

Channel truth carried on every verify/inventory output:
L1 = conflated change stream, never lossless (gap intervals alongside);
trades = trade-id identity, duplicates collapse downstream; L2 = full-book
``orderbooks_full`` facts in eligible v3 releases, with the L2 quality receipt
verified alongside them (historical v2 releases retain their original scope);
RFQ = default OFF and only readable when separately sealed, evidenced and
dual-tagged.

Local caches under work/research_cache/ are prunable; S3, seals and
production are never mutated (this tool has no write implementation).
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402
import research_reference as ref  # noqa: E402

ROOT_DEFAULT = "s3://kalshi-vault-ritcardo/research"


def env_file_path():
    """The research read-only key file. KALSHI_RESEARCH_ENV_FILE overrides
    for tests only."""
    return os.environ.get("KALSHI_RESEARCH_ENV_FILE") or \
        os.path.expanduser("~/.kalshi/research_s3.env.sh")


POLICY_DOC = ("docs/plan_releases/pipeline/"
              "W05_RESEARCH_READONLY_IAM_POLICY.json")
CACHE_DEFAULT = os.path.join(wc.ROOT, "work", "research_cache")
DUCKDB_MEMORY_LIMIT = "8GB"  # spec HYGIENE: explicit on every connection
LADDER_COLUMNS = ("exchange_ts_us", "recv_wall_ns", "recv_mono_ns",
                  "local_recv_ts_us")
# us-east-2 S3 standard pricing (2026-07 rate card; estimate only)
PRICE_STORAGE_GB_MO = 0.023
PRICE_GET_PER_1K = 0.0004
PRICE_EGRESS_GB = 0.09
# new form: <date>__seal-<8>__pub-<16>; legacy pre-correction-order forms
# (<date>__seal-<12>[-rfq]) stay recognized — an already-published release is
# never touched, only read.
_RELEASE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})__seal-[0-9a-f]{8}__pub-[0-9a-f]{16}$"
    r"|^(\d{4}-\d{2}-\d{2})__seal-[0-9a-f]{12}(-rfq)?$"
    r"|^(\d{4}-\d{2}-\d{2})__v3ref__seal-[0-9a-f]{8}"
    r"__pub-[0-9a-f]{16}$")


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


# ---------------------------------------------------------------------------
# Credentials (never printed) + stores
# ---------------------------------------------------------------------------

def load_env_file(path=None):
    """Parses the research key file. Remediation item 6: the file MUST be
    0600 — group/other-readable credential files are refused outright."""
    path = path or env_file_path()
    out = {}
    if not os.path.isfile(path):
        return out
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(
            "REFUSED (credential hygiene): %s has mode %o — the research "
            "key file must be 0600 (chmod 600 '%s'). Values were not read."
            % (path, mode, path))
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.split("#", 1)[0].strip()
            if s.startswith("export "):
                s = s[len("export "):]
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def load_creds():
    env = load_env_file()
    def get(name):
        return os.environ.get(name) or env.get(name)
    key_id, secret = get("AWS_ACCESS_KEY_ID"), get("AWS_SECRET_ACCESS_KEY")
    region = get("AWS_DEFAULT_REGION") or "us-east-2"
    if not key_id or not secret:
        raise SystemExit(
            "IAM_REQUIRED: no research S3 credential found.\n"
            "Create %s (chmod 600) with:\n"
            "  export AWS_ACCESS_KEY_ID=...\n"
            "  export AWS_SECRET_ACCESS_KEY=...\n"
            "  export AWS_DEFAULT_REGION=us-east-2\n"
            "using an IAM user restricted by %s (List+Get on the research/ "
            "prefix only). Values are never printed by this tool." %
            (env_file_path(), POLICY_DOC))
    return key_id, secret, region


def refuse_production_credentials():
    """Remediation item 6: the research CLI runs in the READ-ONLY research
    namespace only. Kalshi production credentials in its environment mean
    the wrong credential mode — refuse before doing anything."""
    present = [k for k in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH")
               if os.environ.get(k)]
    if present:
        raise SystemExit(
            "REFUSED (credential mode): Kalshi PRODUCTION credentials are "
            "present in this environment (%s). tools/research_data.py is "
            "the read-only research CLI and must never run with production "
            "credentials — use a clean shell. Nothing was read or fetched."
            % ", ".join(present))


# ---------------------------------------------------------------------------
# Cache containment (remediation item 3)
# ---------------------------------------------------------------------------

def safe_cache_path(base, key):
    """A manifest-derived key may NEVER escape the cache: absolute paths,
    backslashes and any '.'/'..' segment are refused, and the resolved path
    must stay inside the resolved base."""
    if not key or key.startswith(("/", "\\")) or "\\" in key:
        raise SystemExit("REFUSED (cache containment): illegal manifest "
                         "key %r" % key)
    parts = key.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise SystemExit("REFUSED (cache containment): traversal manifest "
                         "key %r" % key)
    base_real = os.path.realpath(base)
    dst = os.path.realpath(os.path.join(base_real, *parts))
    if dst != base_real and not dst.startswith(base_real + os.sep):
        raise SystemExit("REFUSED (cache containment): %r escapes the "
                         "cache root %s" % (key, base))
    return os.path.join(base_real, *parts)


def safe_release_object_path(base, key):
    """Contain a release entry while allowing its final leaf to be a symlink.

    ``safe_cache_path`` intentionally resolves the whole path and is right for
    regular cache files.  Reference releases use symlinks as their immutable
    logical leaves, so here only the parent is resolved; it must still remain
    inside the release directory.
    """
    if not key or key.startswith(("/", "\\")) or "\\" in key:
        raise SystemExit("REFUSED (cache containment): illegal manifest key %r"
                         % key)
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SystemExit("REFUSED (cache containment): traversal manifest key %r"
                         % key)
    base_real = os.path.realpath(base)
    candidate = os.path.join(base_real, *parts)
    parent_real = os.path.realpath(os.path.dirname(candidate))
    if parent_real != base_real and not parent_real.startswith(base_real + os.sep):
        raise SystemExit("REFUSED (cache containment): %r escapes the release %s"
                         % (key, base))
    return candidate


def contained_remove(path, cache_root, is_dir=False):
    """Every delete this tool performs is physically confined to the cache
    root — a delete outside it is refused, never executed."""
    real, root = os.path.realpath(path), os.path.realpath(cache_root)
    if real != root and not real.startswith(root + os.sep):
        raise SystemExit("REFUSED (cache containment): delete of %s is "
                         "outside the cache root %s" % (path, cache_root))
    if is_dir:
        shutil.rmtree(real, ignore_errors=True)
    elif os.path.isfile(real):
        os.remove(real)


def validate_manifest_keys(manifest, cache_root, rid):
    """Adversarial-manifest guard: every object key must be containment-safe
    BEFORE any filesystem operation uses it."""
    rdir = cache_release_dir(cache_root, rid)
    os.makedirs(rdir, exist_ok=True)
    for o in manifest.get("objects", []):
        safe_cache_path(rdir, o["key"])


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class S3Store:
    """Read-only SigV4 S3 client (ListObjectsV2 + GetObject), stdlib only."""

    def __init__(self, bucket, prefix, region, key_id, secret):
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.region, self.key_id, self.secret = region, key_id, secret
        self.host = "%s.s3.%s.amazonaws.com" % (bucket, region)

    def _signed_request(self, key="", query=None):
        query = dict(query or {})
        amzdate = datetime.datetime.now(datetime.timezone.utc)\
            .strftime("%Y%m%dT%H%M%SZ")
        datestamp = amzdate[:8]
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_uri = "/" + urllib.parse.quote(key, safe="/-_.~")
        canonical_qs = "&".join(
            "%s=%s" % (urllib.parse.quote(k, safe="-_.~"),
                       urllib.parse.quote(v, safe="-_.~"))
            for k, v in sorted(query.items()))
        headers = {"host": self.host,
                   "x-amz-content-sha256": payload_hash,
                   "x-amz-date": amzdate}
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join("%s:%s\n" % (k, headers[k])
                                    for k in sorted(headers))
        creq = "\n".join(["GET", canonical_uri, canonical_qs,
                          canonical_headers, signed_headers, payload_hash])
        scope = "%s/%s/s3/aws4_request" % (datestamp, self.region)
        sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                         hashlib.sha256(creq.encode()).hexdigest()])
        k = _hmac(("AWS4" + self.secret).encode(), datestamp)
        for part in (self.region, "s3", "aws4_request"):
            k = _hmac(k, part)
        signature = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, "
            "Signature=%s" % (self.key_id, scope, signed_headers, signature))
        url = "https://%s%s" % (self.host, canonical_uri)
        if canonical_qs:
            url += "?" + canonical_qs
        req = urllib.request.Request(url)
        for hk, hv in headers.items():
            if hk != "host":
                req.add_header(hk, hv)
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            body = e.read(2048).decode("utf-8", "replace")
            code = re.search(r"<Code>([^<]+)</Code>", body)
            raise SystemExit("S3 %s on %s %s: %s (credential values never "
                             "printed)" % (e.code, "GET",
                                           key or "(list)",
                                           code.group(1) if code else body))

    def _full(self, rel):
        return "%s/%s" % (self.prefix, rel) if self.prefix else rel

    def list(self, subprefix=""):
        """[(key_rel_to_root, size)] under subprefix, paginated."""
        out, token = [], None
        prefix = self._full(subprefix) if subprefix else (
            self.prefix + "/" if self.prefix else "")
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            with self._signed_request(query=q) as resp:
                tree = ET.fromstring(resp.read())
            ns = tree.tag.split("}")[0] + "}" if "}" in tree.tag else ""
            for c in tree.iter(ns + "Contents"):
                key = c.find(ns + "Key").text
                size = int(c.find(ns + "Size").text)
                rel = key[len(self.prefix) + 1:] if self.prefix else key
                out.append((rel, size))
            trunc = tree.find(ns + "IsTruncated")
            if trunc is None or trunc.text != "true":
                return out
            token = tree.find(ns + "NextContinuationToken").text

    def get_to(self, rel, dest, version_id=None):
        """version_id (fix 3): fetch the EXACT object version the release
        manifest recorded at publish time, when the bucket returned one."""
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        query = {"versionId": version_id} if version_id else None
        with self._signed_request(key=self._full(rel), query=query) as resp, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)
        os.replace(tmp, dest)

    def get_bytes(self, rel):
        with self._signed_request(key=self._full(rel)) as resp:
            return resp.read()

    def get_bytes_with_version(self, rel):
        """Read a research-namespace control object and retain its VersionId.

        A v3 data reference is never resolved through this method; canonical
        objects use ``get_source_to`` below with the manifest's exact
        VersionId.  This versionless lookup is only the unavoidable first
        lookup of the release MANIFEST itself.
        """
        with self._signed_request(key=self._full(rel)) as resp:
            raw = resp.read()
            version_id = resp.headers.get("x-amz-version-id")
        if (not version_id or version_id.strip().lower() == "null"):
            raise SystemExit(
                "REFUSED (v3 manifest): research MANIFEST has no non-null "
                "S3 VersionId; versioning is required")
        return raw, version_id

    def get_source_to(self, bucket, key, dest, version_id):
        """Exact-version GET of one allowlisted canonical object.

        Deliberately has no list/latest/versionless fallback.  The manifest
        validator has already constrained bucket and key; repeat the bucket
        and VersionId gates here so a future caller cannot bypass them.
        """
        if bucket != ref.TRUSTED_BUCKET or bucket != self.bucket:
            raise SystemExit("REFUSED (v3 source): untrusted bucket %r" % bucket)
        if (not isinstance(version_id, str) or not version_id.strip()
                or version_id.strip().lower() == "null"):
            raise SystemExit("REFUSED (v3 source): exact VersionId is required")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        with self._signed_request(
                key=key, query={"versionId": version_id}) as resp, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)
        os.replace(tmp, dest)

    def describe(self):
        return "s3://%s/%s" % (self.bucket, self.prefix)


class LocalStore:
    """Directory root standing in for the bucket prefix (fixtures/offline)."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def list(self, subprefix=""):
        base = os.path.join(self.root, subprefix) if subprefix else self.root
        out = []
        for b, _d, files in os.walk(base):
            for fn in files:
                p = os.path.join(b, fn)
                out.append((os.path.relpath(p, self.root).replace(os.sep, "/"),
                            os.stat(p).st_size))
        return sorted(out)

    def get_to(self, rel, dest, version_id=None):
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(os.path.join(self.root, rel), dest)

    def get_bytes(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()

    def get_bytes_with_version(self, rel):
        raw = self.get_bytes(rel)
        # Offline fixtures have no S3 response headers.  The synthetic value
        # is loud and content-bound; it is never accepted as a production
        # canonical object VersionId.
        return raw, "LOCAL-FIXTURE-MANIFEST-%s" % hashlib.sha256(raw).hexdigest()

    def get_source_to(self, bucket, key, dest, version_id):
        if bucket != ref.TRUSTED_BUCKET:
            raise SystemExit("REFUSED (v3 source): untrusted bucket %r" % bucket)
        if (not isinstance(version_id, str) or not version_id.strip()
                or version_id.strip().lower() == "null"):
            raise SystemExit("REFUSED (v3 source): exact VersionId is required")
        # Fixture-only canonical namespace, intentionally outside releases/.
        src_root = os.path.join(self.root, "__canonical__", bucket)
        src = safe_cache_path(src_root, key)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(src, dest)

    def describe(self):
        return self.root


def make_store(root):
    if root.startswith("s3://"):
        m = re.match(r"^s3://([^/]+)/?(.*)$", root)
        key_id, secret, region = load_creds()
        return S3Store(m.group(1), m.group(2), region, key_id, secret)
    if not os.path.isdir(root):
        raise SystemExit("root is neither s3:// nor an existing dir: %s"
                         % root)
    return LocalStore(root)


# ---------------------------------------------------------------------------
# Release model
# ---------------------------------------------------------------------------

def list_releases(store):
    """release_id -> {exposed, objects: [(rel_after_id, size)], bytes}."""
    rel = {}
    for key, size in store.list("releases/"):
        parts = key.split("/", 2)
        if len(parts) < 3 or parts[0] != "releases":
            continue
        rid, rest = parts[1], parts[2]
        r = rel.setdefault(rid, {"exposed": False, "objects": [], "bytes": 0})
        if rest == "MANIFEST.json":
            r["exposed"] = True
        r["objects"].append((rest, size))
        r["bytes"] += size
    return rel


def cache_release_dir(cache, rid):
    # rid is externally supplied — containment-check it as a single segment
    if (not rid or "/" in rid or "\\" in rid
            or rid in (".", "..") or rid.startswith(".")):
        raise SystemExit("REFUSED (cache containment): illegal release id "
                         "%r" % rid)
    return os.path.join(cache, "releases", rid)


def is_quarantined_legacy(manifest):
    """Remediation item 5: releases lacking the v2 publication-state /
    version-binding freeze (e.g. pre-correction-order legacy releases) are
    QUARANTINED — readable only with an explicit branded override."""
    if ref.is_reference_manifest(manifest):
        return False
    return (manifest.get("schema_version") != "research-release-manifest-v2"
            or not manifest.get("publication_state_sha256")
            or not isinstance(manifest.get("version_binding"), dict))


QUARANTINE_BRAND = ("QUARANTINED-LEGACY OVERRIDE: this release predates the "
                    "publication-state/version-binding freeze; treat every "
                    "derived result as legacy-grade evidence")


def quarantine_gate(manifest, rid, allow):
    if not is_quarantined_legacy(manifest):
        return False
    if not allow:
        raise SystemExit(
            "REFUSED (quarantined legacy): release %s lacks the "
            "publication_state/version_binding freeze and is QUARANTINED. "
            "Re-run with --allow-legacy-quarantined to read it anyway — "
            "every output will be branded." % rid)
    sys.stderr.write("!! %s (%s)\n" % (QUARANTINE_BRAND, rid))
    return True


def verified_marker(cache, rid):
    return os.path.join(cache_release_dir(cache, rid), ".VERIFIED.json")


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _strict_json_bytes(raw, label):
    def pairs_hook(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate JSON key %r" % key)
            out[key] = value
        return out

    def bad_constant(value):
        raise ValueError("non-finite JSON number %s" % value)

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs_hook,
                          parse_constant=bad_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit("REFUSED (%s): invalid JSON: %s" % (label, exc))


def reference_manifest_dir(cache, rid):
    # Neutral control cache: a manifest alone must never make a release tree
    # look partially materialized or verified.
    cache_release_dir(cache, rid)  # validate the externally supplied id
    return os.path.join(cache, "reference_manifests", rid)


def _cache_reference_manifest(cache, rid, raw, version_id):
    manifest = _strict_json_bytes(raw, "v3 manifest")
    try:
        ref.validate_manifest(manifest, rid)
    except ref.ReferenceManifestError as exc:
        raise SystemExit("REFUSED (v3 manifest): %s" % exc)
    digest = hashlib.sha256(raw).hexdigest()
    if (not isinstance(version_id, str) or not version_id.strip()
            or version_id.strip().lower() == "null"):
        raise SystemExit("REFUSED (v3 manifest): non-null VersionId required")
    mdir = reference_manifest_dir(cache, rid)
    identity_path = os.path.join(mdir, "IDENTITY.json")
    if os.path.isfile(identity_path):
        with open(identity_path, encoding="utf-8") as f:
            old = json.load(f)
        if (old.get("manifest_sha256") != digest
                or old.get("manifest_version_id") != version_id):
            raise SystemExit(
                "REFUSED (v3 manifest equivocation): release %s was already "
                "bound to a different manifest byte/version identity" % rid)
    _atomic_write(os.path.join(mdir, "MANIFEST.json"), raw)
    _atomic_write(identity_path, json.dumps({
        "schema": "research-reference-manifest-cache-v1",
        "release_id": rid,
        "manifest_sha256": digest,
        "manifest_version_id": version_id,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
    return manifest, raw, version_id, digest


def reference_manifest_record(cache, store, rid):
    """Return validated manifest, exact bytes, VersionId and SHA-256."""
    final_manifest = os.path.join(cache_release_dir(cache, rid),
                                  "MANIFEST.json")
    neutral = reference_manifest_dir(cache, rid)
    neutral_manifest = os.path.join(neutral, "MANIFEST.json")
    identity_path = os.path.join(neutral, "IDENTITY.json")
    cached_path = (final_manifest if os.path.isfile(final_manifest)
                   else neutral_manifest if os.path.isfile(neutral_manifest)
                   else None)
    if cached_path:
        with open(cached_path, "rb") as f:
            raw = f.read()
        manifest = _strict_json_bytes(raw, "cached v3 manifest")
        try:
            ref.validate_manifest(manifest, rid)
        except ref.ReferenceManifestError as exc:
            raise SystemExit("REFUSED (cached v3 manifest): %s" % exc)
        identity = None
        if os.path.isfile(identity_path):
            with open(identity_path, encoding="utf-8") as f:
                identity = json.load(f)
        marker = verified_marker(cache, rid)
        if identity is None and os.path.isfile(marker):
            with open(marker, encoding="utf-8") as f:
                identity = json.load(f)
        digest = hashlib.sha256(raw).hexdigest()
        if (not isinstance(identity, dict)
                or identity.get("manifest_sha256") != digest
                or not identity.get("manifest_version_id")):
            raise SystemExit(
                "REFUSED (cached v3 manifest): manifest identity is missing "
                "or inconsistent")
        return (manifest, raw, identity["manifest_version_id"], digest)

    rel = "releases/%s/MANIFEST.json" % rid
    if not hasattr(store, "get_bytes_with_version"):
        raise SystemExit("REFUSED (v3 manifest): store cannot retain VersionId")
    raw, version_id = store.get_bytes_with_version(rel)
    return _cache_reference_manifest(cache, rid, raw, version_id)


def assert_reference_manifest_current(store, rid, raw, version_id):
    """Close the manifest/data TOCTOU window before materialization."""
    if not hasattr(store, "get_bytes_with_version"):
        raise ReferenceVerificationError(
            "store cannot recheck the v3 manifest VersionId")
    rel = "releases/%s/MANIFEST.json" % rid
    current_raw, current_version = store.get_bytes_with_version(rel)
    if current_version != version_id or current_raw != raw:
        raise ReferenceVerificationError(
            "v3 manifest changed while exact-version objects were fetched")


def read_manifest(cache, store, rid):
    if ref.RELEASE_RE.match(rid):
        return reference_manifest_record(cache, store, rid)[0]
    cached = os.path.join(cache_release_dir(cache, rid), "MANIFEST.json")
    if os.path.isfile(cached):
        with open(cached) as f:
            return json.load(f)
    raw = store.get_bytes("releases/%s/MANIFEST.json" % rid)
    parsed = _strict_json_bytes(raw, "release manifest")
    if ref.is_reference_manifest(parsed):
        # A reference manifest under the copied-v2 namespace is invalid; do
        # not write it into a release-shaped directory.
        try:
            ref.validate_manifest(parsed, rid)
        except ref.ReferenceManifestError as exc:
            raise SystemExit("REFUSED (v3 manifest): %s" % exc)
        raise SystemExit("REFUSED (v3 manifest): v3 release id namespace required")
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as f:
        f.write(raw)
    return parsed


def dir_bytes(path):
    total = 0
    for b, _d, files in os.walk(path):
        for fn in files:
            try:
                total += os.stat(os.path.join(b, fn)).st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_inventory(store, cache):
    releases = list_releases(store)
    print("research root: %s" % store.describe())
    if not releases:
        print("EMPTY — no releases published yet (publisher runs post-seal "
              "on the EC2 box; first release lands after the next seal).")
    rows, total_bytes = [], 0
    for rid in sorted(releases):
        r = releases[rid]
        total_bytes += r["bytes"]
        status = "TORN/UNPUBLISHED (no MANIFEST — not exposed)"
        date = rid.split("__")[0] if _RELEASE_RE.match(rid) else "?"
        tier = tl1 = rfq = l2 = mode = "?"
        object_count, object_bytes = len(r["objects"]), r["bytes"]
        if r["exposed"]:
            m = read_manifest(cache, store, rid)
            is_v3 = ref.is_reference_manifest(m)
            mode = "REFERENCE_V3" if is_v3 else "COPIED_V2"
            tier = ((m.get("evidence") or {}).get("tier", "?")
                    if is_v3 else m.get("evidence_tier", "?"))
            tl1 = m.get("tl1_status", "?")
            ch = m.get("channels", {})
            rfq = ch.get("rfq", {}).get("status", "?")
            # fix 4: L2 status comes from the manifest per release — sealed
            # orderbooks_full facts are exposed when present, honestly
            # ABSENT_FROM_THIS_RELEASE otherwise.
            l2 = ch.get("orderbooks_l2", {}).get("status", "?")
            if is_quarantined_legacy(m):
                status = "QUARANTINED_LEGACY"
            else:
                status = "EXPOSED"
                if os.path.isfile(verified_marker(cache, rid)):
                    status = "EXPOSED+VERIFIED_LOCALLY"
                bmode = ("CANONICAL_REFERENCE" if is_v3 else
                         m.get("version_binding", {}).get("mode"))
                if bmode not in ("VERSION_BOUND", "CANONICAL_REFERENCE"):
                    status += "!" + str(bmode)
            object_count = len(m.get("objects", []))
            object_bytes = sum(int(o.get("size") or 0)
                               for o in m.get("objects", []))
        rows.append((rid, date, mode, status, object_count,
                     object_bytes / 1e9, tier, tl1, l2, rfq))
    if rows:
        print("%-55s %-11s %-12s %-28s %6s %9s %-26s %-8s %-26s %s"
              % ("release_id", "date", "mode", "status", "files", "GB",
                 "tier", "tl1", "l2", "rfq"))
        for row in rows:
            print("%-55s %-11s %-12s %-28s %6d %9.3f %-26s %-8s %-26s %s"
                  % row)
        if any("QUARANTINED_LEGACY" in row[3] for row in rows):
            print("note: QUARANTINED_LEGACY releases lack the "
                  "publication-state/version-binding freeze; fetch/verify "
                  "refuse them without --allow-legacy-quarantined "
                  "(all outputs branded).")
        if any("LOCAL_FIXTURE" in row[3] for row in rows):
            print("NOTE: releases marked !LOCAL_FIXTURE_DEST_NO_VERSIONS "
                  "came from an offline fixture destination — never a real "
                  "publication (real S3 is VERSION_BOUND or fails closed, "
                  "P0-1/P0-2 VERSIONING_REQUIRED).")
    # ---- cost + disk (spec HYGIENE) ----------------------------------------
    gb = total_bytes / 1e9
    days = {r.split("__")[0] for r in releases if _RELEASE_RE.match(r)}
    per_day = gb / max(1, len(days))
    print("\nS3 (research/ prefix only): %.3f GB across %d release(s)" %
          (gb, len(releases)))
    print("monthly cost estimate: storage now $%.2f/mo; at ~%.2f GB/day "
          "growth ≈ +$%.2f/mo added EACH month; GET ~$%.4f per full "
          "re-fetch; egress $%.2f per re-fetch of everything (only when "
          "fetching outside AWS). Estimate only, us-east-2 rate card." %
          (gb * PRICE_STORAGE_GB_MO, per_day,
           per_day * 30 * PRICE_STORAGE_GB_MO,
           len(releases) and sum(len(r["objects"])
                                 for r in releases.values())
           / 1000.0 * PRICE_GET_PER_1K or 0.0,
           gb * PRICE_EGRESS_GB))
    cache_b = dir_bytes(cache) if os.path.isdir(cache) else 0
    print("local cache %s: %.3f GB (prunable; S3/seals/production never)"
          % (cache, cache_b / 1e9))
    return 0


def _iter_manifest_objects(manifest, with_rfq):
    for o in manifest["objects"]:
        if o["key"].startswith("raw_rfq/") and not with_rfq:
            continue
        yield o


class ReferenceVerificationError(RuntimeError):
    pass


def reference_content_path(cache, digest):
    if not ref.SHA256_RE.match(str(digest)):
        raise ReferenceVerificationError("invalid content-cache SHA-256")
    return os.path.join(cache, "objects", "sha256", digest[:2], digest)


def _content_matches(path, item):
    return (os.path.isfile(path)
            and not os.path.islink(path)
            and os.stat(path).st_size == item["size"]
            and sha256_file(path) == item["sha256"])


def _quarantine_content(cache, path, digest):
    if not os.path.lexists(path):
        return
    qdir = os.path.join(cache, "quarantine", "objects", digest[:2])
    os.makedirs(qdir, exist_ok=True)
    target = os.path.join(qdir, "%s.%s.%d" % (
        digest, datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y%m%dT%H%M%S%fZ"), os.getpid()))
    os.replace(path, target)


def _fetch_reference_content(store, cache, item):
    dest = reference_content_path(cache, item["sha256"])
    if _content_matches(dest, item):
        return dest, False
    if os.path.lexists(dest):
        _quarantine_content(cache, dest, item["sha256"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = os.path.join(os.path.dirname(dest), ".%s.fetch.%d" % (
        item["sha256"], os.getpid()))
    for residue in (tmp, tmp + ".part"):
        if os.path.lexists(residue):
            contained_remove(residue, cache)
    try:
        store.get_source_to(
            item["source_bucket"], item["source_key"], tmp,
            item["source_version_id"])
        if not _content_matches(tmp, item):
            got_size = os.stat(tmp).st_size if os.path.isfile(tmp) else None
            got_sha = sha256_file(tmp) if os.path.isfile(tmp) else None
            raise ReferenceVerificationError(
                "exact-version bytes mismatch for %s (size=%r sha256=%r)" %
                (item["logical_key"], got_size, got_sha))
        os.replace(tmp, dest)
        return dest, True
    finally:
        for residue in (tmp, tmp + ".part"):
            if os.path.lexists(residue):
                contained_remove(residue, cache)


def _load_reference_json(path, label):
    try:
        with open(path, "rb") as f:
            return _strict_json_bytes(f.read(), label)
    except OSError as exc:
        raise ReferenceVerificationError("%s cannot be read: %s" % (label, exc))


def _receipt_cache_item(descriptor):
    binding = descriptor["receipt_object"]
    return {
        "logical_key": "canonical_receipt",
        "source_bucket": binding["bucket"],
        "source_key": binding["key"],
        "source_version_id": binding["version_id"],
        "size": binding["size"],
        "sha256": binding["sha256"],
    }


def _verify_reference_receipt(cache, descriptor):
    item = _receipt_cache_item(descriptor)
    path = reference_content_path(cache, item["sha256"])
    if not _content_matches(path, item):
        raise ReferenceVerificationError(
            "durable canonical receipt is absent or corrupt")
    receipt = _load_reference_json(path, "durable canonical receipt")
    try:
        result = ref.validate_durable_receipt(receipt, descriptor)
    except ref.ReferenceManifestError as exc:
        raise ReferenceVerificationError(str(exc))
    return result


def _capture_receipt_quality(receipt, seal, date):
    if (not isinstance(receipt, dict)
            or receipt.get("schema_version") != "capture-gap-scan-receipt-v1"
            or receipt.get("date") != date):
        raise ReferenceVerificationError(
            "capture-gap receipt schema/date binding is invalid")
    files = receipt.get("files")
    if not isinstance(files, list):
        raise ReferenceVerificationError("capture-gap receipt inventory missing")
    want = {
        row["file"]: row["size"] for row in seal.get("raw_files", [])
        if isinstance(row, dict)
        and isinstance(row.get("file"), str)
        and row["file"].startswith("date=%s/" % date)
        and os.path.basename(row["file"]).startswith("firehose_")
    }
    got = {}
    for row in files:
        if (not isinstance(row, dict) or not isinstance(row.get("file"), str)
                or not isinstance(row.get("bytes"), int)
                or isinstance(row.get("bytes"), bool)
                or row["bytes"] < 0 or row["file"] in got):
            raise ReferenceVerificationError(
                "capture-gap receipt inventory is malformed or duplicated")
        got[row["file"]] = row["bytes"]
    if got != want:
        raise ReferenceVerificationError(
            "capture-gap receipt does not reproduce sealed firehose inventory")
    if (receipt.get("n_files") != len(files)
            or receipt.get("total_bytes") != sum(got.values())
            or receipt.get("unreadable") is not False
            or not isinstance(receipt.get("gaps"), list)):
        raise ReferenceVerificationError(
            "capture-gap receipt result/count fields are invalid")
    for key in ("records", "unparsed"):
        value = receipt.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReferenceVerificationError(
                "capture-gap receipt %s is invalid" % key)
    lo = wc.day_start_us(date)
    hi = lo + 86_400_000_000
    previous_end = None
    for gap in receipt["gaps"]:
        if (not isinstance(gap, dict)
                or set(gap) != {"start_us", "end_us"}
                or not isinstance(gap.get("start_us"), int)
                or isinstance(gap.get("start_us"), bool)
                or not isinstance(gap.get("end_us"), int)
                or isinstance(gap.get("end_us"), bool)
                or not (lo <= gap["start_us"] < gap["end_us"] <= hi)
                or (previous_end is not None
                    and gap["start_us"] < previous_end)):
            raise ReferenceVerificationError(
                "capture-gap receipt interval is malformed")
        previous_end = gap["end_us"]
    return receipt["gaps"]


def _l2_receipt_quality(receipt, seal, date):
    stat_keys = ("lines", "parse_errors", "seq_gap_events",
                 "seq_missed_total", "sids_total", "sids_with_seq_gaps")
    if (not isinstance(receipt, dict)
            or receipt.get("schema_version") != "l2-gap-receipt-v1"
            or receipt.get("date") != date
            or not isinstance(receipt.get("file_inventory"), list)):
        raise ReferenceVerificationError("L2 quality receipt is not date-exact")
    for key in stat_keys:
        value = receipt.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReferenceVerificationError(
                "L2 quality receipt statistic %s is invalid" % key)
    want = {
        row["file"]: row["size"] for row in seal.get("raw_files", [])
        if isinstance(row, dict)
        and isinstance(row.get("file"), str)
        and row["file"].startswith("date=%s/" % date)
        and os.path.basename(row["file"]).startswith("l2_")
    }
    got = {}
    for row in receipt["file_inventory"]:
        if (not isinstance(row, dict) or not isinstance(row.get("file"), str)
                or not isinstance(row.get("bytes"), int)
                or isinstance(row.get("bytes"), bool)
                or row["bytes"] < 0 or row["file"] in got):
            raise ReferenceVerificationError(
                "L2 quality receipt inventory is malformed or duplicated")
        got[row["file"]] = row["bytes"]
    if got != want or receipt.get("no_l2_files") is not (not want):
        raise ReferenceVerificationError(
            "L2 quality receipt does not reproduce sealed L2 inventory")
    return {key: receipt.get(key) for key in ("no_l2_files",) + stat_keys}


def _derive_reference_tier(seal, l2_facts_present, l2_evidence_ok):
    reasons = []
    if seal.get("method") != "full_v2":
        reasons.append("seal method=%r (expected full_v2)" % seal.get("method"))
    if seal.get("go_no_go_eligible") is not True:
        reasons.append("seal not go_no_go_eligible")
    if l2_facts_present and not l2_evidence_ok:
        reasons.append("orderbooks_full facts present but no VALID matching "
                       "per-date L2 seq-quality receipt (absent) (mandatory, P0-3)")
    cq = str(seal.get("capture_quality_status") or "")
    if (not cq or "UNASSESSED" in cq.upper() or any(
            word in cq.upper() for word in
            ("FAIL", "BAD", "DEGRADED", "REJECT"))):
        reasons.append("capture_quality_status=%r can never earn "
                       "SEALED_CONFIRMATION (P0-3: unassessed quality is "
                       "not confirmation)" % cq)
    tier = "SEALED_CONFIRMATION" if not reasons \
        else "SEALED_DEGRADED_EVIDENCE"
    return tier, {
        "seal_status": seal.get("status"),
        "seal_version": seal.get("version"),
        "method": seal.get("method"),
        "go_no_go_eligible": seal.get("go_no_go_eligible"),
        "capture_quality_status": seal.get("capture_quality_status"),
        "gap_receipt_affirmative": True,
        "l2_facts_present": bool(l2_facts_present),
        "l2_evidence_ok": bool(l2_evidence_ok),
        "downgrade_reasons": reasons,
    }


def _verify_reference_tree(rdir, descriptor, require_rfq):
    date = descriptor["date"]
    n_ok = bytes_ok = 0
    rfq_rows = []
    for item in descriptor["objects"]:
        is_rfq = item["kind"] == "rfq"
        if is_rfq:
            rfq_rows.append(item)
            if not require_rfq:
                continue
        path = safe_release_object_path(rdir, item["local_key"])
        if not os.path.isfile(path):
            raise ReferenceVerificationError(
                "missing materialized object: %s" % item["logical_key"])
        if os.stat(path).st_size != item["size"]:
            raise ReferenceVerificationError(
                "size mismatch: %s" % item["logical_key"])
        if sha256_file(path) != item["sha256"]:
            raise ReferenceVerificationError(
                "sha256 mismatch: %s" % item["logical_key"])
        n_ok += 1
        bytes_ok += item["size"]

    seal_path = os.path.join(rdir, "seal", "date=%s.json" % date)
    seal = _load_reference_json(seal_path, "source seal")
    if (seal.get("status") != "SEALED" or seal.get("version") != 2
            or seal.get("method") != "full_v2" or seal.get("date") != date
            or sha256_file(seal_path) != descriptor["source_seal"]["sha256"]):
        raise ReferenceVerificationError("source seal authority check failed")
    if (seal.get("manifest_date_sha256")
            != descriptor["seal"].get("manifest_date_sha256")):
        raise ReferenceVerificationError("seal summary manifest digest mismatch")
    raw_files = seal.get("raw_files")
    if not isinstance(raw_files, list):
        raise ReferenceVerificationError("source seal raw_files is malformed")
    raw_index = {}
    for row in raw_files:
        if (not isinstance(row, dict) or not isinstance(row.get("file"), str)
                or row["file"] in raw_index):
            raise ReferenceVerificationError("source seal raw_files is duplicated")
        raw_index[row["file"]] = row

    archive_rows = seal.get("archive_file_stats")
    if not isinstance(archive_rows, list):
        raise ReferenceVerificationError(
            "source seal archive_file_stats is malformed")
    archive_index = {}
    for row in archive_rows:
        if (not isinstance(row, dict) or not isinstance(row.get("file"), str)
                or row["file"] in archive_index):
            raise ReferenceVerificationError(
                "source seal archive_file_stats is duplicated")
        archive_index[row["file"]] = row
    facts = [item for item in descriptor["objects"] if item["kind"] == "facts"]
    fact_index = {
        item["logical_key"][len("warehouse/facts/"):]: item
        for item in facts
    }
    if set(fact_index) != set(archive_index):
        raise ReferenceVerificationError(
            "fact references do not equal the sealed archive inventory")
    for rel, item in fact_index.items():
        proof = archive_index[rel]
        if (proof.get("table") != item["channel"]
                or proof.get("size") != item["size"]
                or proof.get("sha256") != item["sha256"]):
            raise ReferenceVerificationError(
                "fact reference differs from source seal: %s" % rel)
    if rfq_rows and descriptor["evidence_tier"] != "SEALED_CONFIRMATION":
        raise ReferenceVerificationError(
            "RFQ references require SEALED_CONFIRMATION evidence")
    for item in rfq_rows:
        rel = item["logical_key"][len("raw_rfq/"):]
        proof = raw_index.get(rel)
        if (not proof or proof.get("size") != item["size"]
                or proof.get("sha256") != item["sha256"]):
            raise ReferenceVerificationError(
                "RFQ reference is not exactly enumerated by the source seal: %s"
                % rel)

    wm = os.path.join(rdir, "warehouse_manifest", "manifest.csv")
    if not os.path.isfile(wm):
        raise ReferenceVerificationError("date-only warehouse manifest missing")
    got_manifest, _rows = wc.manifest_date_sha256(wm, date)
    if got_manifest != seal.get("manifest_date_sha256"):
        raise ReferenceVerificationError("manifest_date_sha256 mismatch")

    capture_path = os.path.join(
        rdir, "quality", "gap_receipt_%s.json" % date)
    capture_gaps = _capture_receipt_quality(
        _load_reference_json(capture_path, "capture-gap receipt"), seal, date)
    gap_projection = os.path.join(
        rdir, "quality", "capture_gaps_%s.csv" % date)
    expected_gap_projection = ("start_us,end_us\n" + "".join(
        "%d,%d\n" % (gap["start_us"], gap["end_us"])
        for gap in capture_gaps)).encode("utf-8")
    has_gap_projection = any(
        item["kind"] == "capture_gaps_projection"
        for item in descriptor["objects"])
    if has_gap_projection:
        try:
            with open(gap_projection, "rb") as f:
                got_gap_projection = f.read()
        except OSError as exc:
            raise ReferenceVerificationError(
                "capture-gap projection cannot be read: %s" % exc)
        if got_gap_projection != expected_gap_projection:
            raise ReferenceVerificationError(
                "capture-gap projection differs from receipt intervals")
    elif capture_gaps or os.path.lexists(gap_projection):
        raise ReferenceVerificationError(
            "capture-gap projection absence disagrees with receipt intervals")

    l2_facts = "orderbooks_full" in descriptor["tables"]
    l2_quality = None
    l2_path = os.path.join(rdir, "quality", "l2_gaps.json")
    if l2_facts:
        l2_quality = _l2_receipt_quality(
            _load_reference_json(l2_path, "L2 quality receipt"), seal, date)
    elif os.path.lexists(l2_path):
        raise ReferenceVerificationError("unexpected L2 quality receipt")
    modern_contract = (
        descriptor.get("manifest_contract_version")
        == ref.MANIFEST_CONTRACT_VERSION)
    # In the fixed modern contract, the channel alias is an immutable
    # component/reference binding.  The decoded receipt remains independently
    # validated above and is used to rederive the evidence tier; it is not the
    # same JSON type as that binding.  Historical pinned v3 manifests retain
    # the legacy raw-statistics alias comparison.
    if (not modern_contract
            and descriptor["channels"]["orderbooks_l2"].get("seq_quality")
            != l2_quality):
        raise ReferenceVerificationError("L2 receipt differs from channel summary")

    expected_tier, expected_basis = _derive_reference_tier(
        seal, l2_facts, l2_quality is not None)
    if (descriptor["evidence_tier"] != expected_tier
            or descriptor["evidence_basis"] != expected_basis):
        raise ReferenceVerificationError(
            "evidence tier/basis does not rederive from sealed quality evidence")

    correction_dir = os.path.join(rdir, "corrections")
    late = os.path.join(correction_dir, "date=%s" % date,
                        "late_rows.ndjson")
    ledger = os.path.join(correction_dir, "date=%s" % date,
                          "ledger_day.ndjson")
    correction_present = os.path.isfile(late) or os.path.isfile(ledger)
    if correction_present and not (os.path.isfile(late) and os.path.isfile(ledger)):
        raise ReferenceVerificationError("correction pair is incomplete locally")
    ledger_lines = 0
    if correction_present:
        with open(ledger, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise ReferenceVerificationError(
                        "correction ledger line %d is invalid: %s" %
                        (lineno, exc))
                if (not isinstance(row, dict)
                        or row.get("event") !=
                        "LATE_FACT_DIVERTED_TO_CORRECTIONS"
                        or row.get("exchange_date", row.get("date")) != date):
                    raise ReferenceVerificationError(
                        "correction ledger is not a date-only projection")
                ledger_lines += 1
    if (not modern_contract
            and ledger_lines
            != descriptor["corrections"].get("ledger_day_entries")):
        raise ReferenceVerificationError("correction ledger count mismatch")

    tables_out = {}
    conn = duckdb_connect()
    try:
        for table, frozen in sorted(descriptor["tables"].items()):
            sample = None
            for item in descriptor["objects"]:
                if item["kind"] == "facts" and item["channel"] == table:
                    candidate = safe_release_object_path(rdir, item["local_key"])
                    if os.path.isfile(candidate):
                        sample = candidate
                        break
            if sample is None:
                raise ReferenceVerificationError(
                    "no materialized facts file for table %s" % table)
            query = ("DESCRIBE SELECT * FROM read_parquet(?)"
                     if sample.endswith(".parquet") else
                     "DESCRIBE SELECT * FROM read_csv_auto(?)")
            columns = [row[0] for row in conn.execute(query, [sample]).fetchall()]
            if columns != frozen["columns"]:
                raise ReferenceVerificationError(
                    "schema drift in %s (got %r, frozen %r)" %
                    (table, columns, frozen["columns"]))
            actual = {
                "tl1_ladder_columns_present":
                    all(column in columns for column in LADDER_COLUMNS),
                "ws_sid_present": "ws_sid" in columns,
                "ws_seq_present": "ws_seq" in columns,
            }
            for key, value in actual.items():
                if frozen.get(key) != value:
                    raise ReferenceVerificationError(
                        "frozen table capability mismatch in %s/%s" %
                        (table, key))
            tables_out[table] = actual
    finally:
        conn.close()
    tl1_count = sum(1 for value in tables_out.values()
                    if value["tl1_ladder_columns_present"])
    actual_tl1 = ("TL1" if tables_out and tl1_count == len(tables_out)
                  else "PRE-TL1" if not tl1_count else "MIXED")
    if actual_tl1 != descriptor["tl1_status"]:
        raise ReferenceVerificationError("TL1 status does not rederive")

    rfq_status = ("ABSENT_FROM_RELEASE" if not rfq_rows else
                  "VERIFIED_SEALED_RAW" if require_rfq else
                  "NOT_FETCHED_OPT_IN")
    return {
        "objects_verified": n_ok,
        "bytes_verified": bytes_ok,
        "rfq_status": rfq_status,
        "tables": tables_out,
    }


def _reference_failure(cache, rid, detail):
    path = os.path.join(cache, "reference_failures", "%s.json" % rid)
    _atomic_write(path, json.dumps({
        "schema": "research-reference-fetch-failure-v1",
        "release_id": rid,
        "failed_at_utc": _now(),
        "failure": str(detail),
    }, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    sys.stderr.write(
        "\n!! REFERENCE FETCH/VERIFY FAILED — %s is not materialized\n"
        "!! %s\n" % (rid, detail))
    return 2


def _materialize_reference_release(cache, rid, raw_manifest, manifest_version,
                                   manifest_sha, descriptor, include_rfq):
    release_root = os.path.join(cache, "releases")
    os.makedirs(release_root, exist_ok=True)
    stage = os.path.join(release_root, ".pending-%s-%d" % (rid, os.getpid()))
    contained_remove(stage, cache, is_dir=True)
    os.makedirs(stage)
    try:
        _atomic_write(os.path.join(stage, "MANIFEST.json"), raw_manifest)
        for item in descriptor["objects"]:
            if item["kind"] == "rfq" and not include_rfq:
                continue
            content = reference_content_path(cache, item["sha256"])
            if not _content_matches(content, item):
                raise ReferenceVerificationError(
                    "content cache changed before materialization: %s" %
                    item["logical_key"])
            dest = safe_release_object_path(stage, item["local_key"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.symlink(os.path.abspath(content), dest)
        verified = _verify_reference_tree(stage, descriptor, include_rfq)
        marker = {
            "schema": "research-reference-verified-v1",
            "storage_mode": "REFERENCE_V3",
            "version_binding_mode": ref.STORAGE_MODE,
            "release_id": rid,
            "date": descriptor["date"],
            "verified_at_utc": _now(),
            "published_at_utc": descriptor["published_at_utc"],
            "generated_at_utc": descriptor["published_at_utc"],
            "manifest_version_id": manifest_version,
            "manifest_sha256": manifest_sha,
            "reference_set_sha256": descriptor["reference_set_sha256"],
            "object_semantics_sha256": descriptor["object_semantics_sha256"],
            "publication_state_sha256":
                descriptor["publication_state_sha256"],
            "canonical_receipt_set_sha256":
                descriptor["canonical_receipt_set_sha256"],
            "canonical_receipt_object_sha256":
                descriptor["receipt_object"]["sha256"],
            "canonical_receipt_verified": True,
            "evidence_tier": descriptor["evidence_tier"],
            "evidence_tier_basis": descriptor["evidence_basis"],
            "tl1_status": descriptor["tl1_status"],
            "seal_sha256": descriptor["source_seal"]["sha256"],
            "corrections_total":
                int(descriptor["corrections"].get("included_files") or 0)
                + int(descriptor["corrections"].get("ledger_day_entries") or 0),
            "rfq_included": descriptor["rfq_included"],
            "objects_referenced": len(descriptor["objects"]),
            **verified,
        }
        _atomic_write(os.path.join(stage, ".VERIFIED.json"), json.dumps(
            marker, sort_keys=True, indent=2).encode("utf-8") + b"\n")

        final = cache_release_dir(cache, rid)
        old = os.path.join(release_root, ".previous-%s-%d" % (rid, os.getpid()))
        contained_remove(old, cache, is_dir=True)
        moved_old = False
        if os.path.lexists(final):
            os.replace(final, old)
            moved_old = True
        try:
            os.replace(stage, final)
        except BaseException:
            if moved_old and not os.path.lexists(final):
                os.replace(old, final)
            raise
        if moved_old:
            contained_remove(old, cache, is_dir=True)
        failure = os.path.join(cache, "reference_failures", "%s.json" % rid)
        contained_remove(failure, cache)
        return marker
    finally:
        if os.path.lexists(stage):
            contained_remove(stage, cache, is_dir=True)


def cmd_fetch_reference(store, cache, rid, with_rfq):
    manifest, raw, manifest_version, manifest_sha = \
        reference_manifest_record(cache, store, rid)
    try:
        descriptor = ref.validate_manifest(manifest, rid)
        previous_rfq = False
        marker_path = verified_marker(cache, rid)
        if os.path.isfile(marker_path):
            try:
                with open(marker_path, encoding="utf-8") as f:
                    previous_rfq = (json.load(f).get("rfq_status")
                                    == "VERIFIED_SEALED_RAW")
            except (OSError, ValueError):
                previous_rfq = False
        include_rfq = bool(with_rfq or previous_rfq)
        fetched = cached = 0
        _path, downloaded = _fetch_reference_content(
            store, cache, _receipt_cache_item(descriptor))
        fetched += int(downloaded)
        cached += int(not downloaded)
        _verify_reference_receipt(cache, descriptor)
        for item in descriptor["objects"]:
            if item["kind"] == "rfq" and not include_rfq:
                continue
            _path, downloaded = _fetch_reference_content(store, cache, item)
            fetched += int(downloaded)
            cached += int(not downloaded)
        assert_reference_manifest_current(
            store, rid, raw, manifest_version)
        marker = _materialize_reference_release(
            cache, rid, raw, manifest_version, manifest_sha,
            descriptor, include_rfq)
    except (Exception, SystemExit) as exc:
        return _reference_failure(cache, rid, exc)
    print("[fetch] %s [REFERENCE_V3]: %d exact-version objects fetched, "
          "%d content-cache hits; RFQ=%s" %
          (rid, fetched, cached, marker["rfq_status"]))
    rebuild_view(cache)
    return 0


def cmd_fetch(store, cache, rid, with_rfq, allow_legacy=False):
    releases = list_releases(store)
    if rid not in releases:
        raise SystemExit("unknown release_id %r (see inventory)" % rid)
    if not releases[rid]["exposed"]:
        raise SystemExit("release %s has no MANIFEST.json — torn/unpublished, "
                         "NOT exposed to research" % rid)
    manifest = read_manifest(cache, store, rid)
    if ref.is_reference_manifest(manifest):
        return cmd_fetch_reference(store, cache, rid, with_rfq)
    quarantined = quarantine_gate(manifest, rid, allow_legacy)
    validate_manifest_keys(manifest, cache, rid)  # item 3: before ANY write
    binding = manifest.get("version_binding") or {}
    if not quarantined and binding.get("mode") != "VERSION_BOUND":
        sys.stderr.write("NOTE: %s carries binding mode %s (offline "
                         "fixture) — a real S3 release is always "
                         "VERSION_BOUND or it was never published "
                         "(P0-1/P0-2).\n" % (rid, binding.get("mode")))
    rdir = cache_release_dir(cache, rid)
    fetched = skipped = 0
    for o in _iter_manifest_objects(manifest, with_rfq):
        dest = safe_cache_path(rdir, o["key"])
        if os.path.isfile(dest) and os.stat(dest).st_size == o["size"]:
            skipped += 1
            continue
        # fix 3 + item 1: request the exact recorded VersionId when present
        store.get_to("releases/%s/%s" % (rid, o["key"]), dest,
                     version_id=o.get("version_id"))
        fetched += 1
    print("[fetch] %s: %d objects fetched, %d already cached (rfq %s)%s"
          % (rid, fetched, skipped,
             "included" if with_rfq else "skipped — cost-bearing; use "
             "--with-rfq",
             "  [%s]" % QUARANTINE_BRAND if quarantined else ""))
    return cmd_verify(store, cache, rid, allow_legacy=allow_legacy)


def _fail(cache, rid, failures):
    rdir = cache_release_dir(cache, rid)
    marker = verified_marker(cache, rid)
    contained_remove(marker, cache)  # deletes never escape the cache
    with open(os.path.join(rdir, ".FAILED.json"), "w") as f:
        json.dump({"release_id": rid, "failures": failures,
                   "failed_at_utc": _now()}, f, indent=2)
    sys.stderr.write(
        "\n" + "!" * 72 + "\n"
        "!! VERIFY FAILED — release %s is NOT exposed to research\n"
        "!! %d failure(s), first: %s\n"
        "!! prior verified releases stay active; see .FAILED.json\n"
        % (rid, len(failures), failures[0]) + "!" * 72 + "\n")
    rebuild_view(cache)
    return 2


def _now():
    return datetime.datetime.now(datetime.timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_verify_reference(store, cache, rid):
    try:
        manifest, raw, manifest_version, manifest_sha = \
            reference_manifest_record(cache, store, rid)
        descriptor = ref.validate_manifest(manifest, rid)
        rdir = cache_release_dir(cache, rid)
        marker_path = verified_marker(cache, rid)
        if not os.path.isdir(rdir) or not os.path.isfile(marker_path):
            raise ReferenceVerificationError(
                "reference release is not atomically materialized; run fetch")
        local_manifest = os.path.join(rdir, "MANIFEST.json")
        with open(local_manifest, "rb") as f:
            if f.read() != raw:
                raise ReferenceVerificationError(
                    "materialized MANIFEST differs from its frozen identity")
        with open(marker_path, encoding="utf-8") as f:
            old_marker = json.load(f)
        if (old_marker.get("manifest_version_id") != manifest_version
                or old_marker.get("manifest_sha256") != manifest_sha
                or old_marker.get("reference_set_sha256")
                != descriptor["reference_set_sha256"]):
            raise ReferenceVerificationError(
                "verified marker differs from manifest identity")
        if (old_marker.get("canonical_receipt_object_sha256")
                != descriptor["receipt_object"]["sha256"]):
            raise ReferenceVerificationError(
                "verified marker differs from durable receipt identity")
        _verify_reference_receipt(cache, descriptor)

        rfq_rows = [item for item in descriptor["objects"]
                    if item["kind"] == "rfq"]
        rfq_presence = [os.path.isfile(safe_release_object_path(
            rdir, item["local_key"])) for item in rfq_rows]
        if any(rfq_presence) and not all(rfq_presence):
            raise ReferenceVerificationError(
                "RFQ materialization is partial; all-or-none is required")
        require_rfq = bool(rfq_rows and all(rfq_presence))
        for item in descriptor["objects"]:
            if item["kind"] == "rfq" and not require_rfq:
                continue
            path = safe_release_object_path(rdir, item["local_key"])
            content = reference_content_path(cache, item["sha256"])
            if (not os.path.islink(path)
                    or os.path.realpath(path) != os.path.realpath(content)):
                raise ReferenceVerificationError(
                    "release object is not linked to its content identity: %s"
                    % item["logical_key"])
        verified = _verify_reference_tree(rdir, descriptor, require_rfq)
        marker = dict(old_marker)
        marker.update(verified)
        marker["verified_at_utc"] = _now()
        _atomic_write(marker_path, json.dumps(
            marker, sort_keys=True, indent=2).encode("utf-8") + b"\n")
        contained_remove(os.path.join(rdir, ".FAILED.json"), cache)
    except (Exception, SystemExit) as exc:
        if os.path.isdir(cache_release_dir(cache, rid)):
            return _fail(cache, rid, [str(exc)])
        return _reference_failure(cache, rid, exc)
    print("[verify] PASS %s [REFERENCE_V3] — tier=%s tl1=%s "
          "objects=%d (%.1f MB) RFQ=%s" %
          (rid, marker["evidence_tier"], marker["tl1_status"],
           marker["objects_verified"], marker["bytes_verified"] / 1e6,
           marker["rfq_status"]))
    rebuild_view(cache)
    return 0


def cmd_verify(store, cache, rid, allow_legacy=False):
    manifest = read_manifest(cache, store, rid)
    if ref.is_reference_manifest(manifest):
        return cmd_verify_reference(store, cache, rid)
    quarantined = quarantine_gate(manifest, rid, allow_legacy)
    validate_manifest_keys(manifest, cache, rid)  # item 3: before ANY read
    rdir = cache_release_dir(cache, rid)
    date = manifest["date"]
    failures = []
    rfq_status, rfq_present = "ABSENT_FROM_RELEASE", False

    # 0) version binding (item 1 + P0-1): a v2 release must carry a
    # consistent binding block; VERSION_BOUND demands a VersionId on every
    # object and the recorded binding digest must reproduce. The only
    # versionless mode accepted is the offline FIXTURE destination (loudly
    # noted); a real S3 release is version-bound or it was never published.
    binding = manifest.get("version_binding") or {}
    if not quarantined:
        mode = binding.get("mode")
        if mode not in ("VERSION_BOUND", "LOCAL_FIXTURE_DEST_NO_VERSIONS"):
            failures.append("version_binding mode missing/unknown/"
                            "non-publishable: %r" % mode)
        else:
            got = hashlib.sha256(json.dumps(
                {o["key"]: o.get("version_id")
                 for o in manifest["objects"]},
                sort_keys=True, separators=(",", ":"),
                ensure_ascii=True).encode()).hexdigest()
            if got != binding.get("bindings_sha256"):
                failures.append("version binding digest mismatch")
            if mode == "VERSION_BOUND" and any(
                    o.get("version_id") is None
                    for o in manifest["objects"]):
                failures.append("VERSION_BOUND release carries a null "
                                "version_id")
            if mode == "LOCAL_FIXTURE_DEST_NO_VERSIONS":
                sys.stderr.write("NOTE: %s came from an offline FIXTURE "
                                 "destination (no object versions) — never "
                                 "a real publication.\n" % rid)

    # 0b) P0-4: consumer-side identity — the manifest must BE the release
    # it claims. Recompute the publication-state digest from the frozen
    # state and verify the requested rid's date + seal prefix + __pub
    # suffix against the RECOMPUTED values; reject inconsistent manifests.
    if not quarantined:
        if manifest.get("release_id") != rid:
            failures.append("manifest.release_id %r != requested %r"
                            % (manifest.get("release_id"), rid))
        state = manifest.get("publication_state")
        if not isinstance(state, dict):
            failures.append("publication_state missing from v2 manifest")
        else:
            recomputed = hashlib.sha256(json.dumps(
                state, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True).encode()).hexdigest()
            if recomputed != manifest.get("publication_state_sha256"):
                failures.append("publication_state_sha256 does not "
                                "recompute from the frozen state")
            m_id = re.match(r"^(\d{4}-\d{2}-\d{2})__seal-([0-9a-f]{8})"
                            r"__pub-([0-9a-f]{16})$", rid)
            seal_sha_m = str(manifest.get("seal", {}).get("sha256") or "")
            if not m_id:
                failures.append("release id is not the v2 "
                                "date__seal__pub form")
            else:
                if m_id.group(1) != manifest.get("date"):
                    failures.append("rid date != manifest date")
                if m_id.group(2) != seal_sha_m[:8]:
                    failures.append("rid seal prefix != manifest seal "
                                    "digest")
                if m_id.group(3) != recomputed[:16]:
                    failures.append("rid __pub suffix != RECOMPUTED "
                                    "publication-state digest")
            if state.get("seal_sha256") != seal_sha_m:
                failures.append("publication_state seal digest != "
                                "manifest seal digest")
            objs = {o["key"]: o["sha256"] for o in manifest["objects"]}
            ge = state.get("gap_evidence") or {}
            gr_key = "quality/gap_receipt_%s.json" % manifest.get("date")
            if (ge.get("gap_receipt_sha256")
                    and objs.get(gr_key) != ge["gap_receipt_sha256"]):
                failures.append("gap receipt hash in the state is not "
                                "bound to the frozen objects")
            for cf in (state.get("corrections") or {}).get("files", []):
                if objs.get(cf.get("key")) != cf.get("sha256"):
                    failures.append("correction state not bound to frozen "
                                    "objects: %s" % cf.get("key"))

    # 1) every frozen object: present + size + sha256 (raw_rfq may be unfetched)
    n_ok = bytes_ok = 0
    for o in manifest["objects"]:
        path = safe_cache_path(rdir, o["key"])
        is_rfq = o["key"].startswith("raw_rfq/")
        if is_rfq:
            rfq_status = "IN_RELEASE_NOT_FETCHED"
        if not os.path.isfile(path):
            if is_rfq:
                continue
            failures.append("missing object: %s" % o["key"])
            continue
        if os.stat(path).st_size != o["size"]:
            failures.append("size mismatch: %s" % o["key"])
            continue
        if sha256_file(path) != o["sha256"]:
            failures.append("sha256 mismatch: %s" % o["key"])
            continue
        n_ok += 1
        bytes_ok += o["size"]
        if is_rfq:
            rfq_present = True
    if rfq_present:
        rfq_status = "VERIFIED_SEALED_RAW"

    # 2) seal anchor
    seal_path = os.path.join(rdir, "seal", "date=%s.json" % date)
    if not os.path.isfile(seal_path):
        failures.append("seal object missing from cache")
    else:
        if sha256_file(seal_path) != manifest["seal"]["sha256"]:
            failures.append("seal sha256 != manifest frozen seal digest")
        try:
            with open(seal_path) as f:
                seal = json.load(f)
            if not (seal.get("status") == "SEALED"
                    and seal.get("version") == 2
                    and seal.get("date") == date):
                failures.append("seal is not a SEALED v2 seal for %s" % date)
        except ValueError:
            failures.append("seal JSON unparsable")

    # 3) warehouse manifest date rows reproduce the sealed digest
    wm = os.path.join(rdir, "warehouse_manifest", "manifest.csv")
    if os.path.isfile(wm):
        got, _rows = wc.manifest_date_sha256(wm, date)
        if got != manifest["seal"]["manifest_date_sha256"]:
            failures.append("manifest_date_sha256 mismatch")
    else:
        failures.append("warehouse_manifest/manifest.csv missing")

    # 4) schema re-check vs frozen (duckdb, explicit memory_limit)
    tables_out = {}
    if not failures:
        conn = duckdb_connect()
        for table, frozen in sorted(manifest.get("tables", {}).items()):
            sample = None
            for o in manifest["objects"]:
                if o["key"].startswith("facts/%s/" % table):
                    p = os.path.join(rdir, o["key"])
                    if os.path.isfile(p):
                        sample = p
                        break
            if sample is None:
                failures.append("no cached facts file for table %s" % table)
                continue
            if sample.endswith(".parquet"):
                q = "DESCRIBE SELECT * FROM read_parquet(?)"
            else:
                q = "DESCRIBE SELECT * FROM read_csv_auto(?)"
            cols = [r[0] for r in conn.execute(q, [sample]).fetchall()]
            if cols != frozen["columns"]:
                failures.append("schema drift in %s (cached vs frozen)"
                                % table)
            tables_out[table] = {
                "tl1_ladder_columns_present":
                    all(c in cols for c in LADDER_COLUMNS),
                "ws_sid_present": "ws_sid" in cols,
                "ws_seq_present": "ws_seq" in cols,
            }
        conn.close()

    # 5) capture-gap evidence re-count
    gap_key = "quality/capture_gaps_%s.csv" % date
    gap_path = os.path.join(rdir, gap_key)
    l1 = manifest.get("channels", {}).get("orderbooks_l1", {})
    if os.path.isfile(gap_path):
        with open(gap_path) as f:
            n_gaps = max(0, sum(1 for _ in f) - 1)
        if (l1.get("gap_intervals_for_date") is not None
                and n_gaps != l1["gap_intervals_for_date"]):
            failures.append("gap interval count %d != manifest %s"
                            % (n_gaps, l1["gap_intervals_for_date"]))
    elif l1.get("gap_intervals_for_date") is not None:
        failures.append("capture-gap evidence missing from cache")

    if failures:
        return _fail(cache, rid, failures)

    marker = {
        "release_id": rid, "date": date, "verified_at_utc": _now(),
        "evidence_tier": manifest.get("evidence_tier"),
        "evidence_tier_basis": manifest.get("evidence_tier_basis"),
        "publication_state_sha256":
            manifest.get("publication_state_sha256"),
        "version_binding_mode":
            (manifest.get("version_binding") or {}).get("mode"),
        "quarantined_legacy_override": bool(quarantined),
        # P0-4: frozen publication/correction state for view ordering —
        # NEVER local verification time
        "corrections_total":
            int((manifest.get("corrections") or {})
                .get("included_files") or 0)
            + int((manifest.get("corrections") or {})
                  .get("ledger_day_entries") or 0),
        "generated_at_utc": manifest.get("generated_at_utc"),
        "rfq_included":
            (manifest.get("channels", {}).get("rfq", {}).get("status")
             == "INCLUDED_SEALED_RAW"),
        "tl1_status": manifest.get("tl1_status"),
        "seal_sha256": manifest["seal"]["sha256"],
        "objects_verified": n_ok, "bytes_verified": bytes_ok,
        "rfq_status": rfq_status,
        "tables": tables_out,
    }
    with open(verified_marker(cache, rid), "w") as f:
        json.dump(marker, f, indent=2, sort_keys=True)
    contained_remove(os.path.join(rdir, ".FAILED.json"), cache)
    ch = manifest.get("channels", {})
    print("[verify] PASS %s — tier=%s tl1=%s binding=%s objects=%d "
          "(%.1f MB)%s"
          % (rid, marker["evidence_tier"], marker["tl1_status"],
             marker["version_binding_mode"], n_ok, bytes_ok / 1e6,
             "  [%s]" % QUARANTINE_BRAND if quarantined else ""))
    basis = manifest.get("evidence_tier_basis", {})
    if basis.get("downgrade_reasons"):
        print("  tier basis (DERIVED, fix 5): %s"
              % "; ".join(basis["downgrade_reasons"]))
    print("  L1: %s (gap intervals for date: %s)"
          % (ch.get("orderbooks_l1", {}).get("completeness", "?"),
             ch.get("orderbooks_l1", {}).get("gap_intervals_for_date")))
    print("  trades: identity=%s"
          % ch.get("trades", {}).get("identity", "?"))
    print("  L2: %s — %s"
          % (ch.get("orderbooks_l2", {}).get("status", "?"),
             ch.get("orderbooks_l2", {}).get("note",
                                             ch.get("orderbooks_l2", {})
                                             .get("reason", ""))))
    print("  RFQ: manifest=%s, local=%s"
          % (ch.get("rfq", {}).get("status", "?"), rfq_status))
    rebuild_view(cache)
    return 0


def rebuild_view(cache, include_non_confirmation=False):
    """Merged warehouse-shaped symlink view over VERIFIED releases.

    P0-3: by default this is the Phase-A ACTIVE research view — only clean
    SEALED_CONFIRMATION releases enter it; degraded/quarantined releases are
    excluded unless include_non_confirmation is passed explicitly (cmd_view
    --include-non-confirmation), and even then quarantined data stays
    branded in the provenance. P0-4: per-date selection ranks by the FROZEN
    publication/correction state (corrections_total, publisher
    generated_at_utc), never by local verification time.

    view/{facts,seals,dim,catalog,raw,quality}/… is what the Event
    Intelligence dashboard consumes via --data-root. Rebuilt from scratch on
    every call (symlinks only; never deletes cached data)."""
    view = os.path.join(cache, "view")
    contained_remove(view, cache, is_dir=True)
    by_date = {}
    rel_root = os.path.join(cache, "releases")
    if os.path.isdir(rel_root):
        for rid in sorted(os.listdir(rel_root)):
            marker = verified_marker(cache, rid)
            if not os.path.isfile(marker) or not _RELEASE_RE.match(rid):
                continue
            with open(marker) as f:
                m = json.load(f)
            # item 5: quarantined-legacy data enters the view ONLY via the
            # explicit override marker, and the provenance is branded
            d = m["date"]
            # P0-3: the Phase-A active research view holds ONLY clean
            # SEALED_CONFIRMATION releases by default
            if not include_non_confirmation and (
                    m.get("quarantined_legacy_override")
                    or m.get("evidence_tier") != "SEALED_CONFIRMATION"):
                continue
            # P0-4: ordering uses FROZEN publication/correction state
            # (corrections monotonically grow; generated_at_utc is the
            # publisher's frozen clock) — NEVER local verified_at time, so
            # re-verifying an older clean release can never displace a
            # newer correction. Legacy -rfq variants break exact ties.
            rank = (not m.get("quarantined_legacy_override"),
                    m.get("corrections_total") or 0,
                    m.get("generated_at_utc") or "",
                    bool(m.get("rfq_included")) or rid.endswith("-rfq"),
                    rid)
            if d not in by_date or rank > by_date[d]["_rank"]:
                by_date[d] = {"rid": rid, "_rank": rank, **m}
    os.makedirs(view, exist_ok=True)
    newest_date = max(by_date) if by_date else None
    for d, info in sorted(by_date.items()):
        rdir = cache_release_dir(cache, info["rid"])
        for base, _dirs, files in os.walk(rdir):
            for fn in files:
                src = os.path.join(base, fn)
                rel = os.path.relpath(src, rdir).replace(os.sep, "/")
                if rel in ("MANIFEST.json", ".VERIFIED.json", ".FAILED.json"):
                    continue
                if rel.startswith("seal/"):
                    dst = os.path.join(view, "seals", fn)
                elif rel.startswith("raw_rfq/"):
                    # new layout keeps the vault-relative date dir (cross-day
                    # receipt hours live under the NEXT day's directory);
                    # legacy releases carried bare basenames
                    sub = rel[len("raw_rfq/"):]
                    if sub.startswith("date="):
                        dst = os.path.join(view, "raw", sub)
                    else:
                        dst = os.path.join(view, "raw", "date=%s" % d, fn)
                elif rel.startswith("quality/"):
                    dst = os.path.join(view, "quality", "date=%s" % d, fn)
                elif rel.startswith("catalog/"):
                    if d != newest_date:
                        continue
                    dst = os.path.join(view, rel)
                elif rel.startswith("warehouse_manifest/"):
                    if d != newest_date:
                        continue
                    dst = os.path.join(view, "manifest.csv")
                else:  # facts/, dim/, corrections/
                    dst = os.path.join(view, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.lexists(dst):
                    # absolute target: a relative cache path must never
                    # produce dangling view links
                    os.symlink(os.path.abspath(src), dst)
    branded = {d: v["rid"] for d, v in by_date.items()
               if v.get("quarantined_legacy_override")}
    with open(os.path.join(view, ".view_provenance.json"), "w") as f:
        json.dump({"built_at_utc": _now(),
                   "verified_releases": {d: v["rid"]
                                         for d, v in by_date.items()},
                   "quarantined_legacy_overrides": branded,
                   "quarantine_brand": QUARANTINE_BRAND if branded else None,
                   "note": "symlink view over VERIFIED releases only; "
                           "rebuilt by tools/research_data.py"}, f, indent=2)
    return view


def cmd_view(cache, include_non_confirmation=False):
    view = rebuild_view(cache,
                        include_non_confirmation=include_non_confirmation)
    with open(os.path.join(view, ".view_provenance.json")) as f:
        prov = json.load(f)
    print("[view] %s — %d date(s)%s: %s"
          % (view, len(prov["verified_releases"]),
             " (INCLUDING NON-CONFIRMATION TIERS)"
             if include_non_confirmation else
             " (SEALED_CONFIRMATION only — active view)",
             ", ".join(sorted(prov["verified_releases"])) or "(none)"))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.environ.get(
        "KALSHI_RESEARCH_S3_ROOT") or load_env_file().get(
        "KALSHI_RESEARCH_S3_ROOT") or ROOT_DEFAULT)
    ap.add_argument("--cache", default=CACHE_DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory")
    pf = sub.add_parser("fetch")
    pf.add_argument("--release", required=True)
    pf.add_argument("--with-rfq", action="store_true",
                    help="also fetch sealed rfq raw (cost-bearing egress)")
    pf.add_argument("--allow-legacy-quarantined", action="store_true",
                    help="explicit override to read a QUARANTINED legacy "
                         "release (all outputs branded)")
    pv = sub.add_parser("verify")
    pv.add_argument("--release", required=True)
    pv.add_argument("--allow-legacy-quarantined", action="store_true",
                    help="explicit override to read a QUARANTINED legacy "
                         "release (all outputs branded)")
    pw = sub.add_parser("view")
    pw.add_argument("--include-non-confirmation", action="store_true",
                    help="P0-3: the default view is the Phase-A ACTIVE "
                         "research view (SEALED_CONFIRMATION only); this "
                         "flag re-includes degraded/branded releases")
    args = ap.parse_args(argv[1:])
    # remediation item 6: wrong credential mode = refuse before anything
    refuse_production_credentials()
    if args.cmd == "view":
        return cmd_view(args.cache,
                        include_non_confirmation=
                        args.include_non_confirmation)
    store = make_store(args.root)
    if args.cmd == "inventory":
        return cmd_inventory(store, args.cache)
    if args.cmd == "fetch":
        return cmd_fetch(store, args.cache, args.release, args.with_rfq,
                         allow_legacy=args.allow_legacy_quarantined)
    return cmd_verify(store, args.cache, args.release,
                      allow_legacy=args.allow_legacy_quarantined)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
