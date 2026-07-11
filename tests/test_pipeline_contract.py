#!/usr/bin/env python3
"""WP-04: Permanent Acceptance Suite — the pipeline contract, pytest-native.

Every past ad-hoc acceptance criterion becomes a permanent test on synthetic
fixtures (no network, no real pipeline data; production modules are read-only).

Adoption per EXECUTION_PLAN amendment A1 (migrate/extend, never rewrite or
double-run): change-only, heartbeat and kill/restart are ADOPTED from
tests/test_ingest.py by IMPORTING its fixture helpers (tick / trade /
make_warehouse / new_ingester) and asserting on DIFFERENT fixture values
(different markets, prices, tick counts, cut offsets), so both suites bite
independently — a regression that slips past one set of magic numbers still
trips the other. The legacy self-runners remain canonical under `make check`
(tests/conftest.py keeps them out of pytest collection; importing them here
does not run them — their main() is __main__-guarded).

Folded in per the plan: WP-01 E4-integrity tests (test_e4_roundtrip,
test_fractional_qty) and WP-02 residue (test_policy_from_config,
test_unknown_category_warns).

stdlib + duckdb + pytest only.
"""
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time

import duckdb
import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, TESTS)

import build_classification as bc  # noqa: E402
import event_pack  # noqa: E402
import export_day  # noqa: E402
import ingest  # noqa: E402
import mm_backtest  # noqa: E402
import mm_calibrate  # noqa: E402
import mm_research  # noqa: E402
import mm_scan  # noqa: E402
import test_ingest as ti  # noqa: E402  (helpers only; not collected — conftest)
import warehouse  # noqa: E402

HOUR_US = 3_600_000_000
# Fixture epoch: 10 minutes into an hour (distinct from test_ingest's T0 so the
# adopted tests bite on independent values). 1_784_008_800_000_000 is an exact
# hour start (2026-07-13T10:00Z-ish); +600s keeps multi-minute tick runs inside
# one hour unless a test crosses hours on purpose.
T0 = 1_784_008_800_000_000 + 600_000_000


def _q(obj, sql):
    con = obj.con if isinstance(obj, ingest.Ingester) else obj
    return con.execute(sql).fetchall()


def _raw(msg_type, msg, ts_us):
    """One capture NDJSON line for an arbitrary WS frame (custom field values)."""
    return json.dumps({"recv_wall_ns": ts_us * 1000,
                       "raw": json.dumps({"type": msg_type, "msg": msg})})


def _day_us(day, hour=12, sec=0):
    base = datetime.datetime(day.year, day.month, day.day, hour,
                             tzinfo=datetime.timezone.utc)
    return int((base + datetime.timedelta(seconds=sec)).timestamp() * 1_000_000)


def _release_warehouse():
    """Drop warehouse.load()'s cached connection + read-only staging attach.

    load() attaches every staging DB as the single alias `stg` on a cached
    connection; with per-test tmp staging paths the second test would hit a
    duplicate-alias retry loop, and DuckDB's single-writer rule would block
    any later in-process/subprocess writer while the read-only attach is held
    (same pattern as tests/test_export_day.py)."""
    if warehouse._CON is not None:
        try:
            warehouse._CON.close()
        except Exception:
            pass
    warehouse._CON = None
    warehouse._ATTACHED.clear()
    warehouse._VALIDATED_ARCHIVES.clear()
    warehouse._VALIDATED_RAW.clear()


@pytest.fixture(autouse=True)
def _isolated_warehouse_con():
    _release_warehouse()
    yield
    _release_warehouse()


def _cls_parquet(wh, rows):
    """Pinned classification dim fixture (generalizes test_ingest.make_warehouse
    to arbitrary series; needed because export/routing fixtures want a Sports
    series the legacy helper does not carry)."""
    out = os.path.join(wh, "catalog", "series_classified")
    os.makedirs(out, exist_ok=True)
    nd = os.path.join(wh, "_cls.ndjson")
    with open(nd, "w") as f:
        for st, cat, sub, grp, kl in rows:
            f.write(json.dumps({"series_ticker": st, "category": cat,
                                "subcategory": sub, "group": grp,
                                "record_class": kl}) + "\n")
    duckdb.connect().execute(
        "COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
        "TO '%s' (FORMAT PARQUET)"
        % (nd.replace("'", "''"),
           os.path.join(out, "part-00000.parquet").replace("'", "''")))
    os.unlink(nd)


EXPORT_CLS = [
    ("KXBTC", "Crypto", "BTC", "BTC", "A"),
    ("KXMLB", "Sports", "MLB", "MLB", "A"),
    ("KXACME", "Companies", "_none", "ACME", "B"),
]


def _export_env(wh):
    env = dict(os.environ)
    env.update(WAREHOUSE_ROOT=wh,
               STAGING_DB=os.path.join(wh, "staging.duckdb"),
               ARCHIVE_ROOT=os.path.join(wh, "facts"),
               RAW_ROOT=os.path.join(wh, "raw"))
    return env


def _ingest_lines(wh, lines, name="cap.ndjson"):
    cap = os.path.join(wh, name)
    os.makedirs(os.path.dirname(cap), exist_ok=True)
    with open(cap, "w") as f:
        f.write("\n".join(lines) + "\n")
    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    ingest.Ingester(con, wh).process_file(cap)
    con.close()


@pytest.fixture
def exported_day(tmp_path):
    """Synthetic staging with yesterday + today rows; yesterday archived via the
    real tools/export_day.py subprocess (fixture-mocked roots through env vars,
    per the tests/test_export_day.py pattern)."""
    wh = str(tmp_path / "warehouse")
    _cls_parquet(wh, EXPORT_CLS)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    yd = today - datetime.timedelta(days=1)
    yesterday_lines = [
        ti.tick("KXBTC-26DEC31-B75", _day_us(yd, 9), 0.0325, 0.0450),
        ti.tick("KXBTC-26DEC31-B75", _day_us(yd, 9, 90), 0.0330, 0.0450),
        ti.trade("KXMLB-26JUL06-BOS", _day_us(yd, 10), "wp04-y1"),
        ti.trade("KXMLB-26JUL06-BOS", _day_us(yd, 10, 30), "wp04-y2"),
        ti.trade("KXACME-26JUL06-YES", _day_us(yd, 11), "wp04-y3"),
        ti.trade("KXUNKNOWN-26JUL06-YES", _day_us(yd, 12), "wp04-y4"),
    ]
    today_lines = [
        ti.tick("KXBTC-26DEC31-B75", _day_us(today, 0, 90), 0.0500, 0.0600),
        ti.trade("KXMLB-26JUL07-CHC", _day_us(today, 0, 95), "wp04-t1"),
    ]
    _ingest_lines(wh, yesterday_lines,
                  name=os.path.join("raw", "date=%s" % yd.isoformat(),
                                    "firehose_00.ndjson"))
    _ingest_lines(wh, today_lines,
                  name=os.path.join("raw", "date=%s" % today.isoformat(),
                                    "firehose_00.ndjson"))
    _ingest_lines(wh, [_raw("subscribed", {}, _day_us(today, 1))],
                  name=os.path.join("raw", "date=%s" % today.isoformat(),
                                    "firehose_01.ndjson"))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
                        "--date", yd.isoformat()], env=_export_env(wh),
                       capture_output=True, text=True)
    assert r.returncode == 0 and "EXPORT PASS" in r.stdout, r.stdout + r.stderr
    seal = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--seal"], env=_export_env(wh),
        capture_output=True, text=True)
    assert seal.returncode == 0 and "DAY SEAL PASS" in seal.stdout, \
        seal.stdout + seal.stderr
    return {"wh": wh, "yd": yd, "today": today}


# ───────────────────────── new contracts ─────────────────────────

def test_every_byte_accounted(tmp_path):
    """Checkpoint offsets cover 100% of the complete-line bytes of EVERY raw
    file — main log AND rotation shard — with the only unaccounted bytes being
    a partial trailing line, which is picked up exactly once when completed
    (the 31-minute rotation-shard / silent-truncation failure class)."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    ing = ti.new_ingester(tmp, wh)
    mt1, mt2 = "KXBTC-26DEC31-B41", "KXBTC-26DEC31-B42"

    complete = [ti.tick(mt1, T0 + i * 60_000_000, 0.20 + i / 100, 0.30)
                for i in range(5)]
    tail = ti.tick(mt1, T0 + 5 * 60_000_000, 0.26, 0.30)
    cut = len(tail) // 2
    raw_dir = os.path.join(tmp, "raw")
    os.makedirs(raw_dir)
    main_f = os.path.join(raw_dir, "firehose_10.ndjson")
    shard_f = os.path.join(raw_dir, "firehose_10.ndjson.1")  # rotation shard
    complete_bytes = len(("\n".join(complete) + "\n").encode())
    with open(main_f, "w") as f:                # partial trailing line, no \n
        f.write("\n".join(complete) + "\n" + tail[:cut])
    with open(shard_f, "w") as f:
        f.write("\n".join(ti.tick(mt2, T0 + (6 + i) * 60_000_000,
                                  0.50 + i / 100, 0.60) for i in range(3)) + "\n")

    ing.process_file(main_f)
    ing.process_file(shard_f)
    ck = dict(_q(ing, "SELECT file, byte_offset FROM checkpoint"))

    # 100% of complete-line bytes accounted for, on both files
    assert ck[os.path.abspath(main_f)] == complete_bytes
    assert ck[os.path.abspath(shard_f)] == os.path.getsize(shard_f)
    # the ONLY unprocessed gap is the incomplete tail (excluded until completed)
    assert os.path.getsize(main_f) - ck[os.path.abspath(main_f)] == cut

    # capture completes the line -> next cycle covers 100%, tail counted ONCE
    with open(main_f, "a") as f:
        f.write(tail[cut:] + "\n")
    ing.process_file(main_f)
    ck2 = dict(_q(ing, "SELECT file, byte_offset FROM checkpoint"))
    assert ck2[os.path.abspath(main_f)] == os.path.getsize(main_f)
    n1 = _q(ing, "SELECT count(*) FROM orderbooks_l1 WHERE market_ticker='%s'" % mt1)[0][0]
    n2 = _q(ing, "SELECT count(*) FROM orderbooks_l1 WHERE market_ticker='%s'" % mt2)[0][0]
    assert (n1, n2) == (6, 3)          # every distinct tick staged exactly once
    dup = _q(ing, "SELECT count(*) FROM (SELECT DISTINCT * FROM orderbooks_l1)")[0][0]
    assert dup == n1 + n2
    ing.con.close()


def test_league_parse():
    """League derivation per tools/build_classification.py derive_group.

    Mapping note (plan wording -> actual mechanism): the plan says unknown
    prefix -> "_unknown + logged warning, never a guess". The implemented
    mechanism is: an unknown SPORTS family returns its family prefix WITH
    needs_review=True (routed to config/classification_review.csv — nothing is
    silently guessed, group_source records the derivation), and an unknown
    CATEGORY warns + defaults Class B (V16) — asserted separately in
    test_unknown_category_warns. This test pins that mechanism."""
    assert bc.derive_group("KXMLB", "Sports", "Baseball", "") == \
        ("MLB", "known_league", False)
    assert bc.derive_group("KXKBO", "Sports", "Baseball", "") == \
        ("KBO", "known_league", False)
    # NCAA-class: NCAABB is a known league token in its own right
    assert bc.derive_group("KXNCAABB", "Sports", "Basketball", "") == \
        ("NCAABB", "known_league", False)
    # longest-match wins: NCAAFB must not stop at the shorter NCAAF token
    assert bc.derive_group("KXNCAAFB", "Sports", "Football", "") == \
        ("NCAAFB", "known_league", False)
    # prefix of a known league inside a longer series body still matches
    assert bc.derive_group("KXMLBGAME", "Sports", "Baseball", "")[0] == "MLB"
    # unknown sports family -> family prefix + needs_review=True, NEVER a guess
    grp, src, review = bc.derive_group("KXQUIDDITCH", "Sports", "_none", "")
    assert (grp, src, review) == ("QUIDDITCH", "family_prefix", True)


@pytest.mark.xfail(
    strict=False,
    reason="settlements export not implemented — export_day.py TABLES covers "
           "orderbooks_l1/orderbooks_full/trades only; docs/warehouse_schema.md "
           "requires settlements partitioned by settlement date (capture ts kept "
           "as a column). BACKLOG 2026-07-07; out of WP-04's test-conversion "
           "scope to build (Operating Protocol rule 1).")
def test_settlement_partitioned_by_settled_date():
    """Contract (docs/warehouse_schema.md): settlements are exported as facts,
    partitioned by SETTLED date — not capture date. The exporter does not
    export settlements at all yet, so this documents the missing feature
    honestly (xfail) instead of faking a pass. When the feature lands, extend
    this test: fixture settlement captured on day D with settled_time on day
    D-1 must land under date=<D-1> with the capture ts kept as a column."""
    exported = {t for t, _ in export_day.TABLES}
    assert "settlements" in exported, \
        "export_day.py TABLES has no settlements fact table"


def test_denormalized_columns_present(exported_day):
    """Every archived fact row (l1 + trades) carries the denormalized hierarchy
    columns: category / subcategory / group / series_ticker / event_ticker."""
    wh, yd = exported_day["wh"], exported_day["yd"]
    con = duckdb.connect()
    cols = ["category", "subcategory", '"group"', "series_ticker", "event_ticker"]
    null_any = " OR ".join("%s IS NULL" % c for c in cols)

    l1_files = glob.glob(os.path.join(wh, "facts", "orderbooks_l1", "*", "*",
                                      "date=%s" % yd, "*.parquet"))
    tr_files = glob.glob(os.path.join(wh, "facts", "trades", "*", "*",
                                      "date=%s" % yd, "*.csv.gz"))
    assert l1_files and tr_files, (l1_files, tr_files)

    for f in l1_files:
        rel = "read_parquet('%s')" % f.replace("'", "''")
        n = con.execute("SELECT count(*) FROM %s" % rel).fetchone()[0]
        assert n > 0
        bad = con.execute("SELECT count(*) FROM %s WHERE %s" % (rel, null_any)).fetchone()[0]
        assert bad == 0, "l1 rows missing denormalized columns in %s" % f
    # value spot-check on the Crypto partition (columns present AND correct)
    l1 = con.execute(
        "SELECT DISTINCT category, subcategory, \"group\", series_ticker, event_ticker "
        "FROM read_parquet('%s')" % l1_files[0].replace("'", "''")).fetchall()
    assert l1 == [("Crypto", "BTC", "BTC", "KXBTC", "KXBTC-26DEC31")]

    seen_cats = set()
    for f in tr_files:
        rel = ("read_csv('%s', header=true, all_varchar=true, "
               "hive_partitioning=false)" % f.replace("'", "''"))
        n = con.execute("SELECT count(*) FROM %s" % rel).fetchone()[0]
        assert n > 0
        bad = con.execute("SELECT count(*) FROM %s WHERE %s" % (rel, null_any)).fetchone()[0]
        if "category=_unclassified" in f:
            assert bad == n  # intentional unknown-series safety-net fixture
        else:
            assert bad == 0, "trade rows missing denormalized columns in %s" % f
        seen_cats |= {r[0] for r in con.execute(
            "SELECT DISTINCT category FROM %s" % rel).fetchall()}
    # both classes plus the intentional unknown-category safety-net are exported.
    assert seen_cats == {"Sports", "Companies", None}


def test_load_routing(exported_day):
    """load() routes a PAST (archived) day to the archive path and TODAY to
    live staging, never double-counting the overlap day."""
    wh, yd, today = exported_day["wh"], exported_day["yd"], exported_day["today"]

    n_yd = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                          warehouse=wh).count("*").fetchone()[0]
    assert n_yd == 4  # wp04-y1/y2/y3 + unknown-category y4 from the archive

    # PROOF past reads the ARCHIVE: a late yesterday row ingested into staging
    # AFTER the export must NOT change what load() returns for that day
    # (archived days are final; staging is excluded for them).
    _release_warehouse()
    _ingest_lines(wh, [ti.trade("KXMLB-26JUL06-BOS", _day_us(yd, 13), "wp04-late")],
                  name="late.ndjson")
    n_yd2 = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                           warehouse=wh).count("*").fetchone()[0]
    assert n_yd2 == 4

    # PROOF today reads STAGING: today was never exported, yet it is queryable,
    # and a fresh staged row shows up live.
    n_today = warehouse.load("trades", start=today.isoformat(), end=today.isoformat(),
                             warehouse=wh).count("*").fetchone()[0]
    assert n_today == 1  # wp04-t1
    _release_warehouse()
    _ingest_lines(wh, [ti.trade("KXMLB-26JUL07-CHC", _day_us(today, 0, 99), "wp04-t2")],
                  name="late2.ndjson")
    n_today2 = warehouse.load("trades", start=today.isoformat(), end=today.isoformat(),
                              warehouse=wh).count("*").fetchone()[0]
    assert n_today2 == 2

    # no double count across the overlap (yesterday exists in BOTH stores)
    n_all = warehouse.load("trades", warehouse=wh).count("*").fetchone()[0]
    assert n_all == 4 + 2


def test_archive_only_load_never_locks_live_staging(exported_day):
    """Completed-day research must not ATTACH the live writer database.

    The 2026-07-11 production incident was caused by mm_calibrate retaining a
    READ_ONLY staging attach for an already-final archived day.  DuckDB then
    refused the ingest writer and the watchdog crash-looped it.  Archive-only
    reads must leave the staging path immediately writable while the research
    relation remains alive.
    """
    wh, yd = exported_day["wh"], exported_day["yd"]
    _release_warehouse()
    rel = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                         warehouse=wh, archive_only=True)
    assert rel.count("*").fetchone()[0] == 4
    assert not warehouse._ATTACHED

    staging = os.path.join(wh, "staging.duckdb")
    writer = duckdb.connect(staging)
    writer.execute("CHECKPOINT")
    writer.close()


def test_archive_only_transition_releases_prior_live_attach(exported_day):
    """Switching a long-lived process to sealed mode releases staging."""
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    live = warehouse.load("trades", start=today.isoformat(), end=today.isoformat(),
                          warehouse=wh)
    assert live.count("*").fetchone()[0] == 1
    assert warehouse._ATTACHED
    sealed = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                            warehouse=wh, archive_only=True)
    assert sealed.count("*").fetchone()[0] == 4
    assert not warehouse._ATTACHED
    writer = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    writer.close()


def test_ingest_entrypoint_waits_out_initial_reader_lock(exported_day, tmp_path):
    """The CLI's FIRST writer open uses retry, not a bare connect.

    This is intentionally a subprocess test: testing connect_with_retry alone
    did not catch that ingest.main bypassed it before constructing Ingester.
    """
    wh = exported_day["wh"]
    staging = os.path.join(wh, "staging.duckdb")
    cap = tmp_path / "empty.ndjson"
    cap.write_text("\n")
    _release_warehouse()
    holder = duckdb.connect(staging, read_only=True)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "tools", "ingest.py"),
         "--warehouse", wh, "--staging", staging, str(cap)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(0.35)
        assert proc.poll() is None, proc.communicate(timeout=2)
    finally:
        holder.close()
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, out + err


def test_ingest_connect_does_not_retry_non_lock_failures(monkeypatch):
    class BrokenDuckDB:
        @staticmethod
        def connect(_path):
            raise RuntimeError("permission denied")

    monkeypatch.setattr(ingest.time, "sleep",
                        lambda _s: pytest.fail("non-lock error was retried"))
    with pytest.raises(RuntimeError, match="permission denied"):
        ingest.connect_with_retry(BrokenDuckDB, "/bad", attempts=30, sleep_s=5)


def test_fact_rows_and_checkpoint_commit_atomically(tmp_path):
    """Crash-window injection: facts cannot commit before their checkpoint."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    db_rel = "atomic.duckdb"
    ing = ti.new_ingester(tmp, wh, db=db_rel)
    real = ing.con

    class FailCheckpointOnce:
        def __init__(self, con):
            self.con, self.armed = con, True

        def execute(self, sql, *args, **kwargs):
            if self.armed and sql.lstrip().startswith("INSERT INTO checkpoint"):
                self.armed = False
                raise RuntimeError("injected crash before checkpoint")
            return self.con.execute(sql, *args, **kwargs)

        def executemany(self, *args, **kwargs):
            return self.con.executemany(*args, **kwargs)

    ing.con = FailCheckpointOnce(real)
    cap = os.path.join(tmp, "atomic.ndjson")
    with open(cap, "w") as f:
        f.write(ti.tick("KXBTC-26DEC31-B88", T0, 0.40, 0.50) + "\n")
        f.write(ti.trade("KXBTC-26DEC31-B88", T0 + 1, "atomic-t1") + "\n")
    with pytest.raises(RuntimeError, match="injected crash"):
        ing.process_file(cap)
    assert real.execute("SELECT count(*) FROM orderbooks_l1").fetchone()[0] == 0
    assert real.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
    assert real.execute("SELECT count(*) FROM checkpoint").fetchone()[0] == 0
    real.close()

    retry = ti.new_ingester(tmp, wh, db=db_rel)
    retry.process_file(cap)
    assert retry.con.execute("SELECT count(*) FROM orderbooks_l1").fetchone()[0] == 1
    assert retry.con.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    assert retry.con.execute("SELECT count(*) FROM checkpoint").fetchone()[0] == 1
    retry.con.close()


def test_late_exchange_time_fact_invalidates_visible_day_seal(exported_day):
    """A D fact received in D+1 hour 02+ cannot leave D archive-authorized.

    Raw partitions follow receipt time, while archive partitions follow the
    exchange timestamp.  This regression is the exact post-seal silent-omission
    incident shape: the fixed 00/01 receipt proof cannot see the later file, so
    ingest must invalidate before committing its row.
    """
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    seal_path = os.path.join(wh, "seals", "date=%s.json" % yd.isoformat())
    assert os.path.isfile(seal_path)
    late_path = os.path.join(wh, "raw", "date=%s" % today.isoformat(),
                             "firehose_02.ndjson")
    with open(late_path, "w") as f:
        f.write(ti.trade("KXMLB-26JUL06-BOS", _day_us(yd, 23, 3599),
                         "wp04-late-after-seal") + "\n")

    _release_warehouse()
    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    ingest.Ingester(con, wh).process_file(late_path)
    assert con.execute(
        "SELECT count(*) FROM trades WHERE trade_id='wp04-late-after-seal'"
    ).fetchone()[0] == 1
    con.close()

    assert not os.path.exists(seal_path)
    parked = glob.glob(os.path.join(
        wh, "seals", "date=%s.invalidated-late-fact.*.json" % yd.isoformat()))
    assert len(parked) == 1
    ledger = [json.loads(line) for line in open(
        os.path.join(wh, "seal_invalidations.ndjson"))]
    assert ledger[-1]["exchange_date"] == yd.isoformat()
    assert ledger[-1]["source_file"] == os.path.abspath(late_path)
    with pytest.raises(FileNotFoundError, match="archive day is not sealed"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_supervisor_gates_daily_research_on_current_export_and_archive_only():
    """Research starts only after raw catch-up, exact export and day seal."""
    path = os.path.join(ROOT, "tools", "pipeline_supervisor.sh")
    text = open(path).read()
    assert 'grep -q "EXPORT PASS\\|already archived" "$LIVE/export.log"' not in text
    assert '--check-caught-up' in text
    assert '--seal' in text
    assert '--verify-seal' in text
    assert ': > "$EXPORT_ATTEMPT_LOG"' in text
    assert 'grep' not in "\n".join(
        line for line in text.splitlines() if "$LIVE/export.log" in line)
    assert '[ "$LAST_SEALED" = "$YESTERDAY" ]' in text
    assert '[ "$HOUR_NOW" -ge 2 ]' in text
    assert '[ "$HOUR_NOW" -lt 2 ]' not in text
    assert text.index('--verify-seal') < text.index('--check-caught-up') \
        < text.index('--force --no-prune') \
        < text.index('--seal') < text.index('run_daily_research "$YESTERDAY"')
    assert text.index('stop_ingest_for_export') < text.index('--check-caught-up')
    assert 'research_${research_date}.done' in text
    assert 'coverage audit failed' in text
    research = text[text.index('run_daily_research()'):text.index('while true; do')]
    assert research.index('identity_start=') < research.index('coverage_audit.py')
    assert research.rindex('--verify-seal') > research.index('mm_calibrate.py')
    assert 'identity_start" = "$identity_end' in research
    assert '"seal_sha256"' in research
    assert '"manifest_date_sha256"' in research
    assert 'research_receipt_current' in text
    assert '-delete' not in "\n".join(
        line for line in text.splitlines()
        if 'find "$RAW"' in line and not line.lstrip().startswith("#"))
    for tool in ("mm_scan.py", "mm_backtest.py", "mm_calibrate.py"):
        line = next(x for x in text.splitlines() if tool in x)
        assert "--archive-only" in line, line


def test_archive_verify_only_requires_exact_current_day(exported_day):
    """An existing first partition is not proof of a complete daily archive."""
    wh, yd = exported_day["wh"], exported_day["yd"]
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--verify-only"]
    ok = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert ok.returncode == 0 and "ARCHIVE VERIFY PASS" in ok.stdout, ok.stdout + ok.stderr

    victim = next(iter(glob.glob(os.path.join(
        wh, "facts", "trades", "category=*", "subcategory=*",
        "date=%s" % yd.isoformat(), "*.csv.gz"))))
    os.unlink(victim)
    bad = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert bad.returncode != 0
    assert "ARCHIVE VERIFY FAIL" in bad.stderr


def test_existing_day_seal_verifies_without_opening_staging(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--verify-seal"]
    ok = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert ok.returncode == 0 and "DAY SEAL VERIFY PASS" in ok.stdout, \
        ok.stdout + ok.stderr


def test_archive_verify_detects_same_count_content_change(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    staging = os.path.join(wh, "staging.duckdb")
    con = duckdb.connect(staging)
    con.execute("UPDATE trades SET yes_price_e4 = yes_price_e4 + 1 "
                "WHERE trade_id = 'wp04-y1'")
    con.close()
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--verify-only"]
    bad = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert bad.returncode != 0
    assert "content mismatch" in bad.stderr


def test_day_seal_requires_all_closed_raw_bytes_checkpointed(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    raw = next(iter(glob.glob(os.path.join(
        wh, "raw", "date=%s" % yd.isoformat(), "*.ndjson*"))))
    with open(raw, "a") as f:
        f.write(json.dumps({"raw": "{}"}) + "\n")
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--seal"]
    bad = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert bad.returncode != 0
    assert "checkpoint behind raw" in bad.stderr
    assert not os.path.exists(os.path.join(
        wh, "seals", "date=%s.json" % yd.isoformat()))


def test_archive_only_rejects_raw_append_after_seal(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    raw = next(iter(glob.glob(os.path.join(
        wh, "raw", "date=%s" % yd.isoformat(), "*.ndjson*"))))
    with open(raw, "a") as f:
        f.write(json.dumps({"raw": "{}"}) + "\n")
    with pytest.raises(RuntimeError, match="raw file changed after seal"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_day_seal_rejects_uncheckpointed_cross_midnight_receipt(exported_day):
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    late = os.path.join(wh, "raw", "date=%s" % today.isoformat(),
                        "firehose_01.ndjson.1")
    with open(late, "w") as f:
        f.write(ti.trade("KXLATE-TEST-YES", _day_us(yd, 23, 3599),
                         "late-cross-day") + "\n")
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--seal"]
    bad = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert bad.returncode != 0
    assert "checkpoint behind raw" in bad.stderr


def test_default_past_window_is_sealed_archive_only(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    rel = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                         warehouse=wh)
    assert rel.count("*").fetchone()[0] == 4
    assert not warehouse._ATTACHED
    unknown = warehouse.load(
        "trades", start=yd.isoformat(), end=yd.isoformat(), warehouse=wh,
        columns=["trade_id", "category", "subcategory"]).filter(
            "trade_id = 'wp04-y4'").fetchone()
    assert unknown == ("wp04-y4", None, None)


def test_day_seal_rejects_renamed_archive_column(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    victim = next(iter(glob.glob(os.path.join(
        wh, "facts", "orderbooks_l1", "category=*", "subcategory=*",
        "date=%s" % yd.isoformat(), "*.parquet"))))
    old_md5 = export_day.md5_file(victim)
    tmp = victim + ".renamed"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * RENAME (yes_bid_e4 AS wrong_yes_bid) "
        "FROM read_parquet('%s', hive_partitioning=false)) TO '%s' "
        "(FORMAT PARQUET, COMPRESSION zstd)"
        % (victim.replace("'", "''"), tmp.replace("'", "''")))
    con.close()
    os.replace(tmp, victim)
    new_md5 = export_day.md5_file(victim)
    manifest = os.path.join(wh, "manifest.csv")
    text = open(manifest).read()
    assert old_md5 in text
    with open(manifest, "w") as f:
        f.write(text.replace(old_md5, new_md5, 1))
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--seal"]
    bad = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert bad.returncode != 0
    assert "schema mismatch" in bad.stderr


def test_day_seal_rejects_archive_mutation_after_exact_verify(exported_day,
                                                               monkeypatch):
    wh, yd = exported_day["wh"], exported_day["yd"]
    victim = next(iter(glob.glob(os.path.join(
        wh, "facts", "trades", "category=Sports", "subcategory=*",
        "date=%s" % yd.isoformat(), "*.csv.gz"))))

    def mutate_after_verify(_con, _retain):
        with open(victim, "ab") as f:
            f.write(b"between-verify-and-seal")

    monkeypatch.setattr(export_day, "prune_staging", mutate_after_verify)
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS stg" % os.path.join(
        wh, "staging.duckdb").replace("'", "''"))
    cfg = dict(_export_env(wh))
    effective = {
        "raw_root": cfg["RAW_ROOT"], "archive_root": cfg["ARCHIVE_ROOT"],
        "warehouse_root": cfg["WAREHOUSE_ROOT"], "staging_retain_days": 2,
    }
    lo = _day_us(yd, 0)
    with pytest.raises(RuntimeError, match="archive changed during seal"):
        export_day.write_day_seal(con, yd.isoformat(), lo,
                                  lo + 86_400_000_000, effective)
    con.close()


def test_refused_force_preserves_existing_valid_seal(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    seal_path = os.path.join(wh, "seals", "date=%s.json" % yd.isoformat())
    before = open(seal_path, "rb").read()
    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    lo = _day_us(yd, 0)
    con.execute("DELETE FROM trades WHERE ts_utc >= ? AND ts_utc < ?",
                [lo, lo + 86_400_000_000])
    con.close()
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--force", "--no-prune"]
    bad = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert bad.returncode == 3 and "SHRINK" in bad.stderr
    assert open(seal_path, "rb").read() == before


def test_successful_seal_prunes_only_older_staging(exported_day):
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    staging = os.path.join(wh, "staging.duckdb")
    con = duckdb.connect(staging)
    old = today - datetime.timedelta(days=3)
    con.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [_day_us(old, 12), "KXOLD-TEST-YES", "KXOLD", "KXOLD-TEST",
         None, None, None, "old-row", 5000, 5000, 10000, "yes"])
    con.close()
    cmd = [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
           "--date", yd.isoformat(), "--seal"]
    ok = subprocess.run(cmd, env=_export_env(wh), capture_output=True, text=True)
    assert ok.returncode == 0 and "DAY SEAL PASS" in ok.stdout, ok.stdout + ok.stderr
    con = duckdb.connect(staging, read_only=True)
    assert con.execute("SELECT count(*) FROM trades WHERE trade_id='old-row'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM trades WHERE trade_id='wp04-y1'").fetchone()[0] == 1
    con.close()


def test_archive_only_rejects_stale_seal_after_manifest_change(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    manifest = os.path.join(wh, "manifest.csv")
    text = open(manifest).read()
    with open(manifest, "w") as f:
        f.write(text.replace("Crypto", "Crypto_changed", 1))
    with pytest.raises(RuntimeError, match="stale or invalid archive seal"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_archive_only_rejects_unknown_seal_version(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    path = os.path.join(wh, "seals", "date=%s.json" % yd.isoformat())
    seal = json.load(open(path))
    seal["version"] = 2
    with open(path, "w") as f:
        json.dump(seal, f)
    with pytest.raises(RuntimeError, match="stale or invalid archive seal"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_archive_only_rejects_missing_sealed_partition(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    victim = next(iter(glob.glob(os.path.join(
        wh, "facts", "trades", "category=*", "subcategory=*",
        "date=%s" % yd.isoformat(), "*.csv.gz"))))
    os.unlink(victim)
    with pytest.raises(RuntimeError, match="file-set mismatch"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_archive_only_rejects_tampered_sealed_partition(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    victim = next(iter(glob.glob(os.path.join(
        wh, "facts", "trades", "category=*", "subcategory=*",
        "date=%s" % yd.isoformat(), "*.csv.gz"))))
    with open(victim, "ab") as f:
        f.write(b"tamper")
    with pytest.raises(RuntimeError, match="md5 mismatch"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_sealed_warehouse_is_relocatable(exported_day, tmp_path):
    wh, yd = exported_day["wh"], exported_day["yd"]
    restored = str(tmp_path / "restored_warehouse")
    shutil.copytree(wh, restored)
    rel = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                         warehouse=restored, archive_only=True)
    assert rel.count("*").fetchone()[0] == 4
    assert not warehouse._ATTACHED


def test_event_pack_archive_only_uses_bounded_sealed_horizon(exported_day,
                                                              tmp_path):
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    row = {
        "unit": "event", "unit_key": "KXMLB-26JUL06", "status": "sealed",
        "category": "Sports", "subcategory": "MLB", "group": "MLB",
        "event_ticker": "KXMLB-26JUL06", "series_ticker": "KXMLB",
        "markets": ["KXMLB-26JUL06-BOS"],
        "win_start_us": _day_us(yd, 0), "win_end_us": _day_us(today, 0),
        "window_source": "test", "crossed_day_boundary": False,
    }
    out = str(tmp_path / "sealed_pack")
    manifest = event_pack.build_pack(row, wh, out, _day_us(today, 2),
                                     archive_only=True)
    assert manifest["status"] == "refused"
    assert "AF-5 finality unavailable" in manifest["reason"]
    assert not warehouse._ATTACHED


def test_event_pack_stale_scan_uses_latest_contiguous_seal(tmp_path):
    root = str(tmp_path / "wh_horizon")
    start = datetime.date(2026, 7, 6)
    os.makedirs(os.path.join(root, "seals"))
    for offset in (0, 1):
        day = start + datetime.timedelta(days=offset)
        with open(os.path.join(root, "seals", "date=%s.json" % day), "w") as f:
            f.write("{}")
    start_us = int(datetime.datetime(2026, 7, 6, tzinfo=datetime.timezone.utc)
                   .timestamp() * 1_000_000)
    expected = int(datetime.datetime(2026, 7, 8, tzinfo=datetime.timezone.utc)
                   .timestamp() * 1_000_000)
    assert event_pack._latest_contiguous_seal_end(root, start_us) == expected


def test_event_pack_af5_requires_next_complete_sealed_day(monkeypatch):
    """A seal for the window day alone cannot hide D+1 late activity."""
    day = 86_400_000_000
    row = {
        "unit": "event", "unit_key": "EV-LATE", "status": "sealed",
        "category": "Sports", "markets": ["EV-LATE-YES"],
        "win_start_us": 10 * day, "win_end_us": 10 * day + 3_600_000_000,
        "window_source": "test", "crossed_day_boundary": False,
    }
    # Only the day containing win_end is sealed.  The required extra complete
    # day is absent, so no warehouse query or pack write is allowed.
    monkeypatch.setattr(event_pack, "_latest_contiguous_seal_end",
                        lambda *_a, **_k: 11 * day)
    monkeypatch.setattr(event_pack, "observed_last_ts",
                        lambda *_a, **_k: pytest.fail("scan ran without horizon"))
    got = event_pack.build_pack(row, "/unused", "/unused", 12 * day,
                                archive_only=True)
    assert got["status"] == "refused"
    assert "required post-window horizon" in got["reason"]


def test_event_pack_af5_detects_late_activity_in_next_sealed_day(monkeypatch,
                                                                 tmp_path):
    """D+1 activity is refused; refresh also waits for a new quiet horizon."""
    day = 86_400_000_000
    stored_end = 10 * day + 3_600_000_000
    late = 11 * day + 3_600_000_000
    row = {
        "unit": "event", "unit_key": "EV-LATE", "status": "sealed",
        "category": "Sports", "markets": ["EV-LATE-YES"],
        "win_start_us": 10 * day, "win_end_us": stored_end,
        "window_source": "test", "crossed_day_boundary": True,
    }
    # D and D+1 are sealed (scan_end=D+2).  That satisfies the stored window's
    # finality horizon and exposes a late D+1 row.  Extending around that row
    # would require D+2 to be sealed too (scan_end=D+3), so refresh must wait.
    monkeypatch.setattr(event_pack, "_latest_contiguous_seal_end",
                        lambda *_a, **_k: 12 * day)
    monkeypatch.setattr(event_pack, "observed_last_ts",
                        lambda *_a, **_k: late)
    refused = event_pack.build_pack(row, "/unused", str(tmp_path), 13 * day,
                                    archive_only=True)
    assert refused["status"] == "refused" and "stale index" in refused["reason"]
    refreshed = event_pack.build_pack(row, "/unused", str(tmp_path), 13 * day,
                                      refresh=True, archive_only=True)
    assert refreshed["status"] == "refused"
    assert "does not prove refreshed post-window horizon" in refreshed["reason"]


def test_event_pack_af5_packs_only_with_sufficient_quiet_horizon(monkeypatch,
                                                                 tmp_path):
    day = 86_400_000_000
    row = {
        "unit": "event", "unit_key": "EV-QUIET", "status": "sealed",
        "category": "Sports", "markets": ["EV-QUIET-YES"],
        "win_start_us": 10 * day, "win_end_us": 10 * day + 3_600_000_000,
        "window_source": "test", "crossed_day_boundary": False,
    }
    monkeypatch.setattr(event_pack, "_latest_contiguous_seal_end",
                        lambda *_a, **_k: 12 * day)
    monkeypatch.setattr(event_pack, "observed_last_ts",
                        lambda *_a, **_k: 10 * day + 1_000_000)

    def empty_extract(_table, _markets, _category, _start, _end, path,
                      _warehouse, _archive_only=None):
        with open(path, "w") as f:
            f.write("header\n")
        return 0

    monkeypatch.setattr(event_pack, "_write_table", empty_extract)
    got = event_pack.build_pack(row, "/unused", str(tmp_path), 13 * day,
                                archive_only=True)
    assert got["status"] == "packed"
    assert got["af5_scan_end_us"] == 12 * day
    assert got["af5_required_horizon_us"] == 12 * day
    assert got["af5_finality_days"] == 1


@pytest.mark.parametrize("module,extra", [
    (mm_scan, []),
    (mm_backtest, ["--markets", "T"]),
    (mm_calibrate, []),
])
@pytest.mark.parametrize("offset,expected", [(-1, True), (0, False)])
def test_mm_tools_select_archive_only_by_range_end(monkeypatch, module, extra,
                                                    offset, expected):
    """Past-day tools cannot accidentally retain the live staging lock."""
    day = (datetime.datetime.now(datetime.timezone.utc).date()
           + datetime.timedelta(days=offset)).isoformat()
    calls = []

    class StopAfterFirstLoad(Exception):
        pass

    def fake_load(table, **kwargs):
        calls.append((table, kwargs))
        raise StopAfterFirstLoad

    monkeypatch.setattr(module, "load", fake_load)
    with pytest.raises(StopAfterFirstLoad):
        module.main([module.__file__, "--date", day] + extra)
    assert calls[0][1]["archive_only"] is expected


@pytest.mark.parametrize("offset,expected", [(-1, True), (0, False)])
def test_mm_research_selects_archive_only_by_range_end(monkeypatch,
                                                       offset, expected):
    day = (datetime.datetime.now(datetime.timezone.utc).date()
           + datetime.timedelta(days=offset)).isoformat()
    seen = []

    class Stop(Exception):
        pass

    monkeypatch.setattr(mm_research, "load_fee_facts",
                        lambda *a, **k: {"verified": False})

    def fake_build(start, end, archive_only=False):
        seen.append((start, end, archive_only))
        raise Stop

    monkeypatch.setattr(mm_research, "build_dataset", fake_build)
    with pytest.raises(Stop):
        mm_research.main([mm_research.__file__, "--start", day, "--end", day])
    assert seen == [(day, day, expected)]


# ─────────────────── folded from WP-01 (E4 integrity) ───────────────────

def test_e4_roundtrip(tmp_path):
    """LOCKED: prices stored as E4 integers end-to-end; display conversion only
    at load()/presentation. raw "0.0325" -> 325 stored -> presented 0.0325
    exactly; "0.5000" -> 5000; legacy integer-cent snapshot levels scale *100."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    # staging INSIDE the warehouse root so load(warehouse=wh) resolves it
    ing = ti.new_ingester(tmp, wh, db=os.path.join("warehouse", "staging.duckdb"))
    mt = "KXBTC-26DEC31-B33"
    cap = os.path.join(tmp, "cap.ndjson")
    with open(cap, "w") as f:
        f.write(ti.tick(mt, T0, 0.0325, 0.5000) + "\n")
        f.write(_raw("trade", {"market_ticker": mt, "ts_ms": (T0 + 1_000_000) // 1000,
                               "trade_id": "e4-1", "yes_price_dollars": "0.0325",
                               "no_price_dollars": "0.9675", "count_fp": "1.00",
                               "taker_side": "yes"}, T0 + 1_000_000) + "\n")
    ing.process_file(cap)
    (yb, ya), = _q(ing, "SELECT yes_bid_e4, yes_ask_e4 FROM orderbooks_l1")
    assert (yb, ya) == (325, 5000)         # sub-penny survives, no cent rounding
    (yp, np_), = _q(ing, "SELECT yes_price_e4, no_price_e4 FROM trades")
    assert (yp, np_) == (325, 9675)
    ing.con.close()

    # presentation at the edge: load() + E4/10000.0 gives 0.0325 EXACTLY
    (bid, ask), = warehouse.load(
        "orderbooks_l1", warehouse=wh,
        columns=["yes_bid_e4/10000.0 AS yes_bid", "yes_ask_e4/10000.0 AS yes_ask"]
    ).fetchall()
    assert bid == 0.0325 and ask == 0.5

    # legacy integer-cent input path (ingest.levels_e4): cents scale *100 to E4
    assert json.loads(ingest.levels_e4({"yes": [[3, "310.00"]]}, "yes")) == \
        [[300, 3100000]]
    # dollars_fp is authoritative when both are present (no double-scaling)
    assert json.loads(ingest.levels_e4(
        {"yes_dollars_fp": [["0.0100", "1.00"]], "yes": [[99, "1.00"]]}, "yes")) == \
        [[100, 10000]]


def test_fractional_qty(tmp_path):
    """LOCKED: quantities are BIGINT E4 — "5119.00" -> 51190000 survives ingest
    exactly; genuinely fractional sizes and trade counts survive too."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    ing = ti.new_ingester(tmp, wh)
    mt = "KXBTC-26DEC31-B34"
    cap = os.path.join(tmp, "cap.ndjson")
    with open(cap, "w") as f:
        f.write(_raw("ticker", {"market_ticker": mt, "ts_ms": T0 // 1000,
                                "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4200",
                                "yes_bid_size_fp": "5119.00",
                                "yes_ask_size_fp": "2.50",
                                "price_dollars": "0.4100", "volume_fp": "10.75",
                                "open_interest_fp": "5.00"}, T0) + "\n")
        f.write(_raw("trade", {"market_ticker": mt, "ts_ms": (T0 + 1_000_000) // 1000,
                               "trade_id": "fq-1", "yes_price_dollars": "0.4100",
                               "no_price_dollars": "0.5900", "count_fp": "0.7500",
                               "taker_side": "no"}, T0 + 1_000_000) + "\n")
    ing.process_file(cap)
    (bq, aq, vol), = _q(ing, "SELECT yes_bid_qty_e4, yes_ask_qty_e4, volume_e4 "
                             "FROM orderbooks_l1")
    assert (bq, aq, vol) == (51190000, 25000, 107500)
    (cnt,), = _q(ing, "SELECT count_e4 FROM trades")
    assert cnt == 7500
    ing.con.close()


# ─────────────────── folded from WP-02 (class policy) ───────────────────

def _series_catalog(wh, rows):
    """catalog/series parquet fixture (ticker, category, tags, title) —
    the input build_classification reads."""
    out = os.path.join(wh, "catalog", "series")
    os.makedirs(out, exist_ok=True)
    nd = os.path.join(wh, "_series.ndjson")
    with open(nd, "w") as f:
        for ticker, category, tags, title in rows:
            f.write(json.dumps({"ticker": ticker, "category": category,
                                "tags": tags, "title": title}) + "\n")
    duckdb.connect().execute(
        "COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
        "TO '%s' (FORMAT PARQUET)"
        % (nd.replace("'", "''"),
           os.path.join(out, "part-00000.parquet").replace("'", "''")))
    os.unlink(nd)


def _write_classes(path, class_a, class_b):
    with open(path, "w") as f:
        f.write("class_a_full_l1:\n")
        for c in class_a:
            f.write("  - %s\n" % c)
        f.write("class_b_trades_only:\n")
        for c in class_b:
            f.write("  - %s\n" % c)


def _run_classification(wh, cfg, tmp):
    review = os.path.join(tmp, "review.csv")
    rc = bc.main(["build_classification", "--warehouse", wh,
                  "--config", cfg, "--review", review])
    assert rc == 0
    pq = os.path.join(wh, "catalog", "series_classified", "part-00000.parquet")
    return {t: k for t, k in duckdb.connect().execute(
        "SELECT series_ticker, record_class FROM read_parquet('%s')"
        % pq.replace("'", "''")).fetchall()}


def test_policy_from_config(tmp_path):
    """Class membership follows config/market_classes.yaml, never hardcode:
    mutating a temp config copy flips the classes."""
    tmp = str(tmp_path)
    wh = os.path.join(tmp, "warehouse")
    _series_catalog(wh, [("KXBTC", "Crypto", ["BTC"], "Bitcoin brackets"),
                         ("KXACME", "Companies", [], "ACME earnings")])
    cfg = os.path.join(tmp, "market_classes.yaml")

    _write_classes(cfg, class_a=["Crypto"], class_b=["Companies"])
    classes = _run_classification(wh, cfg, tmp)
    assert classes == {"KXBTC": "A", "KXACME": "B"}

    # mutate the temp config: swap the classes -> behavior must follow
    _write_classes(cfg, class_a=["Companies"], class_b=["Crypto"])
    classes = _run_classification(wh, cfg, tmp)
    assert classes == {"KXBTC": "B", "KXACME": "A"}


def test_unknown_category_warns(tmp_path, capsys):
    """A live category missing from the config defaults to Class B AND warns on
    stderr (V16: never silent, never a guess)."""
    tmp = str(tmp_path)
    wh = os.path.join(tmp, "warehouse")
    _series_catalog(wh, [("KXBTC", "Crypto", ["BTC"], "Bitcoin brackets"),
                         ("KXNEWTHING", "Underwater Basketweaving", [], "??")])
    cfg = os.path.join(tmp, "market_classes.yaml")
    _write_classes(cfg, class_a=["Crypto"], class_b=["Companies"])

    classes = _run_classification(wh, cfg, tmp)
    err = capsys.readouterr().err
    assert "WARNING" in err and "Underwater Basketweaving" in err
    assert classes["KXNEWTHING"] == "B"   # fail-closed default
    assert classes["KXBTC"] == "A"        # known categories unaffected


# ──────────── adopted from tests/test_ingest.py (A1: independent values) ────────────

def test_change_only(tmp_path):
    """57 identical ticks + 1 price change + 1 quantity-only change -> exactly
    3 rows (1 snapshot + 2 changes): the dedup state is the FULL
    (bid, bid_qty, ask, ask_qty) tuple, not just prices."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    ing = ti.new_ingester(tmp, wh)
    mt = "KXBTC-26DEC31-B91"
    lines = [ti.tick(mt, T0 + i * 2_000_000, 0.13, 0.15) for i in range(57)]
    lines.append(ti.tick(mt, T0 + 58 * 2_000_000, 0.13, 0.16))            # price change
    lines.append(ti.tick(mt, T0 + 59 * 2_000_000, 0.13, 0.16, ask_q=150))  # qty-only change
    cap = os.path.join(tmp, "cap.ndjson")
    with open(cap, "w") as f:
        f.write("\n".join(lines) + "\n")
    ing.process_file(cap)
    rows = _q(ing, "SELECT is_snapshot, yes_ask_e4, yes_ask_qty_e4 FROM orderbooks_l1 "
                   "WHERE market_ticker='%s' ORDER BY ts_utc" % mt)
    assert [r[0] for r in rows] == [True, False, False]
    assert [(r[1], r[2]) for r in rows] == [(1500, 1_000_000), (1600, 1_000_000),
                                            (1600, 1_500_000)]
    ing.con.close()


def test_heartbeat(tmp_path):
    """3 simulated quiet hours -> exactly 3 is_snapshot heartbeat rows at the
    hour starts, carrying the last book state, with NULL price fields."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    ing = ti.new_ingester(tmp, wh)
    mt_quiet, mt_clock = "KXBTC-26DEC31-B93", "KXBTC-26DEC31-B94"
    h1 = (T0 // HOUR_US + 1) * HOUR_US
    lines = [ti.tick(mt_quiet, T0, 0.33, 0.35)]
    for k in range(3):  # a different market advances the data clock 3 hours
        lines.append(ti.tick(mt_clock, h1 + k * HOUR_US + 300_000_000,
                             0.60 + k / 100, 0.62))
    cap = os.path.join(tmp, "cap.ndjson")
    with open(cap, "w") as f:
        f.write("\n".join(lines) + "\n")
    ing.process_file(cap)
    hb = _q(ing, "SELECT ts_utc, yes_bid_e4, yes_bid_qty_e4, price_e4 "
                 "FROM orderbooks_l1 WHERE market_ticker='%s' AND is_snapshot "
                 "AND ts_utc > %d ORDER BY ts_utc" % (mt_quiet, T0))
    assert [ts for ts, _, _, _ in hb] == [h1, h1 + HOUR_US, h1 + 2 * HOUR_US]
    assert all(ts % HOUR_US == 0 for ts, _, _, _ in hb)
    assert all(bid == 3300 and q == 1_000_000 for _, bid, q, _ in hb)  # state carried
    assert all(p is None for _, _, _, p in hb)   # scheduled heartbeats: book only
    total = _q(ing, "SELECT count(*) FROM orderbooks_l1 WHERE market_ticker='%s'"
               % mt_quiet)[0][0]
    assert total == 4                            # 1 first-observation + 3 heartbeats
    ing.con.close()


def test_kill_restart_determinism(tmp_path):
    """Interrupt ingest at a byte offset (partial line = kill mid-write), resume
    with a fresh Ingester: every table equals the uninterrupted run ROW-FOR-ROW
    (multiset EXCEPT ALL in both directions — stronger than an ORDER
    BY-all-columns diff because it also catches duplicate-row miscounts)."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    mt1, mt2 = "KXBTC-26DEC31-B95", "KXBTC-26DEC31-B96"
    lines = []
    for i in range(40):  # changes + repeats within the hour
        lines.append(ti.tick(mt1, T0 + i * 30_000_000, 0.30 + (i % 7) / 100, 0.44))
        if i % 5 == 0:
            lines.append(ti.trade(mt1, T0 + i * 30_000_000 + 1000, "kr-%d" % i))
    h1 = (T0 // HOUR_US + 1) * HOUR_US
    for k in range(2):   # cross 2 hour boundaries -> scheduled heartbeats too
        lines.append(ti.tick(mt2, h1 + k * HOUR_US + 120_000_000, 0.70 + k / 100, 0.72))
    lines.append(_raw("orderbook_snapshot",
                      {"market_ticker": mt1, "ts_ms": (h1 + HOUR_US + 240_000_000) // 1000,
                       "yes_dollars_fp": [["0.3100", "12.50"]],
                       "no_dollars_fp": [["0.5600", "3.00"]]},
                      h1 + HOUR_US + 240_000_000))
    full = "\n".join(lines) + "\n"

    # uninterrupted reference run
    cap_ref = os.path.join(tmp, "ref.ndjson")
    with open(cap_ref, "w") as f:
        f.write(full)
    ref = ti.new_ingester(tmp, wh, "ref.duckdb")
    ref.process_file(cap_ref)
    ref.con.close()

    # interrupted run: cut mid-line at a deterministic byte offset, "kill",
    # append the rest (capture keeps writing), restart with a FRESH Ingester
    cut = full.find("\n", int(len(full) * 0.4)) + 25
    cap_int = os.path.join(tmp, "int.ndjson")
    with open(cap_int, "w") as f:
        f.write(full[:cut])
    ing1 = ti.new_ingester(tmp, wh, "int.duckdb")
    ing1.process_file(cap_int)
    ing1.con.close()                      # kill
    with open(cap_int, "a") as f:
        f.write(full[cut:])
    ing2 = ti.new_ingester(tmp, wh, "int.duckdb")   # restart: state rebuilds
    ing2.process_file(cap_int)
    ing2.con.close()

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS r (READ_ONLY)"
                % os.path.join(tmp, "ref.duckdb").replace("'", "''"))
    con.execute("ATTACH '%s' AS i (READ_ONLY)"
                % os.path.join(tmp, "int.duckdb").replace("'", "''"))
    for t in ("orderbooks_l1", "trades", "orderbooks_full"):
        n_ref = con.execute("SELECT count(*) FROM r.%s" % t).fetchone()[0]
        n_int = con.execute("SELECT count(*) FROM i.%s" % t).fetchone()[0]
        assert n_ref == n_int and n_ref > 0, (t, n_ref, n_int)   # never vacuous
        only_ref = con.execute("SELECT count(*) FROM (SELECT * FROM r.%s "
                               "EXCEPT ALL SELECT * FROM i.%s)" % (t, t)).fetchone()[0]
        only_int = con.execute("SELECT count(*) FROM (SELECT * FROM i.%s "
                               "EXCEPT ALL SELECT * FROM r.%s)" % (t, t)).fetchone()[0]
        assert (only_ref, only_int) == (0, 0), \
            "%s differs after kill/restart: ref-only=%d int-only=%d" % (t, only_ref, only_int)
    con.close()


def test_supervisor_wires_capture_gaps_daily_and_live():
    """W-C2.1 contract: the supervisor MUST (a) record each completed day's
    capture gaps into the durable record before its raw ages out of the 3-day
    retention (else B3 — a pruned unscanned day loses its gaps forever), and
    (b) refresh the live gap alert. A regression that drops either wiring
    silently rots the gap gate / hides an in-progress outage."""
    sup = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()
    # Non-comment lines only, so commenting a call out (not just deleting the
    # text) trips the test — the substring must be on a live line.
    live = [ln for ln in sup.splitlines() if not ln.lstrip().startswith("#")]
    assert any('capture_gaps.py --date "$YESTERDAY"' in ln for ln in live), \
        "daily capture-gap record not wired into the supervisor export block"
    assert any("capture_gaps.py --live" in ln for ln in live), \
        "live capture-gap alert not wired into the supervisor watchdog loop"
    # next_actions.md item 1: daily coverage audit, non-zero exit surfaced.
    assert any('coverage_audit.py --date "$research_date"' in ln for ln in live), \
        "daily coverage audit not wired into the archive-only research function"
    assert any('run_daily_research "$YESTERDAY"' in ln for ln in live), \
        "sealed-day research function not invoked by the supervisor"


def test_supervisor_single_rest_owner_gate():
    """W-A4 contract: the catalog block (catalog_sync / build_classification /
    dim_snapshot — the supervisor's ONLY REST spenders; ws_shadow's XCHECK
    defaults off) must be gated on the absence of work/live/rest_disabled.
    This is the mechanism behind the cutover's single-REST-owner rule: a box
    runs WS capture while provably spending zero REST tokens (EC2 before
    step 4, the Mac after step 3). The skip must be LOUD (D2), never silent."""
    sup = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()
    live = [ln for ln in sup.splitlines() if not ln.lstrip().startswith("#")]
    assert any('-f "$LIVE/rest_disabled"' in ln for ln in live), \
        "rest_disabled flag gate missing from the supervisor catalog block"
    txt = "\n".join(live)
    assert txt.find('rest_disabled') < txt.find("catalog_sync.py"), \
        "rest_disabled gate must precede the first catalog_sync invocation"
    assert any("REST disabled" in ln for ln in live), \
        "suppressed catalog block must log loudly (D2), not skip silently"


def test_rider_b_all_categories_full_l1():
    """Rider (b), W-A5 (operator-approved 2026-07-08): the PRODUCTION config
    must carry every category as class_a_full_l1 — an empty Class B. A
    regression that demotes a category silently reopens the permanent
    Class-B orderbook loss (DATA_COMPLETENESS 拼图①)."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(ROOT, "tools"))
    from build_classification import load_yaml_classes
    a, b = load_yaml_classes(os.path.join(ROOT, "config", "market_classes.yaml"))
    assert b == [], "class_b_trades_only must be empty since rider (b): %r" % b
    # the full historical taxonomy (18 categories) is all present in A
    for cat in ("Crypto", "Sports", "Elections", "Exotics", "Mentions",
                "World", "Science and Technology"):
        assert cat in a, "category %s missing from class_a_full_l1" % cat
    assert len(a) >= 18, "expected the full 18-category taxonomy, got %d" % len(a)


def test_supervisor_metrics_rotation_rider_a():
    """Rider (a), W-A5: metrics.ndjson rotates at 512MB keep-3 via
    tools/rotate_metrics.sh, wired into the supervisor's hourly loop (the
    file was 23GB unbounded on the Mac). Threshold env-tunable for tests."""
    sup = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()
    live = [ln for ln in sup.splitlines() if not ln.lstrip().startswith("#")]
    assert any("rotate_metrics.sh" in ln for ln in live), \
        "supervisor must invoke tools/rotate_metrics.sh in the hourly loop"
    rot = os.path.join(ROOT, "tools", "rotate_metrics.sh")
    assert os.path.exists(rot), "tools/rotate_metrics.sh missing"


def test_metrics_rotation_semantics(tmp_path):
    """rotate_metrics.sh: keep-3 chain, no-op below threshold/missing file."""
    import subprocess
    rot = os.path.join(ROOT, "tools", "rotate_metrics.sh")
    mf = tmp_path / "metrics.ndjson"

    def run():
        return subprocess.run(
            ["bash", rot, str(mf)], env={**os.environ,
                                         "METRICS_ROTATE_BYTES": "10"},
            capture_output=True, text=True).returncode

    assert run() == 0  # missing file: no-op, success
    mf.write_text("x" * 5)
    assert run() == 0 and mf.exists()  # below threshold: untouched
    for gen in "abc":
        mf.write_text(gen * 20)  # above threshold each time
        assert run() == 0
    mf.write_text("d" * 20)
    assert run() == 0
    # keep-3: current gone (rotated to .1), chain shifted, oldest dropped
    assert not mf.exists()
    assert (tmp_path / "metrics.ndjson.1").read_text() == "d" * 20
    assert (tmp_path / "metrics.ndjson.2").read_text() == "c" * 20
    assert (tmp_path / "metrics.ndjson.3").read_text() == "b" * 20
    assert not (tmp_path / "metrics.ndjson.4").exists()


def test_supervisor_sigterm_graceful():
    """W-A5 (audit finding 3): systemctl stop must not need SIGKILL.
    Two requirements on live lines: (1) ws_shadow runs BACKGROUNDED with a
    `wait` (a foreground child blocks bash trap delivery for the whole
    hour); (2) INT/TERM trap must EXIT (bash resumes after a signal trap —
    the old combined trap would keep looping after cleanup)."""
    sup = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()
    live = [ln for ln in sup.splitlines() if not ln.lstrip().startswith("#")]
    txt = "\n".join(live)
    assert 'wait "$WS_PID"' in txt, "ws_shadow must be backgrounded + waited"
    assert any("exit 143" in ln and "trap" in ln for ln in live), \
        "INT/TERM trap must exit (143) so the EXIT trap runs cleanup once"
