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


def test_late_fact_for_sealed_day_diverts_to_corrections(exported_day):
    """WRITE-ONCE seals (operator ruling 2026-07-11): a D fact received after
    D was sealed NEVER enters staging and NEVER touches the seal — it lands
    VERBATIM in the corrections partition with a ledger entry and a counter,
    and archive-only reads of D keep working."""
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    seal_path = os.path.join(wh, "seals", "date=%s.json" % yd.isoformat())
    assert os.path.isfile(seal_path)
    seal_before = open(seal_path, "rb").read()
    late_path = os.path.join(wh, "raw", "date=%s" % today.isoformat(),
                             "firehose_02.ndjson")
    with open(late_path, "w") as f:
        f.write(ti.trade("KXMLB-26JUL06-BOS", _day_us(yd, 23, 3599),
                         "wp04-late-after-seal") + "\n")

    _release_warehouse()
    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    ingest.Ingester(con, wh).process_file(late_path)
    # the late row must NOT be in staging (it would demand mutating the
    # sealed archive later)
    assert con.execute(
        "SELECT count(*) FROM trades WHERE trade_id='wp04-late-after-seal'"
    ).fetchone()[0] == 0
    con.close()

    # seal byte-identical (write-once, untouched)
    assert os.path.isfile(seal_path)
    assert open(seal_path, "rb").read() == seal_before
    # row landed verbatim in the corrections partition
    corr = os.path.join(wh, "corrections", "date=%s" % yd.isoformat(),
                        "late_rows.ndjson")
    recs = [json.loads(line) for line in open(corr)]
    assert any(r["table"] == "trades" and
               "wp04-late-after-seal" in json.dumps(r["row"]) for r in recs)
    assert recs[-1]["source_file"] == os.path.abspath(late_path)
    # counted in the corrections ledger, seal explicitly untouched
    ledger = [json.loads(line) for line in open(
        os.path.join(wh, "corrections", "ledger.ndjson"))]
    assert ledger[-1]["event"] == "LATE_FACT_DIVERTED_TO_CORRECTIONS"
    assert ledger[-1]["exchange_date"] == yd.isoformat()
    assert ledger[-1]["n_rows"] >= 1
    assert ledger[-1]["seal_untouched"] is True
    # archive-only reads of the sealed day KEEP working
    rel = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                         warehouse=wh, archive_only=True)
    assert rel.count("*").fetchone()[0] == 4


def test_supervisor_gates_daily_research_on_current_export_and_archive_only():
    """Research starts only after raw catch-up, exact export and a WRITE-ONCE
    day seal — and the whole seal chain runs in the BACKGROUND, off the
    ws_shadow launch path (P4: capture never waits on seal work)."""
    path = os.path.join(ROOT, "tools", "pipeline_supervisor.sh")
    text = open(path).read()
    live = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert 'grep -q "EXPORT PASS\\|already archived" "$LIVE/export.log"' not in text
    # NB: the watchdog also contains an (indented) 'while true; do'; the
    # main loop is the column-0 one.
    chain = text[text.index('run_seal_chain()'):text.index('\nwhile true; do')]
    assert chain.index('--verify-seal') < chain.index('--check-caught-up') \
        < chain.index('--force --no-prune') \
        < chain.index('--seal') < chain.index('run_daily_research "$CHAIN_DATE"')
    assert chain.index('stop_ingest_for_export') < chain.index('--check-caught-up')
    # WRITE-ONCE: a corrupt existing seal is a durable operator alarm, never
    # an automatic reseal; the ingest-side reseal mechanism no longer exists.
    assert 'SEAL CORRUPT' in chain and 'write_seal_alarm' in chain
    assert 'stop_research_for_reseal' not in text
    assert 'invalidated-late-fact' not in text
    # 02:00 earliest seal attempt; 03:00 durable alarm artifact
    assert '-ge 2 ]' in chain and '-ge 3 ]' in chain
    assert 'UNSEALED_PAST_ALARM_LINE' in chain
    assert 'seal_alarm.json' in text
    # ASYNC + single instance: chain backgrounded before ws_shadow relaunch
    main = text[text.index('\nwhile true; do'):]
    assert 'run_seal_chain "$YESTERDAY" &' in main
    assert 'seal_chain_active' in main
    assert main.index('run_seal_chain "$YESTERDAY" &') < main.index('./build/ws_shadow')
    # main loop + watchdog both respect the chain's ingest pause
    assert '[ -f "$LIVE/export_pause" ] || ingest_alive || start_ingest' in main
    assert ': > "$EXPORT_ATTEMPT_LOG"' in text
    assert 'research_${research_date}.done' in text
    assert 'coverage audit failed' in text
    research = text[text.index('run_daily_research()'):text.index('run_seal_chain()')]
    assert research.index('identity_start=') < research.index('coverage_audit.py')
    assert research.rindex('--verify-seal') > research.index('mm_calibrate.py')
    assert 'identity_start" = "$identity_end' in research
    assert ' ) &' not in research  # research is SYNCHRONOUS inside the chain
    assert 'research_receipt_current' in text
    # raw pruning is seal-gated (prune_raw.py, fail-closed); no blind find -delete
    assert 'prune_raw.py --retention-days' in live
    assert '-delete' not in live
    for tool in ("mm_scan.py", "mm_backtest.py", "mm_calibrate.py"):
        line = next(x for x in text.splitlines()
                    if tool in x and not x.lstrip().startswith("#"))
        assert "--archive-only" in line, line


def test_production_supervisor_defaults_heavy_research_off_fail_closed():
    """PIPE-HOTFIX-02: production keeps seal/quality work but cannot start
    the three memory-heavy research tools unless an explicit audited override
    is present.  Invalid configuration narrows to OFF (S2/D2)."""
    path = os.path.join(ROOT, "tools", "pipeline_supervisor.sh")
    text = open(path).read()
    live = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    research = live[live.index("run_daily_research()"):
                    live.index("run_seal_chain()")]

    requested = 'AUTO_RESEARCH_REQUESTED="${AUTO_RESEARCH:-0}"'
    restored = 'AUTO_RESEARCH="$AUTO_RESEARCH_REQUESTED"'
    assert live.index(requested) < live.index('source "$CREDS"') \
        < live.index(restored)
    assert '*)' in live and 'AUTO_RESEARCH=0' in live
    assert research.index("coverage_audit.py") \
        < research.index('if [ "$AUTO_RESEARCH" != "1" ]') \
        < research.index("mm_scan.py")
    assert "AUTO_RESEARCH_DISABLED" in research
    assert "capture_gaps.py" in live

    service = open(os.path.join(
        ROOT, "deploy", "kalshi-pipeline.service")).read()
    assert "Environment=AUTO_RESEARCH=0" in service


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
    inv = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--operator-invalidate-seal", "test"],
        env=_export_env(wh), capture_output=True, text=True)
    assert inv.returncode == 0, inv.stdout + inv.stderr
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


def test_archive_only_reads_survive_raw_append_and_raw_pruning(exported_day):
    """Raw is verified ONCE at seal time; afterwards readers verify only the
    ARCHIVE against the seal (operator ruling 2026-07-11) — so post-seal raw
    appends do not poison reads, and pruned raw does not brick history."""
    wh, yd = exported_day["wh"], exported_day["yd"]
    raw = next(iter(glob.glob(os.path.join(
        wh, "raw", "date=%s" % yd.isoformat(), "*.ndjson*"))))
    with open(raw, "a") as f:
        f.write(json.dumps({"raw": "{}"}) + "\n")
    rel = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                         warehouse=wh, archive_only=True)
    assert rel.count("*").fetchone()[0] == 4
    _release_warehouse()
    import shutil
    shutil.rmtree(os.path.join(wh, "raw", "date=%s" % yd.isoformat()))
    rel = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                         warehouse=wh, archive_only=True)
    assert rel.count("*").fetchone()[0] == 4


def test_day_seal_rejects_uncheckpointed_cross_midnight_receipt(exported_day):
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    late = os.path.join(wh, "raw", "date=%s" % today.isoformat(),
                        "firehose_01.ndjson.1")
    with open(late, "w") as f:
        f.write(ti.trade("KXLATE-TEST-YES", _day_us(yd, 23, 3599),
                         "late-cross-day") + "\n")
    # the day is already sealed -> --seal is a write-once verify no-op;
    # park the seal (operator procedure) so a FRESH seal attempt runs
    inv = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--operator-invalidate-seal", "test"],
        env=_export_env(wh), capture_output=True, text=True)
    assert inv.returncode == 0, inv.stdout + inv.stderr
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
    inv = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--operator-invalidate-seal", "test"],
        env=_export_env(wh), capture_output=True, text=True)
    assert inv.returncode == 0, inv.stdout + inv.stderr
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
    assert bad.returncode == 3 and "SEALED" in bad.stderr
    assert open(seal_path, "rb").read() == before


def test_successful_seal_prunes_only_older_staging(exported_day):
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    staging = os.path.join(wh, "staging.duckdb")
    con = duckdb.connect(staging)
    old = today - datetime.timedelta(days=3)
    con.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_day_us(old, 12), "KXOLD-TEST-YES", "KXOLD", "KXOLD-TEST",
         None, None, None, "old-row", 5000, 5000, 10000, "yes",
         None, None, None, None])  # W-TL1 ladder columns (nullable)
    con.close()
    inv = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--operator-invalidate-seal", "test"],
        env=_export_env(wh), capture_output=True, text=True)
    assert inv.returncode == 0, inv.stdout + inv.stderr
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
    seal["version"] = 999
    with open(path, "w") as f:
        json.dump(seal, f)
    with pytest.raises(RuntimeError, match="stale or invalid archive seal"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_archive_only_rejects_unknown_seal_method(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    path = os.path.join(wh, "seals", "date=%s.json" % yd.isoformat())
    seal = json.load(open(path))
    seal["method"] = "bogus_method"
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
@pytest.mark.parametrize("offset,expected", [(-1, True)])
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


@pytest.mark.parametrize("module,extra", [
    (mm_scan, []),
    (mm_backtest, ["--markets", "T"]),
    (mm_calibrate, []),
])
def test_mm_tools_refuse_today_entirely(monkeypatch, module, extra):
    """Operator ruling 2026-07-11: research tools NEVER attach live staging —
    today (unsealed by definition) is a hard argparse error, and load() is
    never even reached."""
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    def forbidden_load(*a, **k):
        raise AssertionError("load() must not be reached for today")

    monkeypatch.setattr(module, "load", forbidden_load)
    with pytest.raises(SystemExit) as e:
        module.main([module.__file__, "--date", today] + extra)
    assert e.value.code == 2


@pytest.mark.parametrize("offset,expected", [(-1, True)])
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
    assert any('capture_gaps.py --date "$CHAIN_DATE"' in ln for ln in live), \
        "daily capture-gap record not wired into the seal chain"
    assert any("capture_gaps.py --live" in ln for ln in live), \
        "live capture-gap alert not wired into the supervisor watchdog loop"
    # next_actions.md item 1: daily coverage audit, non-zero exit surfaced.
    assert any('coverage_audit.py --date "$research_date"' in ln for ln in live), \
        "daily coverage audit not wired into the archive-only research function"
    assert any('run_daily_research "$CHAIN_DATE"' in ln for ln in live), \
        "sealed-day research function not invoked by the seal chain"
    assert any('run_seal_chain "$YESTERDAY" &' in ln for ln in live), \
        "seal chain not launched (backgrounded) from the main loop"


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


# ────────── operator seal ruling 2026-07-11: mandatory red fixtures ──────────

def _rewrite_csv_gz(path, mutate):
    import gzip
    with gzip.open(path, "rt") as f:
        lines = f.read().splitlines()
    lines = mutate(lines)
    with gzip.open(path, "wt") as f:
        f.write("\n".join(lines) + "\n")


def _flip_last_char(line):
    return line[:-1] + ("X" if line[-1] != "X" else "Y")


@pytest.mark.parametrize("name,mutate", [
    ("missing_line", lambda ls: ls[:1] + ls[2:]),
    ("duplicated_line", lambda ls: ls + [ls[-1]]),
    ("altered_line", lambda ls: ls[:-1] + [_flip_last_char(ls[-1])]),
    ("reordered_lines", lambda ls: [ls[0]] + list(reversed(ls[1:]))),
])
def test_sealed_archive_mutation_is_red(exported_day, name, mutate):
    """The four ruled red-proofs: a missing, duplicated, altered or REORDERED
    line inside a sealed archive file MUST break the reader-side seal gate."""
    wh, yd = exported_day["wh"], exported_day["yd"]
    victim = next(iter(glob.glob(os.path.join(
        wh, "facts", "trades", "category=*", "subcategory=*",
        "date=%s" % yd.isoformat(), "*.csv.gz"))))
    _rewrite_csv_gz(victim, mutate)
    with pytest.raises(RuntimeError, match="mismatch"):
        warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                       warehouse=wh, archive_only=True)


def test_seal_write_once_noop_keeps_bytes(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    seal_path = os.path.join(wh, "seals", "date=%s.json" % yd.isoformat())
    before = open(seal_path, "rb").read()
    ok = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--seal"],
        env=_export_env(wh), capture_output=True, text=True)
    assert ok.returncode == 0 and "write-once no-op" in ok.stdout, \
        ok.stdout + ok.stderr
    assert open(seal_path, "rb").read() == before


def test_operator_invalidate_parks_never_deletes(exported_day):
    wh, yd = exported_day["wh"], exported_day["yd"]
    seal_path = os.path.join(wh, "seals", "date=%s.json" % yd.isoformat())
    before = open(seal_path, "rb").read()
    ok = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--operator-invalidate-seal", "audit drill"],
        env=_export_env(wh), capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert not os.path.exists(seal_path)
    parked = glob.glob(os.path.join(
        wh, "seals", "date=%s.invalidated-operator.*.json" % yd.isoformat()))
    assert len(parked) == 1
    assert open(parked[0], "rb").read() == before
    ledger = [json.loads(line) for line in open(
        os.path.join(wh, "seal_invalidations.ndjson"))]
    assert ledger[-1]["event"] == "SEAL_INVALIDATED_BY_OPERATOR"
    assert ledger[-1]["reason"] == "audit drill"


def test_seal_carries_code_commit_and_sha256(exported_day):
    import re as _re
    wh, yd = exported_day["wh"], exported_day["yd"]
    seal = json.load(open(os.path.join(
        wh, "seals", "date=%s.json" % yd.isoformat())))
    assert seal["version"] == 2 and seal["method"] == "full_v2"
    assert seal["go_no_go_eligible"] is True and seal["unverified"] == []
    assert _re.fullmatch(r"[0-9a-f]{40}", seal["code_commit"])
    stats = seal["archive_file_stats"]
    assert stats and all(_re.fullmatch(r"[0-9a-f]{64}", r["sha256"])
                         for r in stats)


def test_legacy_seal_grades_and_permanent_ineligibility(exported_day):
    """Operator ruling 2026-07-11 option A: pre-seal-system history gets a
    legacy_v0 seal (archive self-consistency only, unverified items named in
    the seal) and is PERMANENTLY go/no-go ineligible."""
    import shutil
    wh, yd = exported_day["wh"], exported_day["yd"]
    inv = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--operator-invalidate-seal",
         "legacy migration test"],
        env=_export_env(wh), capture_output=True, text=True)
    assert inv.returncode == 0, inv.stdout + inv.stderr
    # legacy-seal refuses while raw is still present (full seal must be used)
    early = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--legacy-seal"],
        env=_export_env(wh), capture_output=True, text=True)
    assert early.returncode != 0 and "raw" in early.stderr
    shutil.rmtree(os.path.join(wh, "raw", "date=%s" % yd.isoformat()))
    ok = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--legacy-seal"],
        env=_export_env(wh), capture_output=True, text=True)
    assert ok.returncode == 0 and "LEGACY SEAL PASS" in ok.stdout, \
        ok.stdout + ok.stderr
    seal = json.load(open(os.path.join(
        wh, "seals", "date=%s.json" % yd.isoformat())))
    assert seal["method"] == "legacy_v0"
    assert seal["go_no_go_eligible"] is False
    assert seal["unverified"] == ["raw_byte_checkpoint",
                                  "staging_archive_content_identity"]
    _release_warehouse()
    rel = warehouse.load("trades", start=yd.isoformat(), end=yd.isoformat(),
                         warehouse=wh, archive_only=True)
    assert rel.count("*").fetchone()[0] == 4
    grades = warehouse.last_seal_grades()
    assert grades[yd.isoformat()] == {"method": "legacy_v0",
                                      "go_no_go_eligible": False}


def test_prune_raw_is_seal_gated_and_fail_closed(tmp_path):
    import prune_raw
    raw = tmp_path / "raw"
    whr = tmp_path / "wh"
    (raw / "date=2026-01-01").mkdir(parents=True)
    (raw / "date=2026-01-02").mkdir(parents=True)
    (whr / "seals").mkdir(parents=True)
    f_unsealed = raw / "date=2026-01-01" / "firehose_12.ndjson"
    f_cross = raw / "date=2026-01-02" / "firehose_01.ndjson"
    f_prunable = raw / "date=2026-01-02" / "firehose_12.ndjson"
    for f in (f_unsealed, f_cross, f_prunable):
        f.write_text("{}\n")
    (whr / "seals" / "date=2026-01-02.json").write_text(
        json.dumps({"status": "SEALED", "version": 2, "method": "full_v2"}))
    alert = tmp_path / "alert.json"
    argv = ["prune_raw", "--retention-days", "1", "--raw-root", str(raw),
            "--warehouse-root", str(whr), "--alert-path", str(alert)]
    # dry-run deletes nothing
    assert prune_raw.main(argv + ["--dry-run"]) == 0
    assert f_unsealed.exists() and f_cross.exists() and f_prunable.exists()
    # real run: only the sealed, non-cross-day file goes
    assert prune_raw.main(argv) == 0
    assert not f_prunable.exists()
    assert f_unsealed.exists(), "unsealed day must never be pruned"
    assert f_cross.exists(), \
        "hour-01 file must survive while the PREVIOUS day is unsealed"
    report = json.loads(alert.read_text())
    reasons = {r["reason"] for r in report["retained_overdue"]}
    assert "day_unsealed" in reasons and "cross_day_prev_unsealed" in reasons
    # fail-closed: a corrupt dependency ledger deletes NOTHING
    (whr / "seal_invalidations.ndjson").write_text("not json\n")
    f_new = raw / "date=2026-01-02" / "firehose_13.ndjson"
    f_new.write_text("{}\n")
    assert prune_raw.main(argv) == 1
    assert f_new.exists()


# ─────────────────── PIPE-W03: discovery + pause ownership ───────────────────

def test_unsealed_old_day_rotated_raw_stays_discoverable(tmp_path):
    """PIPE-W03 regression (2026-07-10 hour-13 incident): raw for a day OLDER
    than the fixed yesterday+today window — including WsRecorder rotation
    shards (.ndjson.1) — must still be discovered by the scanner as long as
    the day is UNSEALED. The pre-W03 scanner globbed only yesterday+today, so
    a day whose ingestion stalled (staging lock starvation) could leave the
    scan window with raw bytes never ingested and nothing would ever pick
    them up again. Sealed old days stay excluded (proven byte-complete)."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    raw_root = os.path.join(tmp, "raw")
    today = datetime.datetime.now(datetime.timezone.utc).date()
    old = today - datetime.timedelta(days=3)       # outside the old 2-day window
    sealed_old = today - datetime.timedelta(days=4)
    yd = today - datetime.timedelta(days=1)

    old_dir = os.path.join(raw_root, "date=%s" % old.isoformat())
    os.makedirs(old_dir)
    base = os.path.join(old_dir, "firehose_13.ndjson")
    shard = os.path.join(old_dir, "firehose_13.ndjson.1")  # rotation shard
    t_base, t_shard = _day_us(old, 13, 30), _day_us(old, 13, 930)
    with open(base, "w") as f:
        f.write(ti.tick("KXBTC-26DEC31-B71", t_base, 0.30, 0.40) + "\n")
    with open(shard, "w") as f:
        f.write(ti.tick("KXBTC-26DEC31-B71", t_shard, 0.31, 0.40) + "\n")

    sealed_dir = os.path.join(raw_root, "date=%s" % sealed_old.isoformat())
    os.makedirs(sealed_dir)
    sealed_file = os.path.join(sealed_dir, "firehose_05.ndjson")
    with open(sealed_file, "w") as f:
        f.write(ti.tick("KXBTC-26DEC31-B72", _day_us(sealed_old, 5), 0.10, 0.20) + "\n")
    os.makedirs(os.path.join(wh, "seals"))
    with open(os.path.join(wh, "seals",
                           "date=%s.json" % sealed_old.isoformat()), "w") as f:
        json.dump({"status": "SEALED", "version": 2, "method": "full_v2"}, f)

    yd_dir = os.path.join(raw_root, "date=%s" % yd.isoformat())
    os.makedirs(yd_dir)
    yd_file = os.path.join(yd_dir, "firehose_00.ndjson")
    with open(yd_file, "w") as f:
        f.write(ti.tick("KXBTC-26DEC31-B73", _day_us(yd, 0), 0.50, 0.60) + "\n")

    # the PRE-W03 production scanner: yesterday+today glob only — it NEVER
    # returns the old day's base or rotation shard (the incident class)
    old_window = []
    for d in (today - datetime.timedelta(days=1), today):
        old_window.extend(sorted(glob.glob(os.path.join(
            raw_root, "date=%s" % d.isoformat(), "*.ndjson*"))))
    assert base not in old_window and shard not in old_window

    cfg = {"raw_root": raw_root, "warehouse_root": wh}
    files = ingest.raw_files_to_scan(cfg)
    # new scanner discovers BOTH the base file and the rotation shard...
    assert base in files and shard in files
    # ...keeps scanning yesterday, and still excludes the SEALED old day
    assert yd_file in files
    assert sealed_file not in files

    # end-to-end: one scan pass ingests the stranded files and checkpoints
    # them byte-exactly, so a later seal's caught-up gate can pass
    ing = ti.new_ingester(tmp, wh)
    for path in files:
        ing.process_file(path)
    ck = dict(_q(ing, "SELECT file, byte_offset FROM checkpoint"))
    assert ck[os.path.abspath(base)] == os.path.getsize(base)
    assert ck[os.path.abspath(shard)] == os.path.getsize(shard)
    # both stranded ticks staged (Class-A hourly heartbeats add further rows
    # at hour boundaries as the ingest clock crosses days; not asserted here)
    n = _q(ing, "SELECT count(*) FROM orderbooks_l1 "
                "WHERE market_ticker='KXBTC-26DEC31-B71' "
                "AND ts_utc IN (%d, %d)" % (t_base, t_shard))[0][0]
    assert n == 2
    ing.con.close()


def test_caught_up_failure_reports_every_behind_file(tmp_path):
    """PIPE-W03: the caught-up gate reports ALL undiscovered/behind raw files,
    not only the first. The 2026-07-10 refusal named a single file
    (firehose_13.ndjson checkpoint=None) while ~11 further hours were equally
    missing — the blast radius was invisible until backfill."""
    tmp = str(tmp_path)
    wh = ti.make_warehouse(tmp)
    raw_root = os.path.join(tmp, "raw")
    today = datetime.datetime.now(datetime.timezone.utc).date()
    yd = today - datetime.timedelta(days=1)
    yd_dir = os.path.join(raw_root, "date=%s" % yd.isoformat())
    td_dir = os.path.join(raw_root, "date=%s" % today.isoformat())
    os.makedirs(yd_dir)
    os.makedirs(td_dir)

    ingested = os.path.join(yd_dir, "firehose_10.ndjson")
    missed_base = os.path.join(yd_dir, "firehose_13.ndjson")
    missed_shard = os.path.join(yd_dir, "firehose_13.ndjson.1")
    with open(ingested, "w") as f:
        f.write(ti.tick("KXBTC-26DEC31-B74", _day_us(yd, 10), 0.20, 0.30) + "\n")
    for path, sec in ((missed_base, 0), (missed_shard, 900)):
        with open(path, "w") as f:
            f.write(ti.tick("KXBTC-26DEC31-B74", _day_us(yd, 13, sec),
                            0.25, 0.35) + "\n")
    cross = []
    for hh in (0, 1):
        p = os.path.join(td_dir, "firehose_0%d.ndjson" % hh)
        with open(p, "w") as f:
            f.write(ti.tick("KXBTC-26DEC31-B74", _day_us(today, hh), 0.40, 0.50) + "\n")
        cross.append(p)

    ing = ti.new_ingester(tmp, wh)
    for path in (ingested, *cross):        # hour 13 base+shard NEVER ingested
        ing.process_file(path)
    ing.con.close()

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS stg (READ_ONLY)"
                % os.path.join(tmp, "staging.duckdb").replace("'", "''"))
    with pytest.raises(RuntimeError) as e:
        export_day.verify_raw_caught_up(con, raw_root, yd.isoformat(), wh)
    con.close()
    msg = str(e.value)
    assert "ingest checkpoint behind raw" in msg
    assert "2 file(s)" in msg
    assert os.path.abspath(missed_base) in msg
    assert os.path.abspath(missed_shard) in msg
    assert "checkpoint=None" in msg


def test_seal_evidence_records_per_family_discovery(exported_day):
    """PIPE-W03 (D2): the seal carries per file-family discovery evidence —
    hours/shards present on disk vs discovered by ingest, plus exchange-day
    hours with no file at all — so a never-discovered channel/hour can never
    again hide behind a green seal."""
    wh, yd, today = (exported_day[k] for k in ("wh", "yd", "today"))
    seal = json.load(open(os.path.join(
        wh, "seals", "date=%s.json" % yd.isoformat())))
    fams = seal["discovery_completeness"]
    assert set(fams) == {"firehose"}
    fam = fams["firehose"]
    # fixture: yesterday hour 00 + cross-day (today) hours 00/01, all ingested
    assert fam["files_present"] == 3
    assert fam["files_discovered"] == 3
    assert fam["undiscovered_files"] == []
    assert fam["hours"]["%s/00" % yd.isoformat()] == {"present": 1, "discovered": 1}
    assert fam["hours"]["%s/00" % today.isoformat()] == {"present": 1, "discovered": 1}
    assert fam["hours"]["%s/01" % today.isoformat()] == {"present": 1, "discovered": 1}
    # only hour 00 of the exchange day exists on disk; 01..23 recorded missing
    assert fam["exchange_day_hours_missing_on_disk"] == list(range(1, 24))


def test_discovery_completeness_is_per_family_and_shard_aware(tmp_path):
    """Forward-compatible with the planned l2_ raw family: families are keyed
    by filename prefix, shards aggregate into their hour bucket, and an
    undiscovered file is listed per family."""
    raw_root = str(tmp_path / "raw")
    day = "2026-07-10"
    ddir = os.path.join(raw_root, "date=%s" % day)
    os.makedirs(ddir)
    names = ["firehose_13.ndjson", "firehose_13.ndjson.1", "l2_13.ndjson"]
    files = []
    for n in names:
        p = os.path.join(ddir, n)
        with open(p, "w") as f:
            f.write("{}\n")
        files.append(p)
    ckpts = {os.path.abspath(files[0]): os.path.getsize(files[0]),
             os.path.abspath(files[1]): os.path.getsize(files[1])}
    fams = export_day.discovery_completeness(raw_root, day, files, ckpts)
    assert set(fams) == {"firehose", "l2"}
    assert fams["firehose"]["hours"]["%s/13" % day] == {"present": 2, "discovered": 2}
    assert fams["firehose"]["undiscovered_files"] == []
    assert fams["l2"]["files_present"] == 1
    assert fams["l2"]["files_discovered"] == 0
    assert fams["l2"]["undiscovered_files"] == [
        os.path.join("date=%s" % day, "l2_13.ndjson")]
    assert 13 not in fams["l2"]["exchange_day_hours_missing_on_disk"]
    assert 13 not in fams["firehose"]["exchange_day_hours_missing_on_disk"]


def _pause_functions_script():
    """Extract the two pause-ownership functions verbatim from the supervisor
    so the functional tests exercise the REAL production shell code."""
    text = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()

    def block(name):
        start = text.index("%s() {" % name)
        end = text.index("\n}", start)
        return text[start:end + 2]

    return block("acquire_export_pause") + "\n" + block("release_export_pause")


def _run_pause_scenario(live_dir, body):
    script = "set -u\nLIVE='%s'\n%s\n%s" % (live_dir,
                                            _pause_functions_script(), body)
    return subprocess.run(["bash", "-c", script],
                          capture_output=True, text=True)


def test_seal_chain_pause_ownership_foreign_pause_refused(tmp_path):
    """BACKLOG B4: a foreign/operator pause refuses the window and is NEVER
    deleted or rewritten — token-bearing content and bare `touch` alike."""
    live = str(tmp_path)
    for content in ("operator manual backfill hold\n", ""):
        pause = os.path.join(live, "export_pause")
        with open(pause, "w") as f:
            f.write(content)
        r = _run_pause_scenario(live, "acquire_export_pause")
        assert r.returncode != 0
        assert "refused" in r.stdout
        assert open(pause).read() == content, "foreign pause must be untouched"
        # release must also leave a foreign pause in place
        r2 = _run_pause_scenario(live, "release_export_pause")
        assert r2.returncode == 0
        assert os.path.exists(pause) and open(pause).read() == content
        os.unlink(pause)


def test_seal_chain_pause_ownership_own_lifecycle_and_stale_reclaim(tmp_path):
    live = str(tmp_path)
    pause = os.path.join(live, "export_pause")
    # normal lifecycle: acquire writes our token, release removes it
    r = _run_pause_scenario(
        live, 'acquire_export_pause && grep -qx "seal_chain pid=${BASHPID:-$$}" '
              '"$LIVE/export_pause" && release_export_pause')
    assert r.returncode == 0, r.stdout + r.stderr
    assert not os.path.exists(pause)
    # stale chain pause (dead pid, own token format) is reclaimed loudly
    r = _run_pause_scenario(
        live, '( : ) & dead=$!; wait "$dead"; '
              'echo "seal_chain pid=$dead" > "$LIVE/export_pause"; '
              'acquire_export_pause')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "reclaiming stale seal-chain export_pause" in r.stdout
    assert open(pause).read().startswith("seal_chain pid=")
    # a LIVE chain's pause is not stolen by another acquire
    r = _run_pause_scenario(
        live, 'sleep 5 & live_pid=$!; '
              'echo "seal_chain pid=$live_pid" > "$LIVE/export_pause"; '
              'acquire_export_pause; rc=$?; kill "$live_pid"; exit $rc')
    assert r.returncode != 0
    assert "held by live seal chain" in r.stdout


def test_seal_chain_pause_ownership_static_contract():
    """The chain acquires/releases ONLY via the ownership helpers; the old
    unconditional touch/rm of the pause file is gone; on a refused window the
    chain must not restart a paused ingest daemon."""
    text = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()
    live = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert 'touch "$LIVE/export_pause"' not in live
    assert 'rm -f "$LIVE/export_pause"' not in live
    chain = text[text.index("run_seal_chain()"):text.index("\nwhile true; do")]
    assert "acquire_export_pause" in chain and "release_export_pause" in chain
    # window structure: acquire gates BOTH the ingest stop and the restart
    assert chain.index("acquire_export_pause") \
        < chain.index("stop_ingest_for_export") \
        < chain.index("release_export_pause") \
        < chain.index("ingest_alive || start_ingest")
    # rm -f of the pause appears only inside release_export_pause
    release = _pause_functions_script()
    assert 'rm -f "$pause"' in release
    assert live.count('rm -f "$pause"') == 1


# ─────────────── PIPE-W06 Stage 1: targeted sports L2 (LAYER 1b) ───────────────

def _l2_frame(msg_type, mt, ts_us, sid, seq, **msg_extra):
    """One l2_ raw capture line: WsRecorder envelope (compact, sid/seq/
    channel/source_ticker before "raw" — src/storage.cpp) around a Kalshi
    orderbook frame whose TOP-LEVEL sid/seq is what ingest stages into
    orderbooks_full.ws_sid/ws_seq (W5)."""
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000}
    msg.update(msg_extra)
    frame = {"type": msg_type, "sid": sid, "seq": seq, "msg": msg}
    env = {"recv_mono_ns": 1, "recv_wall_ns": ts_us * 1000, "source": "Kalshi",
           "channel": msg_type, "source_ticker": mt, "source_sequence": seq,
           "sid": sid, "raw": json.dumps(frame, separators=(",", ":"))}
    return json.dumps(env, separators=(",", ":"))


def test_l2_raw_family_is_discovered_ingested_and_sealed_with_evidence(tmp_path):
    """PIPE-W06 D4 gate: the NEW capture-side naming family (l2_<HH>.ndjson +
    rotation shards) flows through the WHOLE ingest side in one pass —
    (a) discovered by the production scanner, (b) typed into orderbooks_full
    with ws_sid/ws_seq, (c) byte-exactly checkpointed, and (d) the day seal's
    discovery_completeness carries an `l2` family entry proving it."""
    wh = str(tmp_path / "warehouse")
    _cls_parquet(wh, EXPORT_CLS)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    yd = today - datetime.timedelta(days=1)
    raw_root = os.path.join(wh, "raw")
    mt = "KXMLB-26JUL12-BOS"

    yd_dir = os.path.join(raw_root, "date=%s" % yd.isoformat())
    td_dir = os.path.join(raw_root, "date=%s" % today.isoformat())
    os.makedirs(yd_dir)
    os.makedirs(td_dir)
    with open(os.path.join(yd_dir, "firehose_00.ndjson"), "w") as f:
        f.write(ti.tick("KXBTC-26DEC31-B75", _day_us(yd, 0, 30), 0.30, 0.40) + "\n")
        f.write(ti.trade(mt, _day_us(yd, 0, 40), "w06-l2-t1") + "\n")
    l2_base = os.path.join(yd_dir, "l2_13.ndjson")
    l2_shard = os.path.join(yd_dir, "l2_13.ndjson.1")
    with open(l2_base, "w") as f:
        f.write(_l2_frame("orderbook_snapshot", mt, _day_us(yd, 13, 5), 7, 1,
                          yes_dollars_fp=[["0.4000", "100.00"]],
                          no_dollars_fp=[["0.5900", "80.00"]]) + "\n")
        f.write(_l2_frame("orderbook_delta", mt, _day_us(yd, 13, 6), 7, 2,
                          side="yes", price_dollars="0.4100",
                          delta_fp="25.00") + "\n")
    with open(l2_shard, "w") as f:  # rotation shard continues the stream
        f.write(_l2_frame("orderbook_delta", mt, _day_us(yd, 13, 7), 7, 3,
                          side="no", price_dollars="0.5800",
                          delta_fp="-10.00") + "\n")
    for hh in (0, 1):  # closed cross-day receipts the seal requires
        with open(os.path.join(td_dir, "firehose_0%d.ndjson" % hh), "w") as f:
            f.write(ti.tick("KXBTC-26DEC31-B75", _day_us(today, hh, 30),
                            0.50, 0.60) + "\n")

    # (a) the production scanner discovers the l2 family (base AND shard)
    files = ingest.raw_files_to_scan(
        {"raw_root": raw_root, "warehouse_root": wh})
    assert l2_base in files and l2_shard in files

    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    ing = ingest.Ingester(con, wh, raw_root=raw_root)
    for path in files:
        ing.process_file(path)

    # (b) typed rows in orderbooks_full, per-sid seq carried (W5 columns)
    rows = con.execute(
        "SELECT msg_type, ws_sid, ws_seq, category FROM orderbooks_full "
        "WHERE market_ticker=? ORDER BY ws_seq", [mt]).fetchall()
    assert rows == [("snapshot", 7, 1, "Sports"), ("delta", 7, 2, "Sports"),
                    ("delta", 7, 3, "Sports")]
    # (c) byte-exact checkpoints on both l2 segments
    ck = dict(con.execute("SELECT file, byte_offset FROM checkpoint").fetchall())
    assert ck[os.path.abspath(l2_base)] == os.path.getsize(l2_base)
    assert ck[os.path.abspath(l2_shard)] == os.path.getsize(l2_shard)
    con.close()

    # (d) export + WRITE-ONCE seal; the seal evidence names the l2 family
    env = _export_env(wh)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
                        "--date", yd.isoformat()], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "EXPORT PASS" in r.stdout, r.stdout + r.stderr
    seal_run = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--seal"], env=env,
        capture_output=True, text=True)
    assert seal_run.returncode == 0 and "DAY SEAL PASS" in seal_run.stdout, \
        seal_run.stdout + seal_run.stderr
    seal = json.load(open(os.path.join(
        wh, "seals", "date=%s.json" % yd.isoformat())))
    fams = seal["discovery_completeness"]
    assert set(fams) == {"firehose", "l2"}
    l2 = fams["l2"]
    assert l2["files_present"] == 2 and l2["files_discovered"] == 2
    assert l2["undiscovered_files"] == []
    assert l2["hours"]["%s/13" % yd.isoformat()] == {"present": 2, "discovered": 2}
    assert 13 not in l2["exchange_day_hours_missing_on_disk"]


def test_supervisor_layer1b_independence_and_disable_static_contract():
    """PIPE-W06 hard rules, pinned on live supervisor lines: LAYER 1b is
    launched backgrounded BEFORE (and never waited on by) the LAYER 1
    firehose; every L2 failure mode lives inside run_l2_shadow; one-touch
    disable is checked pre-start AND polled mid-segment; the selector refresh
    precedes a fail-closed freshness gate which precedes the launch; the l2
    instance is pinned to orderbook_delta / explicit tickers / its own
    capture family, metrics file, log and lock; the seal chain records the
    NON-GATING l2 quality evidence next to capture_gaps."""
    sup = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()
    live = "\n".join(ln for ln in sup.splitlines()
                     if not ln.lstrip().startswith("#"))
    # launched fire-and-forget from the main loop, before the firehose launch
    assert 'run_l2_shadow >> "$L2_LOG" 2>&1 &' in live
    main = live[live.index("\nwhile true; do"):]
    assert main.index('run_l2_shadow >> "$L2_LOG" 2>&1 &') \
        < main.index("./build/ws_shadow")
    # the firehose waits ONLY on its own pid; L2 is never waited on
    assert 'wait "$WS_PID"' in live and 'wait "$L2_PID"' not in live

    start = sup.index("run_l2_shadow() {")
    fn = sup[start:sup.index("\n}", start) + 2]
    fn_live = "\n".join(ln for ln in fn.splitlines()
                        if not ln.lstrip().startswith("#"))
    # one-touch disable: pre-start gate + per-segment gate + mid-segment poll
    assert fn_live.count('-f "$L2_DISABLE"') >= 3
    assert 'kill "$L2_WS_PID"' in fn_live
    # selector refresh -> fail-closed --check gate -> launch, in that order
    assert fn_live.index("python3 tools/l2_targets.py") \
        < fn_live.index("--check") < fn_live.index("./build/ws_shadow")
    assert 'l2_alert "no_targets"' in fn_live      # stale/missing => no start
    assert 'return 0' in fn_live                    # refusals never propagate
    # W-A4 single-REST-owner: rest_disabled suspends the selector's REST spend
    assert '-f "$LIVE/rest_disabled"' in fn_live
    # pinned launch env: delta channel, explicit tickers, no firehose flag,
    # own hourly capture family, own metrics (never work/metrics.ndjson),
    # zero REST cross-check
    assert "KALSHI_WS_CHANNELS=orderbook_delta" in fn_live
    assert 'KALSHI_WS_TICKERS="$L2_TICKERS"' in fn_live
    assert "KALSHI_WS_FIREHOSE=0" in fn_live
    assert 'KALSHI_SHADOW_CAPTURE="$L2_DAYDIR/l2_$L2_HH.ndjson"' in fn_live
    assert 'KALSHI_SHADOW_METRICS="$LIVE/l2_metrics.ndjson"' in fn_live
    assert "KALSHI_SHADOW_XCHECK=0" in fn_live
    # own single-instance lock, distinct from the supervisor's
    assert 'L2_LOCK="$LIVE/l2_shadow.lock"' in live
    assert 'LOCK="$LIVE/supervisor.lock"' in live
    # crash retry stays inside the hour segment, with backoff
    assert "retrying in 15s" in fn
    # supervisor cleanup also reaps the L2 runner + its ws_shadow
    cleanup = sup[sup.index("cleanup() {"):sup.index("\n}", sup.index("cleanup() {"))]
    assert '"$L2_PID"' in cleanup and "$L2_LOCK/ws_pid" in cleanup
    # seal chain: l2_gap_check wired post-seal, non-gating, AFTER capture_gaps
    # and BEFORE research — with its own done-marker (idempotent per day)
    chain = live[live.index("run_seal_chain()"):live.index("\nwhile true; do")]
    assert 'l2_gap_check.py --date "$CHAIN_DATE"' in chain
    assert "l2_gaps_${CHAIN_DATE}.done" in chain
    assert chain.index("capture_gaps.py") < chain.index("l2_gap_check.py") \
        < chain.index('run_daily_research "$CHAIN_DATE"')


def _l2_functions_script():
    """Extract l2_alert + run_l2_shadow VERBATIM from the supervisor so the
    functional tests below exercise the REAL production shell code (same
    pattern as _pause_functions_script)."""
    text = open(os.path.join(ROOT, "tools", "pipeline_supervisor.sh")).read()

    def block(name):
        start = text.index("%s() {" % name)
        end = text.index("\n}", start)
        return text[start:end + 2]

    return block("l2_alert") + "\n" + block("run_l2_shadow")


def _run_l2_scenario(tmp, body, timeout=60):
    """Drive the extracted LAYER 1b functions with stubbed python3/ws_shadow.

    python3 shim: the selector refresh is a no-op; `--check` emits the ticker
    list only when the scenario planted $LIVE/targets_ok (else exit 3 — the
    fail-closed gate). ws_shadow stub: records its env + capture path, then
    exits 0 immediately (or sleeps 60 s when $LIVE/ws_slow exists, for the
    mid-segment kill scenario)."""
    live = os.path.join(tmp, "work", "live")
    os.makedirs(live, exist_ok=True)
    os.makedirs(os.path.join(tmp, "work", "raw"), exist_ok=True)
    bindir = os.path.join(tmp, "stub_bin")
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(os.path.join(tmp, "build"), exist_ok=True)
    shim = os.path.join(bindir, "python3")
    with open(shim, "w") as f:
        f.write('#!/bin/sh\ncase "$*" in\n'
                '  *--check*) if [ -f "%s/targets_ok" ]; then '
                'echo "TICK-A,TICK-B"; exit 0; else exit 3; fi ;;\n'
                '  *) exit 0 ;;\nesac\n' % live)
    os.chmod(shim, 0o755)
    ws = os.path.join(tmp, "build", "ws_shadow")
    with open(ws, "w") as f:
        f.write('#!/bin/sh\nprintenv > "%s/ws_env.txt"\n'
                'echo "$KALSHI_SHADOW_CAPTURE" > "%s/ws_capture.txt"\n'
                'if [ -f "%s/ws_slow" ]; then sleep 60; fi\nexit 0\n'
                % (live, live, live))
    os.chmod(ws, 0o755)
    script = ("set -u\nexport PATH='%s':\"$PATH\"\ncd '%s'\n"
              "RAW='work/raw'\nLIVE='work/live'\n"
              'L2_TARGETS_CSV="$LIVE/l2_targets.csv"\n'
              'L2_DISABLE="$LIVE/l2_disable"\n'
              'L2_LOG="$LIVE/l2_shadow.log"\n'
              'L2_LOCK="$LIVE/l2_shadow.lock"\n'
              'L2_ALERT="$LIVE/l2_alert.json"\n'
              "L2_TARGETS_MAX_AGE_SECS=7200\nL2_POLL_SECS=1\n"
              "L2_MIN_SEGMENT_SECS=0\n%s\n%s\n"
              % (bindir, tmp, _l2_functions_script(), body))
    return subprocess.run(["bash", "-c", script],
                          capture_output=True, text=True, timeout=timeout)


def test_l2_shadow_disable_flag_prevents_start(tmp_path):
    tmp = str(tmp_path)
    r = _run_l2_scenario(
        tmp, 'touch "$LIVE/targets_ok"; touch "$LIVE/l2_disable"; run_l2_shadow')
    assert r.returncode == 0, r.stdout + r.stderr
    live = os.path.join(tmp, "work", "live")
    assert not os.path.exists(os.path.join(live, "ws_env.txt")), \
        "l2 ws_shadow must not launch while l2_disable exists"
    alert = json.load(open(os.path.join(live, "l2_alert.json")))
    assert alert["status"] == "disabled"


def test_l2_shadow_fails_closed_without_fresh_targets(tmp_path):
    tmp = str(tmp_path)
    r = _run_l2_scenario(tmp, "run_l2_shadow")  # no targets_ok => --check fails
    assert r.returncode == 0, r.stdout + r.stderr  # refusal never propagates (P4)
    live = os.path.join(tmp, "work", "live")
    assert not os.path.exists(os.path.join(live, "ws_env.txt"))
    alert = json.load(open(os.path.join(live, "l2_alert.json")))
    assert alert["status"] == "no_targets"
    assert "fail-closed" in r.stdout


def test_l2_shadow_launch_env_pins_channel_tickers_and_own_paths(tmp_path):
    tmp = str(tmp_path)
    r = _run_l2_scenario(tmp, 'touch "$LIVE/targets_ok"; run_l2_shadow')
    assert r.returncode == 0, r.stdout + r.stderr
    live = os.path.join(tmp, "work", "live")
    env = dict(ln.split("=", 1) for ln in
               open(os.path.join(live, "ws_env.txt")).read().splitlines()
               if "=" in ln)
    assert env["KALSHI_WS_CHANNELS"] == "orderbook_delta"
    assert env["KALSHI_WS_TICKERS"] == "TICK-A,TICK-B"
    assert env["KALSHI_WS_FIREHOSE"] == "0"
    assert env["KALSHI_MODE"] == "data_collect"
    assert env["KALSHI_SHADOW_XCHECK"] == "0"
    assert env["KALSHI_SHADOW_METRICS"] == "work/live/l2_metrics.ndjson"
    cap = open(os.path.join(live, "ws_capture.txt")).read().strip()
    import re as _re
    assert _re.fullmatch(
        r"work/raw/date=\d{4}-\d{2}-\d{2}/l2_\d{2}\.ndjson", cap), cap
    # healthy run: alert cleared, single-instance lock released
    assert not os.path.exists(os.path.join(live, "l2_alert.json"))
    assert not os.path.exists(os.path.join(live, "l2_shadow.lock"))


def test_l2_shadow_mid_segment_disable_kills_the_running_segment(tmp_path):
    tmp = str(tmp_path)
    body = ('touch "$LIVE/targets_ok"; touch "$LIVE/ws_slow"\n'
            "run_l2_shadow & rp=$!\n"
            "for i in $(seq 1 100); do\n"
            '  [ -f "$LIVE/ws_env.txt" ] && break; sleep 0.2\ndone\n'
            'touch "$LIVE/l2_disable"\nwait "$rp"\n')
    start = time.time()
    r = _run_l2_scenario(tmp, body, timeout=45)
    elapsed = time.time() - start
    assert r.returncode == 0, r.stdout + r.stderr
    # the stub sleeps 60s; a working mid-segment kill returns far sooner
    assert elapsed < 30, "mid-segment l2_disable did not kill the segment"
    live = os.path.join(tmp, "work", "live")
    assert os.path.exists(os.path.join(live, "ws_env.txt"))  # it HAD started
    alert = json.load(open(os.path.join(live, "l2_alert.json")))
    assert alert["status"] == "disabled"
    assert not os.path.exists(os.path.join(live, "l2_shadow.lock"))


# ───────────────────────── W-A (B15 + B16) ─────────────────────────


def test_rebuild_state_bounded_to_active_window(tmp_path):
    """B15: a fresh Ingester rebuilds last-L1-state from the trailing
    active_us window behind max(ts_utc) only. Every heartbeat-eligible market
    (last_seen within active_us of the newest tick) is restored with values
    identical to the old full-table window scan; a market silent longer than
    that (its heartbeats exhausted too) starts stateless — the documented
    INGEST_SKIP_REBUILD snapshot contract."""
    wh = str(tmp_path / "warehouse")
    _cls_parquet(wh, EXPORT_CLS)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    d3 = today - datetime.timedelta(days=3)
    fresh, stale = "KXBTC-26DEC31-FRESH", "KXBTC-26DEC31-STALE"
    _ingest_lines(wh, [
        ti.tick(stale, _day_us(d3, 12), 0.0400, 0.0500),
        ti.tick(fresh, _day_us(d3, 12), 0.0100, 0.0200),
    ], name=os.path.join("raw", "date=%s" % d3.isoformat(),
                         "firehose_12.ndjson"))
    _ingest_lines(wh, [
        ti.tick(fresh, _day_us(today, 0, 30), 0.0325, 0.0450),
    ], name=os.path.join("raw", "date=%s" % today.isoformat(),
                         "firehose_00.ndjson"))
    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    try:
        ing = ingest.Ingester(con, wh)
        assert fresh in ing.state, sorted(ing.state)
        assert stale not in ing.state, sorted(ing.state)
        # bounded result == unbounded full-table scan for the restored market
        full = {r[0]: r[1:] for r in con.execute("""
            SELECT market_ticker, yes_bid_e4, yes_bid_qty_e4, yes_ask_e4,
                   yes_ask_qty_e4, ts_utc FROM orderbooks_l1
            QUALIFY row_number() OVER
              (PARTITION BY market_ticker ORDER BY ts_utc DESC)=1""").fetchall()}
        assert ing.state[fresh] == full[fresh][:4]
        assert ing.last_seen[fresh] == full[fresh][4]
        # stale WAS in the full-table result — that is exactly the dead
        # weight the bound drops
        assert stale in full
        mx = con.execute("SELECT max(ts_utc) FROM orderbooks_l1").fetchone()[0]
        assert ing.global_hour == mx // ingest.HOUR_US
    finally:
        con.close()


def test_rebuild_state_empty_staging(tmp_path):
    """B15 edge: empty staging rebuilds to empty state, global_hour=None."""
    wh = str(tmp_path / "warehouse")
    _cls_parquet(wh, EXPORT_CLS)
    con = duckdb.connect(os.path.join(wh, "staging.duckdb"))
    try:
        ing = ingest.Ingester(con, wh)
        assert ing.state == {} and ing.global_hour is None
    finally:
        con.close()


def test_prune_sealed_removes_sealed_day_keeps_unsealed(exported_day):
    """B16: --prune-sealed deletes the SEALED day's staging rows in the same
    window (archive is authoritative), keeps today and any UNSEALED past day,
    and leaves the checkpoint table alone (seal caught-up proofs read it)."""
    wh, yd, today = (exported_day["wh"], exported_day["yd"],
                     exported_day["today"])
    staging = os.path.join(wh, "staging.duckdb")
    yd_lo = int(datetime.datetime(yd.year, yd.month, yd.day,
                tzinfo=datetime.timezone.utc).timestamp() * 1_000_000)
    yd_hi = yd_lo + 86_400_000_000
    con = duckdb.connect(staging)
    n_yd_before = sum(con.execute(
        "SELECT count(*) FROM %s WHERE ts_utc >= ? AND ts_utc < ?" % t,
        [yd_lo, yd_hi]).fetchone()[0]
        for t in ("orderbooks_l1", "orderbooks_full", "trades"))
    assert n_yd_before > 0  # sealed day still staged (old retain window)
    n_today_before = con.execute(
        "SELECT count(*) FROM trades WHERE ts_utc >= ?",
        [yd_hi]).fetchone()[0]
    # inject an UNSEALED older-day row (simulates a stuck unsealed backlog)
    con.execute("INSERT INTO trades SELECT * REPLACE "
                "(ts_utc - 2*86400000000 AS ts_utc, "
                "'wa-unsealed' AS trade_id) FROM trades LIMIT 1")
    n_ckpt = con.execute("SELECT count(*) FROM checkpoint").fetchone()[0]
    assert n_ckpt > 0
    con.close()
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "export_day.py"),
         "--date", yd.isoformat(), "--prune-sealed"],
        env=_export_env(wh), capture_output=True, text=True)
    assert r.returncode == 0 and "PRUNE-SEALED PASS" in r.stdout, \
        r.stdout + r.stderr
    assert "removed" in r.stdout and "NOT sealed" in r.stdout, r.stdout
    con = duckdb.connect(staging, read_only=True)
    try:
        n_yd_after = sum(con.execute(
            "SELECT count(*) FROM %s WHERE ts_utc >= ? AND ts_utc < ?" % t,
            [yd_lo, yd_hi]).fetchone()[0]
            for t in ("orderbooks_l1", "orderbooks_full", "trades"))
        assert n_yd_after == 0, "sealed day must leave staging"
        assert con.execute(
            "SELECT count(*) FROM trades WHERE trade_id='wa-unsealed'"
            ).fetchone()[0] == 1, "unsealed day must be kept"
        assert con.execute(
            "SELECT count(*) FROM trades WHERE ts_utc >= ?",
            [yd_hi]).fetchone()[0] == n_today_before, "today must be untouched"
        assert con.execute(
            "SELECT count(*) FROM checkpoint").fetchone()[0] == n_ckpt
    finally:
        con.close()
