#!/usr/bin/env python3
"""Ingest the Kalshi reference/dimension tables into the warehouse — RAW.

Stores every field the API returns (no derivation, no feature engineering) so the
data can be classified/joined later. These dimension tables give the taxonomy the
live firehose facts (orderbooks_l1, trades) are sliced by:

  work/warehouse/catalog/series/part-00000.parquet       (dim_series)
  work/warehouse/catalog/events/part-00000.parquet       (dim_event)
  work/warehouse/catalog/markets/part-00000.parquet      (dim_market, open snapshot)
  work/warehouse/catalog/settlements/part-00000.parquet  (dim_settlement, settled markets)

Snowflake key chain (Kalshi ticker convention):
  market_ticker = {event_ticker}-{outcome};  event_ticker = {series_ticker}-{event_id}

The category-specific structure lives in ORIGINAL fields:
  league/region  -> series.category + series.tags + series.title
  game/date      -> event.title / sub_title / event_ticker
  time           -> event/market close_time, expiration_time
  price range    -> market.strike_type / floor_strike / cap_strike / yes_sub_title
  price + book   -> orderbooks_l1 / trades (live)

stdlib + duckdb only. Read-only public endpoints (no auth).
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publication_generation as pg  # noqa: E402

BASE = "https://external-api.kalshi.com/trade-api/v2"

# shared pacing state across all requests in this process
_LAST_REQUEST_MONO = [0.0]

CATALOG_MEMBERS = {
    "series": "series/part-00000.parquet",
    "events": "events/part-00000.parquet",
    "markets": "markets/part-00000.parquet",
    "settlements": "settlements/part-00000.parquet",
    "series_classified": "series_classified/part-00000.parquet",
}
CATALOG_REQUIRED = {
    CATALOG_MEMBERS[name] for name in ("series", "events", "markets")}


def paced_open(url, timeout=45, min_interval_s=0.05, max_tries=6,
               opener=None, sleeper=None, clock=None):
    """Rate-paced urlopen with exponential backoff on HTTP 429.

    W-A4 cutover lesson (2026-07-09): on EC2 (same-region, <1 ms RTT) the
    sequential pagination loop fires ~30x more requests/second than on the
    ~30 ms-RTT Mac and burns through the read-token burst -> HTTP 429. The
    Mac never hit this only because its network latency throttled it for
    free. So: every request waits min_interval_s (default 50 ms, <=20 req/s
    vs the 300 tok/s refill) and 429 retries back off 1,2,4,8,16 s. Non-429
    errors raise immediately (fail-closed, never masked).
    """
    opener = opener or urllib.request.urlopen
    sleeper = sleeper or time.sleep
    clock = clock or time.monotonic
    for attempt in range(max_tries):
        wait = _LAST_REQUEST_MONO[0] + min_interval_s - clock()
        if wait > 0:
            sleeper(wait)
        _LAST_REQUEST_MONO[0] = clock()
        try:
            return opener(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == max_tries - 1:
                raise
            sleeper(2 ** attempt)
    raise AssertionError("unreachable")


def fetch_all(path, key, limit=200, params=None, cap_pages=400, log=None):
    items, cur, pages = [], None, 0
    base_q = dict(params or {})
    base_q["limit"] = limit
    while pages < cap_pages:
        q = dict(base_q)
        if cur:
            q["cursor"] = cur
        url = "%s%s?%s" % (BASE, path, urllib.parse.urlencode(q))
        with paced_open(url, timeout=45) as r:
            d = json.load(r)
        batch = d.get(key, [])
        items.extend(batch)
        pages += 1
        cur = d.get("cursor")
        if log and pages % 10 == 0:
            log("    %s: %d rows / %d pages" % (key, len(items), pages))
        if not cur or not batch:
            break
    return items, (cur is not None and pages >= cap_pages)


def write_parquet_raw(rows, out_dir):
    """Write rows (list of dicts, ALL original fields preserved) to one parquet."""
    import duckdb
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "part-00000.parquet")
    if not rows:
        return 0
    tmp = tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False)
    try:
        for r in rows:
            tmp.write(json.dumps(r) + "\n")   # raw object, every field
        tmp.close()
        con = duckdb.connect()
        try:
            src = tmp.name.replace("'", "''")
            dst = out.replace("'", "''")
            # union_by_name so heterogeneous objects keep every field they carry.
            # sample_size=-1 (W-A5, 2026-07-10): schema inference must scan ALL
            # rows — the 400k-event crawl has second-precision timestamps in the
            # head and microsecond-precision ones deep in the tail; head-only
            # sampling inferred "%Y-%m-%dT%H:%M:%SZ" and hard-crashed at row
            # 363,172. Full sampling makes mixed-format columns fall back to
            # VARCHAR — lossless, correct for a store-it-raw dim layer.
            con.execute(
                "COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited', "
                "union_by_name=true, maximum_object_size=1048576, sample_size=-1)) "
                "TO '%s' (FORMAT PARQUET)" % (src, dst))
        finally:
            con.close()
        return len(rows)
    finally:
        os.unlink(tmp.name)


def _catalog_file_set(warehouse):
    """Return the strict allowlisted canonical catalog member set."""
    catalog = os.path.join(warehouse, "catalog")
    if os.path.lexists(catalog) and os.path.islink(catalog):
        raise pg.GenerationError("catalog root is a symlink")
    actual = set()
    if not os.path.isdir(catalog):
        return actual
    allowed = set(CATALOG_MEMBERS.values())
    for base, dirs, files in os.walk(catalog):
        dirs.sort()
        for name in dirs:
            path = os.path.join(base, name)
            if os.path.islink(path):
                raise pg.GenerationError("catalog directory is a symlink: %s" % path)
        for name in sorted(files):
            path = os.path.join(base, name)
            if os.path.islink(path):
                raise pg.GenerationError("catalog member is a symlink: %s" % path)
            rel = os.path.relpath(path, catalog).replace(os.sep, "/")
            if rel not in allowed:
                raise pg.GenerationError("unexpected catalog member %s" % rel)
            actual.add(rel)
    return actual


def _link_preserved_catalog(warehouse, stage_root, refreshed):
    """Pin unchanged members into the stage while holding catalog shared."""
    actual = _catalog_file_set(warehouse)
    if actual:
        manifest, state = pg.verified_or_synthesized_manifest(
            warehouse, "catalog", ["catalog/" + rel for rel in actual])
    else:
        manifest, state = None, "EMPTY"
    for rel in sorted(actual - set(refreshed)):
        src = os.path.join(warehouse, "catalog", *rel.split("/"))
        dst = os.path.join(stage_root, "catalog", *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.link(src, dst)
    return manifest, state


def _assert_catalog_base_unchanged(warehouse, base_manifest, base_state):
    current = _catalog_file_set(warehouse)
    if base_state == "EMPTY":
        if current:
            raise pg.GenerationError(
                "catalog appeared while a new generation was staged")
        return
    expected = ["catalog/" + rel for rel in current]
    if base_state == "PRODUCER_MANIFEST_VERIFIED":
        now = pg.load_manifest(
            warehouse, "catalog", expected_paths=expected,
            verify_files=False, allow_missing=False)
    else:
        now, _state = pg.verified_or_synthesized_manifest(
            warehouse, "catalog", expected)
    if now["generation_id"] != base_manifest["generation_id"]:
        raise pg.GenerationError("catalog changed while a new generation was staged")


def _publish_catalog_generation(warehouse, stage_root, refreshed):
    with pg.generation_locks(warehouse, {"catalog": "shared"}, timeout=30.0):
        base_manifest, base_state = _link_preserved_catalog(
            warehouse, stage_root, refreshed)

    staged_rel = set()
    catalog_stage = os.path.join(stage_root, "catalog")
    for base, dirs, files in os.walk(catalog_stage):
        dirs.sort()
        for name in dirs:
            if os.path.islink(os.path.join(base, name)):
                raise pg.GenerationError("staged catalog directory is a symlink")
        for name in sorted(files):
            full = os.path.join(base, name)
            if os.path.islink(full):
                raise pg.GenerationError("staged catalog member is a symlink")
            rel = os.path.relpath(full, catalog_stage).replace(os.sep, "/")
            if rel not in set(CATALOG_MEMBERS.values()):
                raise pg.GenerationError("unexpected staged catalog member %s" % rel)
            staged_rel.add(rel)
    missing = CATALOG_REQUIRED - staged_rel
    if missing:
        raise pg.GenerationError("catalog generation missing %s" % sorted(missing))
    generation_paths = ["catalog/" + rel for rel in sorted(staged_rel)]
    prior_files = ({row["relative_path"]: row
                    for row in base_manifest["files"]}
                   if base_manifest is not None else {})
    generation_files = []
    for rel in generation_paths:
        catalog_rel = rel[len("catalog/"):]
        if catalog_rel not in refreshed and rel in prior_files:
            generation_files.append(prior_files[rel])
        else:
            generation_files.extend(pg.attest_files(stage_root, [rel]))
    manifest = pg.build_manifest("catalog", generation_files)
    staged = {
        rel: os.path.join(stage_root, *rel.split("/"))
        for rel in generation_paths}
    with pg.generation_locks(warehouse, {"catalog": "exclusive"}, timeout=30.0):
        _assert_catalog_base_unchanged(warehouse, base_manifest, base_state)
        pg.publish_transaction(warehouse, staged, manifest)
    return manifest


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", default="work/warehouse")
    ap.add_argument("--settled-pages", type=int, default=40,
                    help="pages of settled markets to keep (0 = skip settlements)")
    ap.add_argument("--skip-markets", action="store_true",
                    help="only refresh series + events (skip the ~60k markets snapshot)")
    args = ap.parse_args(argv[1:])

    def log(m):
        print(m); sys.stdout.flush()

    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("error: needs the duckdb module (pip install duckdb)", file=sys.stderr)
        return 1

    warehouse = os.path.abspath(args.warehouse)
    stage_parent = os.path.join(warehouse, ".publication-stage")
    if os.path.lexists(stage_parent) and os.path.islink(stage_parent):
        raise pg.GenerationError("publication stage is a symlink")
    os.makedirs(stage_parent, mode=0o700, exist_ok=True)
    stage_root = tempfile.mkdtemp(prefix="catalog-", dir=stage_parent)
    refreshed = set()
    try:
        # Fetch every requested table before touching the canonical catalog.
        # A network error therefore leaves the previous complete generation.
        log("[1/4] series (dim_series) ...")
        series, _ = fetch_all("/series/", "series", limit=1000, log=log)
        if not series:
            raise pg.GenerationError("series crawl returned no rows")
        n = write_parquet_raw(
            series, os.path.join(stage_root, "catalog", "series"))
        refreshed.add(CATALOG_MEMBERS["series"])
        cats = sorted({s.get("category") for s in series if s.get("category")})
        log("      %d series, %d categories (staged)" % (n, len(cats)))

        log("[2/4] events (dim_event) ...")
        # cap_pages raised 400->2000 (W-A5 audit item, 2026-07-09): the crawl hit
        # exactly 80,000 = 400x200 — a hard-cap truncation, not the real total.
        events, trunc = fetch_all("/events/", "events", limit=200, log=log,
                                  cap_pages=2000)
        if not events:
            raise pg.GenerationError("events crawl returned no rows")
        n = write_parquet_raw(
            events, os.path.join(stage_root, "catalog", "events"))
        refreshed.add(CATALOG_MEMBERS["events"])
        log("      %d events%s (staged)" %
            (n, " (capped)" if trunc else ""))

        if not args.skip_markets:
            log("[3/4] markets (dim_market, open snapshot) ...")
            markets, trunc = fetch_all(
                "/markets", "markets", limit=200,
                params={"status": "open"}, log=log, cap_pages=2000)
            if not markets:
                raise pg.GenerationError("open-markets crawl returned no rows")
            n = write_parquet_raw(
                markets, os.path.join(stage_root, "catalog", "markets"))
            refreshed.add(CATALOG_MEMBERS["markets"])
            log("      %d open markets%s (staged)" %
                (n, " (capped)" if trunc else ""))
        else:
            log("[3/4] markets — preserved from current generation")

        if args.settled_pages > 0:
            log("[4/4] settlements (dim_settlement, settled markets) ...")
            settled, trunc = fetch_all(
                "/markets", "markets", limit=200,
                params={"status": "settled"},
                cap_pages=args.settled_pages, log=log)
            if settled:
                n = write_parquet_raw(
                    settled,
                    os.path.join(stage_root, "catalog", "settlements"))
                refreshed.add(CATALOG_MEMBERS["settlements"])
            else:
                n = 0
            log("      %d settled markets%s%s" % (
                n, " (capped — increase --settled-pages)" if trunc else "",
                " (old optional snapshot preserved)" if not settled else
                " (staged)"))
        else:
            log("[4/4] settlements — preserved from current generation")

        manifest = _publish_catalog_generation(
            warehouse, stage_root, refreshed)
        log("reference sync complete -> %s [generation %s]" %
            (os.path.join(warehouse, "catalog"),
             manifest["generation_id"][:16]))
        return 0
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
