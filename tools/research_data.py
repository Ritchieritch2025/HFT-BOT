#!/usr/bin/env python3
"""PIPE-W05 Phase A: Mac-side research data CLI — inventory | fetch | verify | view.

Reads the dedicated research/ S3 prefix (immutable releases published by
tools/research_release.py as releases/<release_id>/ with MANIFEST.json LAST).
Release-id addressing only; a release without its MANIFEST.json is treated as
unpublished (torn) and is never exposed.

    python3 tools/research_data.py inventory
    python3 tools/research_data.py fetch  --release <release_id> [--with-rfq]
    python3 tools/research_data.py verify --release <release_id>
    python3 tools/research_data.py view

No aws CLI and no boto3 required: S3 access is stdlib SigV4 (ListObjectsV2 +
GetObject), read-only by construction. Credentials come from
    ~/.kalshi/research_s3.env.sh          (chmod 600; values never printed)
containing exactly:
    export AWS_ACCESS_KEY_ID=...          # the W05 research read-only key
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_DEFAULT_REGION=us-east-2   # optional (default us-east-2)
    export KALSHI_RESEARCH_S3_ROOT=...    # optional root override
The matching least-privilege IAM policy for the operator console is
docs/plan_releases/pipeline/W05_RESEARCH_READONLY_IAM_POLICY.json.
The root may also be a local directory (fixture tests / offline use).

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

Channel truth carried on every verify/inventory output (W05 amendment 4):
L1 = conflated change stream, never lossless (gap intervals alongside);
trades = trade-id identity, duplicates collapse downstream; L2 = NOT
RESEARCH-EXPOSABLE in Phase A (no L2 facts extraction; generic raw excluded
from research/ by amendment 1); RFQ = sealed raw form, operator cost switch.

Local caches under work/research_cache/ are prunable; S3, seals and
production are never touched (this tool holds a read-only credential).
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

ROOT_DEFAULT = "s3://kalshi-vault-ritcardo/research"
ENV_FILE = os.path.expanduser("~/.kalshi/research_s3.env.sh")
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
_RELEASE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})__seal-[0-9a-f]{12}(-rfq)?$")


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

def load_env_file(path=ENV_FILE):
    out = {}
    if not os.path.isfile(path):
        return out
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
            (ENV_FILE, POLICY_DOC))
    return key_id, secret, region


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

    def get_to(self, rel, dest):
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        with self._signed_request(key=self._full(rel)) as resp, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)
        os.replace(tmp, dest)

    def get_bytes(self, rel):
        with self._signed_request(key=self._full(rel)) as resp:
            return resp.read()

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

    def get_to(self, rel, dest):
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(os.path.join(self.root, rel), dest)

    def get_bytes(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()

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
    return os.path.join(cache, "releases", rid)


def verified_marker(cache, rid):
    return os.path.join(cache_release_dir(cache, rid), ".VERIFIED.json")


def read_manifest(cache, store, rid):
    cached = os.path.join(cache_release_dir(cache, rid), "MANIFEST.json")
    if os.path.isfile(cached):
        with open(cached) as f:
            return json.load(f)
    raw = store.get_bytes("releases/%s/MANIFEST.json" % rid)
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as f:
        f.write(raw)
    return json.loads(raw)


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
        tier = tl1 = rfq = "?"
        if r["exposed"]:
            m = read_manifest(cache, store, rid)
            tier, tl1 = m.get("evidence_tier", "?"), m.get("tl1_status", "?")
            rfq = m.get("channels", {}).get("rfq", {}).get("status", "?")
            status = "EXPOSED"
            if os.path.isfile(verified_marker(cache, rid)):
                status = "EXPOSED+VERIFIED_LOCALLY"
        rows.append((rid, date, status, len(r["objects"]),
                     r["bytes"] / 1e9, tier, tl1, rfq))
    if rows:
        print("%-32s %-11s %-28s %6s %9s %-20s %-8s %s"
              % ("release_id", "date", "status", "files", "GB", "tier",
                 "tl1", "rfq"))
        for row in rows:
            print("%-32s %-11s %-28s %6d %9.3f %-20s %-8s %s" % row)
        print("L2: NOT RESEARCH-EXPOSABLE in Phase A — no L2 facts "
              "extraction exists; generic raw (incl. l2_<HH>) is excluded "
              "from research/ by W05 addendum amendment 1.")
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


def cmd_fetch(store, cache, rid, with_rfq):
    releases = list_releases(store)
    if rid not in releases:
        raise SystemExit("unknown release_id %r (see inventory)" % rid)
    if not releases[rid]["exposed"]:
        raise SystemExit("release %s has no MANIFEST.json — torn/unpublished, "
                         "NOT exposed to research" % rid)
    manifest = read_manifest(cache, store, rid)
    rdir = cache_release_dir(cache, rid)
    fetched = skipped = 0
    for o in _iter_manifest_objects(manifest, with_rfq):
        dest = os.path.join(rdir, o["key"])
        if os.path.isfile(dest) and os.stat(dest).st_size == o["size"]:
            skipped += 1
            continue
        store.get_to("releases/%s/%s" % (rid, o["key"]), dest)
        fetched += 1
    print("[fetch] %s: %d objects fetched, %d already cached (rfq %s)"
          % (rid, fetched, skipped,
             "included" if with_rfq else "skipped — cost-bearing; use "
             "--with-rfq"))
    return cmd_verify(store, cache, rid)


def _fail(cache, rid, failures):
    rdir = cache_release_dir(cache, rid)
    marker = verified_marker(cache, rid)
    if os.path.isfile(marker):
        os.remove(marker)
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


def cmd_verify(store, cache, rid):
    manifest = read_manifest(cache, store, rid)
    rdir = cache_release_dir(cache, rid)
    date = manifest["date"]
    failures = []
    rfq_status, rfq_present = "ABSENT_FROM_RELEASE", False

    # 1) every frozen object: present + size + sha256 (raw_rfq may be unfetched)
    n_ok = bytes_ok = 0
    for o in manifest["objects"]:
        path = os.path.join(rdir, o["key"])
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
        "tl1_status": manifest.get("tl1_status"),
        "seal_sha256": manifest["seal"]["sha256"],
        "objects_verified": n_ok, "bytes_verified": bytes_ok,
        "rfq_status": rfq_status,
        "tables": tables_out,
    }
    with open(verified_marker(cache, rid), "w") as f:
        json.dump(marker, f, indent=2, sort_keys=True)
    failed = os.path.join(rdir, ".FAILED.json")
    if os.path.isfile(failed):
        os.remove(failed)
    ch = manifest.get("channels", {})
    print("[verify] PASS %s — tier=%s tl1=%s objects=%d (%.1f MB)"
          % (rid, marker["evidence_tier"], marker["tl1_status"], n_ok,
             bytes_ok / 1e6))
    print("  L1: %s (gap intervals for date: %s)"
          % (ch.get("orderbooks_l1", {}).get("completeness", "?"),
             ch.get("orderbooks_l1", {}).get("gap_intervals_for_date")))
    print("  trades: identity=%s"
          % ch.get("trades", {}).get("identity", "?"))
    print("  L2: %s — %s"
          % (ch.get("orderbooks_l2", {}).get("status", "?"),
             ch.get("orderbooks_l2", {}).get("reason", "")))
    print("  RFQ: manifest=%s, local=%s"
          % (ch.get("rfq", {}).get("status", "?"), rfq_status))
    rebuild_view(cache)
    return 0


def rebuild_view(cache):
    """Merged warehouse-shaped symlink view over VERIFIED releases only.

    view/{facts,seals,dim,catalog,raw,quality}/… is what the Event
    Intelligence dashboard consumes via --data-root. Rebuilt from scratch on
    every call (symlinks only; never deletes cached data). Newest verified
    release wins per date; catalog comes from the newest verified date.
    """
    view = os.path.join(cache, "view")
    shutil.rmtree(view, ignore_errors=True)
    by_date = {}
    rel_root = os.path.join(cache, "releases")
    if os.path.isdir(rel_root):
        for rid in sorted(os.listdir(rel_root)):
            marker = verified_marker(cache, rid)
            if not os.path.isfile(marker) or not _RELEASE_RE.match(rid):
                continue
            with open(marker) as f:
                m = json.load(f)
            d = m["date"]
            # newest verified wins; on a same-second tie the -rfq variant
            # wins (it is the strict superset of its base release)
            rank = (m["verified_at_utc"], rid.endswith("-rfq"), rid)
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
                    os.symlink(src, dst)
    with open(os.path.join(view, ".view_provenance.json"), "w") as f:
        json.dump({"built_at_utc": _now(),
                   "verified_releases": {d: v["rid"]
                                         for d, v in by_date.items()},
                   "note": "symlink view over VERIFIED releases only; "
                           "rebuilt by tools/research_data.py"}, f, indent=2)
    return view


def cmd_view(cache):
    view = rebuild_view(cache)
    with open(os.path.join(view, ".view_provenance.json")) as f:
        prov = json.load(f)
    print("[view] %s — %d verified date(s): %s"
          % (view, len(prov["verified_releases"]),
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
    pv = sub.add_parser("verify")
    pv.add_argument("--release", required=True)
    sub.add_parser("view")
    args = ap.parse_args(argv[1:])
    if args.cmd == "view":
        return cmd_view(args.cache)
    store = make_store(args.root)
    if args.cmd == "inventory":
        return cmd_inventory(store, args.cache)
    if args.cmd == "fetch":
        return cmd_fetch(store, args.cache, args.release, args.with_rfq)
    return cmd_verify(store, args.cache, args.release)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
