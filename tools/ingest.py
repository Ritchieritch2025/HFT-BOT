#!/usr/bin/env python3
"""Change-only ingester: raw firehose logs -> staging DuckDB (LAYER 2).

Per docs/warehouse_schema.md. Reads capture NDJSON (each line a RawRecord whose
`raw` is a WS frame), resolves the snowflake join at ingest, applies the class
policy + change-only L1 dedup + hourly heartbeats, and inserts typed rows. Byte
offsets are checkpointed inside the staging DB so restarts are gap/dup-free.

Modes:
  one-shot : python3 tools/ingest.py CAPTURE.ndjson [...]      (tests, migration)
  daemon   : python3 tools/ingest.py --loop [SECONDS]          (24/7 pipeline)
             scans raw_root/date=<today|yesterday>/*.ndjson every cycle and
             advances the heartbeat clock on wall time as well as data time.

TYPES (locked — no downcasting; Kalshi is sub-penny + fractional):
  prices     -> INT32  E4  (dollars * 10000, e.g. "0.9900" -> 9900)
  quantities -> BIGINT E4  (size   * 10000, e.g. "5119.00" -> 51190000)
  timestamps -> BIGINT epoch MICROSECONDS (UTC)
  is_snapshot-> BOOLEAN (heartbeat / first-observation = true; real change = false)

TIMESTAMP LADDER (W-TL1): every fact row additionally carries exchange_ts_us +
recv_wall_ns + recv_mono_ns + local_recv_ts_us (all nullable BIGINT; scheduled
heartbeats = all NULL). ts_utc = COALESCE(exchange_ts_us, local_recv_ts_us) is
legacy-compat only — never a tradable replay clock (see comment on STAGING_DDL).

CHANGE-ONLY + HEARTBEAT (orderbooks_l1, Class A markets only):
  state = (yes_bid_e4, yes_bid_qty_e4, yes_ask_e4, yes_ask_qty_e4)
  - first observation of a market       -> write, is_snapshot=true
  - tick in a new clock-hour            -> write, is_snapshot=true (counts as heartbeat)
  - state changed within the hour       -> write, is_snapshot=false
  - otherwise                           -> skip
  SCHEDULER: whenever the global clock (max data ts; wall clock too in --loop)
  crosses an hour boundary, every active-session market that has no snapshot for
  that hour gets a heartbeat row written from remembered state (ts = hour start;
  price/volume/oi are NULL on scheduled heartbeats — book fields only).
  "Active-session" = a tick was seen within heartbeat_active_hours (default 24h).
  Reconstruction: book at T = most recent row <= T (LOCF); max lookback = 1h.

Trades are recorded for EVERY market regardless of class. orderbook_snapshot /
orderbook_delta frames (watchlist runs) land in orderbooks_full as typed rows,
carrying the frame-level per-sid WS sequence (ws_sid/ws_seq, nullable — W5).
Per-day per-category tick counts land in ingest_stats (compression visibility).
stdlib + duckdb only.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

HOUR_US = 3_600_000_000


def _fsync_dir(path):
    """Persist same-filesystem rename/create directory entries (POSIX)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# W-TL1 (2026-07-09): timestamp ladder. Four additive nullable BIGINT columns
# on all three fact tables:
#   exchange_ts_us   exchange-reported time (ms-granular! `ts_ms` authoritative,
#                    legacy `ts` fallback) normalized to epoch micros
#   recv_wall_ns     capture-host wall clock at WS receive, raw envelope value
#   recv_mono_ns     capture-host monotonic clock at WS receive, raw envelope
#                    value (arbitrary epoch — only differences are meaningful,
#                    and only within one stream_epoch/connection)
#   local_recv_ts_us recv_wall_ns // 1000 (epoch micros) — the tradable clock
# ts_utc is kept for LEGACY COMPATIBILITY ONLY: ts_utc =
# COALESCE(exchange_ts_us, local_recv_ts_us). It is used for partitioning /
# day export / coarse queries / old tools. It MUST NOT be used as a tradable
# replay clock: on most rows it is EXCHANGE time, and backtesting decisions on
# it is look-ahead bias (see tools/mm_backtest.py --clock). Old archive files
# are never rewritten; pre-TL1 rows read back NULL in all four columns.
STAGING_DDL = """
CREATE TABLE IF NOT EXISTS checkpoint (
  file TEXT PRIMARY KEY, byte_offset BIGINT, updated_us BIGINT);
CREATE TABLE IF NOT EXISTS orderbooks_l1 (
  ts_utc BIGINT, market_ticker TEXT, series_ticker TEXT, event_ticker TEXT,
  category TEXT, subcategory TEXT, "group" TEXT, record_class TEXT,
  yes_bid_e4 INTEGER, yes_bid_qty_e4 BIGINT, yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT,
  price_e4 INTEGER, volume_e4 BIGINT, open_interest_e4 BIGINT, is_snapshot BOOLEAN,
  exchange_ts_us BIGINT, recv_wall_ns BIGINT, recv_mono_ns BIGINT,
  local_recv_ts_us BIGINT);
CREATE TABLE IF NOT EXISTS trades (
  ts_utc BIGINT, market_ticker TEXT, series_ticker TEXT, event_ticker TEXT,
  category TEXT, subcategory TEXT, "group" TEXT,
  trade_id TEXT, yes_price_e4 INTEGER, no_price_e4 INTEGER, count_e4 BIGINT, taker_side TEXT,
  exchange_ts_us BIGINT, recv_wall_ns BIGINT, recv_mono_ns BIGINT,
  local_recv_ts_us BIGINT);
CREATE TABLE IF NOT EXISTS orderbooks_full (
  ts_utc BIGINT, market_ticker TEXT, series_ticker TEXT, event_ticker TEXT,
  category TEXT, subcategory TEXT, "group" TEXT,
  msg_type TEXT, side TEXT, price_e4 INTEGER, delta_e4 BIGINT,
  yes_levels TEXT, no_levels TEXT, ws_sid BIGINT, ws_seq BIGINT,
  exchange_ts_us BIGINT, recv_wall_ns BIGINT, recv_mono_ns BIGINT,
  local_recv_ts_us BIGINT);
CREATE TABLE IF NOT EXISTS ingest_stats (
  day TEXT, category TEXT, ticks_seen BIGINT, l1_written BIGINT, trades_written BIGINT,
  PRIMARY KEY (day, category));
"""

LADDER_COLS = ("exchange_ts_us", "recv_wall_ns", "recv_mono_ns", "local_recv_ts_us")

# W5 (2026-07-07): per-sid WS seq. orderbook_snapshot / orderbook_delta frames
# carry top-level `sid` + `seq` ints (verified against live captures, W3.3) —
# the per-subscription sequence that fixes the known ordering defect (same-µs
# deltas were ordered only by file position). Additive + nullable: frames
# without them (or pre-W5 rows) stay NULL, never required.
#
# W-TL1: EVERY insert uses an explicit column list (the FULL_INSERT pattern),
# so a column-count change can never silently shift values by position on
# either a fresh or an ALTER-migrated staging DB.
L1_COLS = ("ts_utc", "market_ticker", "series_ticker", "event_ticker",
           "category", "subcategory", '"group"', "record_class",
           "yes_bid_e4", "yes_bid_qty_e4", "yes_ask_e4", "yes_ask_qty_e4",
           "price_e4", "volume_e4", "open_interest_e4", "is_snapshot") + LADDER_COLS
TRADE_COLS = ("ts_utc", "market_ticker", "series_ticker", "event_ticker",
              "category", "subcategory", '"group"', "trade_id", "yes_price_e4",
              "no_price_e4", "count_e4", "taker_side") + LADDER_COLS
FULL_COLS = ("ts_utc", "market_ticker", "series_ticker", "event_ticker",
             "category", "subcategory", '"group"', "msg_type", "side",
             "price_e4", "delta_e4", "yes_levels", "no_levels",
             "ws_sid", "ws_seq") + LADDER_COLS
L1_INSERT = "INSERT INTO orderbooks_l1 (%s) VALUES (%s)" % (
    ", ".join(L1_COLS), ", ".join(["?"] * len(L1_COLS)))
TRADE_INSERT = "INSERT INTO trades (%s) VALUES (%s)" % (
    ", ".join(TRADE_COLS), ", ".join(["?"] * len(TRADE_COLS)))
FULL_INSERT = "INSERT INTO orderbooks_full (%s) VALUES (%s)" % (
    ", ".join(FULL_COLS), ", ".join(["?"] * len(FULL_COLS)))


def ws_int(v):
    """Frame-level sid/seq: a real int passes through; anything else -> NULL
    (boundary validation, D3 — additive column, never required)."""
    return v if type(v) is int else None


def e4(v):
    """Dollar/size string or number -> integer scaled by 10000 (full precision)."""
    if v is None or v == "":
        return None
    try:
        return int(round(float(v) * 10000))
    except (ValueError, TypeError):
        return None


def levels_e4(msg, side):
    """Snapshot book levels -> compact JSON [[price_e4, qty_e4], ...].

    Kalshi sends `yes_dollars_fp` / `no_dollars_fp` as [["0.0100","310.00"], ...]
    (verified against live capture). Legacy `yes` / `no` carried integer cents,
    which need *100 to reach E4."""
    levels = msg.get(side + "_dollars_fp")
    cents = False
    if levels is None:
        levels = msg.get(side)
        cents = True
    out = []
    for pair in levels or []:
        try:
            p, q = pair[0], pair[1]
        except (TypeError, IndexError):
            continue
        pe = e4(p) if not cents else (int(p) * 100 if p is not None else None)
        qe = e4(q)
        if pe is not None and qe is not None:
            out.append([pe, qe])
    return json.dumps(out, separators=(",", ":"))


def split_ticker(mt):
    """market_ticker = {event}-{outcome}; event = {series}-{event_id}."""
    if not mt:
        return None, None
    series = mt.split("-", 1)[0]
    event = mt.rsplit("-", 1)[0] if "-" in mt else mt
    return series, event


# Valid Kalshi ticker shape. Interleaved-write corruption can splice another
# message's bytes INTO a string field while the line stays parseable JSON, so
# every ticker is validated before it can reach staging.
TICKER_RE = re.compile(r"^[A-Z0-9._-]{3,80}$")

# Plausibility window for normalized timestamps (2020-01-01 .. 2036-01-01 UTC).
# Interleaved/corrupt capture lines can carry absurd numbers; those frames are
# skipped and counted rather than poisoning staging or crashing the daemon.
TS_MIN_US = 1_577_836_800_000_000
TS_MAX_US = 2_082_758_400_000_000


def normalize_ts_us(v):
    """Numeric timestamp of unknown unit (s/ms/us/ns) -> epoch micros, or None."""
    try:
        v = int(v)
    except (ValueError, TypeError):
        return None
    for scaled in (v * 1_000_000, v * 1000, v, v // 1000):  # s, ms, us, ns
        if TS_MIN_US <= scaled <= TS_MAX_US:
            return scaled
    return None


def _plaus(us):
    return us if (us is not None and TS_MIN_US <= us <= TS_MAX_US) else None


def exchange_ts_us(msg):
    """Exchange-reported timestamp -> epoch micros, or None (W-TL1).

    `ts_ms` is AUTHORITATIVE (epoch milliseconds, verified against live
    captures): micros = ts_ms * 1000. `ts` is a LEGACY FALLBACK only: a JSON
    number is parsed as epoch SECONDS, a JSON string as ISO-8601; anything
    else is NULL — no unit guessing (D3 boundary validation + plausibility
    window). Kalshi exchange timestamps are millisecond-granular: sub-ms
    conclusions are never supported by this value.
    """
    v = msg.get("ts_ms")
    if type(v) in (int, float):
        us = _plaus(int(v) * 1000)
        if us is not None:
            return us
    v = msg.get("ts")
    if type(v) in (int, float):
        return _plaus(int(v * 1_000_000))
    if isinstance(v, str):
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return _plaus(int(dt.timestamp() * 1_000_000))
        except ValueError:
            return None
    return None


def recv_ladder(rec):
    """Raw-envelope receive clocks -> (recv_wall_ns, recv_mono_ns,
    local_recv_ts_us), each None when absent/corrupt (W-TL1).

    recv_wall_ns / recv_mono_ns are stored as the envelope's ORIGINAL values
    (ints only — boundary validation, D3). local_recv_ts_us = recv_wall_ns //
    1000, derived only when it lands in the plausibility window; an implausible
    wall clock nulls both (corrupt values never enter staging). recv_mono_ns
    has an arbitrary epoch (monotonic), so it is only type/sign-checked —
    differences are meaningful solely within one stream_epoch/connection.
    """
    wall = rec.get("recv_wall_ns")
    wall = wall if type(wall) is int else None
    mono = rec.get("recv_mono_ns")
    mono = mono if (type(mono) is int and mono >= 0) else None
    local_us = _plaus(wall // 1000) if wall is not None else None
    if wall is not None and local_us is None:
        wall = None
    return wall, mono, local_us


class Ingester:
    def __init__(self, con, warehouse_root, active_hours=24, raw_root=None):
        self.con = con
        self.warehouse_root = os.path.abspath(warehouse_root)
        self.raw_root = os.path.abspath(
            raw_root or os.path.join(os.path.dirname(self.warehouse_root), "raw"))
        self.con.execute(STAGING_DDL)
        self._migrate_additive()
        self.classes = self._load_classification(warehouse_root)
        self.active_us = active_hours * HOUR_US
        # per market: book state, dim meta, last tick ts, last snapshot-hour
        self.state, self.meta, self.last_seen, self.hb_hour = {}, {}, {}, {}
        self.global_hour = None
        self.stats = {}          # (day, category) -> [ticks, l1, trades]
        self.bad_ts = 0          # frames dropped for corrupt/implausible timestamps
        self.bad_ticker = 0      # frames dropped for corrupt/spliced tickers
        self._rebuild_state()

    def _migrate_additive(self):
        """Additive-column migrations for pre-existing staging DBs (CREATE
        TABLE IF NOT EXISTS never adds columns): W5 ws_sid/ws_seq on
        orderbooks_full + W-TL1 timestamp-ladder columns on all three fact
        tables. ALTER TABLE ADD COLUMN is nullable, instant, and
        idempotent-guarded — existing rows read back NULL, which is the
        additive contract (history is NOT backfilled here; that is W-TL2)."""
        for table, cols in (("orderbooks_full", ("ws_sid", "ws_seq") + LADDER_COLS),
                            ("orderbooks_l1", LADDER_COLS),
                            ("trades", LADDER_COLS)):
            have = {r[1] for r in self.con.execute(
                "PRAGMA table_info('%s')" % table).fetchall()}
            for col in cols:
                if col not in have:
                    self.con.execute(
                        "ALTER TABLE %s ADD COLUMN %s BIGINT" % (table, col))

    def _load_classification(self, warehouse_root):
        pq = os.path.join(warehouse_root, "catalog", "series_classified", "part-00000.parquet")
        m = {}
        if os.path.exists(pq):
            for st, cat, sub, grp, klass in self.con.execute(
                'SELECT series_ticker, category, subcategory, "group", record_class '
                "FROM read_parquet('%s')" % pq.replace("'", "''")).fetchall():
                m[st] = (cat, sub, grp, klass)
        return m

    def _rebuild_state(self):
        """Restart-safe: last written L1 state + meta + snapshot hour per market."""
        try:
            rows = self.con.execute("""
              SELECT market_ticker, series_ticker, event_ticker, category, subcategory,
                     "group", record_class, yes_bid_e4, yes_bid_qty_e4, yes_ask_e4,
                     yes_ask_qty_e4, ts_utc FROM orderbooks_l1 QUALIFY row_number() OVER
                     (PARTITION BY market_ticker ORDER BY ts_utc DESC)=1""").fetchall()
        except Exception:
            rows = []
        for mt, se, ev, cat, sub, grp, kl, yb, bq, ya, aq, ts in rows:
            self.state[mt] = (yb, bq, ya, aq)
            self.meta[mt] = (se, ev, cat, sub, grp, kl)
            self.last_seen[mt] = ts
            self.hb_hour[mt] = ts // HOUR_US
        mx = self.con.execute("SELECT max(ts_utc) FROM orderbooks_l1").fetchone()[0]
        self.global_hour = (mx // HOUR_US) if mx else None

    def _stat(self, ts_us, category, ticks=0, l1=0, trades=0):
        k = (wc.day_of_us(ts_us), category or "_unclassified")
        s = self.stats.setdefault(k, [0, 0, 0])
        s[0] += ticks; s[1] += l1; s[2] += trades

    def _heartbeats(self, new_hour, l1_rows):
        """Write hour-start heartbeats for every active market lacking one, for
        each hour crossed. Runs before processing the tick that advanced the clock.

        Scheduled heartbeats are SYSTEM-GENERATED state-continuation rows, not
        exchange messages (W-TL1 contract, both halves mandatory):
          - ts_utc = the hour start, ALWAYS (the per-day export, LOCF
            reconstruction and heartbeat detection depend on it; NULL here
            breaks production).
          - exchange_ts_us / recv_wall_ns / recv_mono_ns / local_recv_ts_us =
            ALL NULL (no real message exists; a fabricated timestamp would
            poison the timestamp ladder).
        A heartbeat may seed LOCF state continuation, but must NEVER be
        treated as "the strategy newly observed a market change" — it enters
        no lag/jitter/pacing-residual statistics and is counted separately by
        every consumer (jitter report, backtest clock)."""
        if self.global_hour is None:
            self.global_hour = new_hour
            return
        while self.global_hour < new_hour:
            h = self.global_hour + 1
            h_start = h * HOUR_US
            for mt, st in self.state.items():
                if self.hb_hour.get(mt, -1) >= h:
                    continue
                if h_start - self.last_seen.get(mt, 0) > self.active_us:
                    continue  # market left the active session
                se, ev, cat, sub, grp, kl = self.meta[mt]
                l1_rows.append((h_start, mt, se, ev, cat, sub, grp, kl,
                                st[0], st[1], st[2], st[3], None, None, None, True,
                                None, None, None, None))
                self.hb_hour[mt] = h
                self._stat(h_start, cat, l1=1)
            self.global_hour = h

    def advance_wall_clock(self, now_us=None):
        """Daemon mode: heartbeat even if the feed is silent."""
        now_us = int(time.time() * 1_000_000) if now_us is None else now_us
        rows = []
        self._heartbeats(now_us // HOUR_US, rows)
        rows, _tr, _full = self._divert_late_facts(
            rows, [], [], source_file="<wall-clock-heartbeat>",
            source_start=None, source_end=None)
        self._insert(rows, [], [])

    def _divert_late_facts(self, l1_rows, tr_rows, full_rows,
                           source_file, source_start, source_end):
        """Sealed days are WRITE-ONCE (operator seal ruling 2026-07-11).

        A fact whose exchange day already carries an active seal NEVER enters
        staging (that would eventually demand mutating the sealed archive).
        It is diverted VERBATIM to the corrections partition
        (``warehouse_root/corrections/date=D/late_rows.ndjson``), counted in
        the corrections ledger and alerted.  The seal itself is never touched.

        Sources in non-canonical receipt partitions for a NOT-yet-sealed day
        are still recorded as raw dependencies so the future seal binds them
        (unchanged behaviour).

        Returns the (l1_rows, tr_rows, full_rows) that may enter staging.
        Corrections are fsynced BEFORE the caller's DuckDB commit advances the
        byte checkpoint, so a crash can duplicate a correction but never lose
        one.
        """
        tables = (("orderbooks_l1", l1_rows), ("trades", tr_rows),
                  ("orderbooks_full", full_rows))
        dates = sorted({wc.day_of_us(row[0])
                        for _t, rows in tables
                        for row in rows if row and row[0] is not None})
        source = os.path.abspath(source_file) if not source_file.startswith("<") \
            else source_file
        source_rel = None
        if not source.startswith("<") and \
           os.path.commonpath([self.raw_root, source]) == self.raw_root:
            source_rel = os.path.relpath(source, self.raw_root)

        # Record every non-canonical receipt dependency, even when the first
        # late row already removed the active seal.  Otherwise hour 03/04 files
        # arriving while D is unsealed would be omitted by the next reseal.
        dependency_days = set()
        if source_rel:
            match = re.search(
                r"(?:^|/)date=(\d{4}-\d{2}-\d{2})/[^/]*_(\d{2})\.ndjson(?:\.\d+)?$",
                source_rel)
            for day in dates:
                canonical = False
                if match:
                    receipt_day = datetime.date.fromisoformat(match.group(1))
                    receipt_hour = int(match.group(2))
                    exchange_day = datetime.date.fromisoformat(day)
                    canonical = (receipt_day == exchange_day or
                                 (receipt_day == exchange_day + datetime.timedelta(days=1)
                                  and receipt_hour < 2))
                if not canonical:
                    dependency_days.add(day)

        sealed_days = {day for day in dates
                       if os.path.isfile(wc.seal_path(self.warehouse_root, day))}
        # A sealed day never re-enters the dependency mechanism: its seal is
        # final and its late rows live in the corrections partition instead.
        dependency_days -= sealed_days

        if not sealed_days and not dependency_days:
            return l1_rows, tr_rows, full_rows

        kept, diverted = [], {}
        observed_at = datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for tname, rows in tables:
            keep = []
            for row in rows:
                day = wc.day_of_us(row[0]) if (row and row[0] is not None) else None
                if day in sealed_days:
                    diverted.setdefault(day, []).append({
                        "table": tname, "row": list(row),
                        "observed_at_utc": observed_at,
                        "source_file": source,
                        "source_raw_rel": source_rel,
                        "source_start_offset": source_start,
                        "source_end_offset": source_end,
                    })
                else:
                    keep.append(row)
            kept.append(keep)

        for day, records in sorted(diverted.items()):
            cdir = os.path.join(self.warehouse_root, "corrections", "date=%s" % day)
            os.makedirs(cdir, exist_ok=True)
            cpath = os.path.join(cdir, "late_rows.ndjson")
            with open(cpath, "a", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            _fsync_dir(cdir)

        if diverted:
            cledger = os.path.join(self.warehouse_root, "corrections",
                                   "ledger.ndjson")
            os.makedirs(os.path.dirname(cledger), exist_ok=True)
            with open(cledger, "a", encoding="utf-8") as f:
                for day, records in sorted(diverted.items()):
                    f.write(json.dumps({
                        "event": "LATE_FACT_DIVERTED_TO_CORRECTIONS",
                        "exchange_date": day,
                        "n_rows": len(records),
                        "observed_at_utc": observed_at,
                        "source_file": source,
                        "source_raw_rel": source_rel,
                        "seal_untouched": True,
                    }, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            print("ALERT: %d late fact row(s) for SEALED day(s) %s diverted to "
                  "corrections partition (seal untouched, write-once)"
                  % (sum(len(r) for r in diverted.values()),
                     ",".join(sorted(diverted))), file=sys.stderr)

        if dependency_days:
            ledger = os.path.join(self.warehouse_root, "seal_invalidations.ndjson")
            os.makedirs(os.path.dirname(ledger), exist_ok=True)
            with open(ledger, "a", encoding="utf-8") as f:
                for day in sorted(dependency_days):
                    f.write(json.dumps({
                        "event": "LATE_RAW_DEPENDENCY_OBSERVED",
                        "exchange_date": day,
                        "observed_at_utc": observed_at,
                        "source_file": source,
                        "source_raw_rel": source_rel,
                        "source_start_offset": source_start,
                        "source_end_offset": source_end,
                        "parked_seal": None,
                        "action": "BIND_SOURCE_IN_ANY_FUTURE_DAY_SEAL",
                    }, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
        _fsync_dir(self.warehouse_root)
        return tuple(kept)

    def process_file(self, path):
        path = os.path.abspath(path)
        off = self.con.execute("SELECT byte_offset FROM checkpoint WHERE file=?",
                               [path]).fetchone()
        start = off[0] if off else 0
        size = os.path.getsize(path)
        if size < start:
            print("WARN: %s shrank below checkpoint (%d < %d); skipping" %
                  (path, size, start), file=sys.stderr)
            return 0, 0, 0
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
        # Only process complete lines; a trailing partial line waits for next cycle.
        end = data.rfind(b"\n")
        if end < 0:
            return 0, 0, 0
        chunk = data[:end + 1]
        l1_rows, tr_rows, full_rows = [], [], []
        for line in chunk.split(b"\n"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                frame = json.loads(rec["raw"]) if "raw" in rec else rec
                msg = frame.get("msg", {})
            except (ValueError, KeyError, TypeError):
                continue
            self._frame(rec, frame, msg, l1_rows, tr_rows, full_rows)
        l1_rows, tr_rows, full_rows = self._divert_late_facts(
            l1_rows, tr_rows, full_rows, source_file=path,
            source_start=start, source_end=start + len(chunk))
        # Facts/stats and the byte checkpoint are one atomic unit.  Autocommit
        # here used to leave a crash window: facts committed, checkpoint absent,
        # then restart replayed the chunk and duplicated every fact row.
        self.con.execute("BEGIN TRANSACTION")
        try:
            self._insert(l1_rows, tr_rows, full_rows)
            self.con.execute(
                "INSERT INTO checkpoint VALUES (?,?, epoch_us(now())) "
                "ON CONFLICT (file) DO UPDATE SET byte_offset=excluded.byte_offset, "
                "updated_us=excluded.updated_us", [path, start + len(chunk)])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return len(l1_rows), len(tr_rows), len(full_rows)

    def _frame(self, rec, frame, msg, l1_rows, tr_rows, full_rows):
        typ = frame.get("type") or rec.get("channel")
        mt = msg.get("market_ticker") or msg.get("ticker") or rec.get("source_ticker")
        if not mt or typ not in ("ticker", "trade", "orderbook_snapshot", "orderbook_delta"):
            return
        # Real market tickers are {event}-{outcome}, so a dash is structural.
        if not TICKER_RE.match(str(mt)) or "-" not in mt:
            self.bad_ticker += 1
            return  # spliced/corrupt ticker never enters staging
        series, event = split_ticker(mt)
        cat, sub, grp, klass = self.classes.get(series, (None, None, None, "B"))
        # W-TL1 timestamp ladder: exchange + receive clocks carried as separate
        # columns; ts_utc = COALESCE(exchange, local recv) for LEGACY
        # compatibility only (partitioning/coarse queries — never a tradable
        # replay clock; see mm_backtest --clock).
        exch_us = exchange_ts_us(msg)
        wall_ns, mono_ns, local_us = recv_ladder(rec)
        ts_us = exch_us if exch_us is not None else local_us
        if ts_us is None:
            self.bad_ts += 1
            return  # corrupt/implausible timestamps never enter staging
        ladder = (exch_us, wall_ns, mono_ns, local_us)
        self._stat(ts_us, cat, ticks=1)
        self._heartbeats(ts_us // HOUR_US, l1_rows)

        if typ == "ticker":
            if klass != "A":
                return  # Class B: no L1
            st = (e4(msg.get("yes_bid_dollars")), e4(msg.get("yes_bid_size_fp")),
                  e4(msg.get("yes_ask_dollars")), e4(msg.get("yes_ask_size_fp")))
            hour = ts_us // HOUR_US
            prev = self.state.get(mt)
            if prev is None or hour > self.hb_hour.get(mt, -1):
                snap = True
            elif st != prev:
                snap = False
            else:
                self.last_seen[mt] = ts_us
                return  # unchanged within the hour -> skip
            self.state[mt] = st
            self.meta[mt] = (series, event, cat, sub, grp, klass)
            self.last_seen[mt] = ts_us
            if snap:
                self.hb_hour[mt] = hour
            l1_rows.append((ts_us, mt, series, event, cat, sub, grp, klass,
                            st[0], st[1], st[2], st[3],
                            e4(msg.get("price_dollars")), e4(msg.get("volume_fp")),
                            e4(msg.get("open_interest_fp")), snap) + ladder)
            self._stat(ts_us, cat, l1=1)
        elif typ == "trade":
            tr_rows.append((ts_us, mt, series, event, cat, sub, grp,
                            msg.get("trade_id"), e4(msg.get("yes_price_dollars")),
                            e4(msg.get("no_price_dollars")), e4(msg.get("count_fp")),
                            msg.get("taker_side")) + ladder)
            self._stat(ts_us, cat, trades=1)
        elif typ == "orderbook_snapshot":
            full_rows.append((ts_us, mt, series, event, cat, sub, grp, "snapshot",
                              None, None, None,
                              levels_e4(msg, "yes"), levels_e4(msg, "no"),
                              ws_int(frame.get("sid")), ws_int(frame.get("seq"))) + ladder)
        elif typ == "orderbook_delta":
            price = msg.get("price_dollars", msg.get("price"))
            delta = msg.get("delta_fp", msg.get("delta"))
            full_rows.append((ts_us, mt, series, event, cat, sub, grp, "delta",
                              msg.get("side"), e4(price), e4(delta), None, None,
                              ws_int(frame.get("sid")), ws_int(frame.get("seq"))) + ladder)

    def _insert(self, l1_rows, tr_rows, full_rows):
        if l1_rows:
            self.con.executemany(L1_INSERT, l1_rows)
        if tr_rows:
            self.con.executemany(TRADE_INSERT, tr_rows)
        if full_rows:
            self.con.executemany(FULL_INSERT, full_rows)
        if self.stats:
            for (day, cat), (t, l1, tr) in self.stats.items():
                self.con.execute("""
                  INSERT INTO ingest_stats VALUES (?,?,?,?,?)
                  ON CONFLICT (day, category) DO UPDATE SET
                    ticks_seen = ingest_stats.ticks_seen + excluded.ticks_seen,
                    l1_written = ingest_stats.l1_written + excluded.l1_written,
                    trades_written = ingest_stats.trades_written + excluded.trades_written
                """, [day, cat, t, l1, tr])
            self.stats.clear()


def connect_with_retry(duckdb, path, attempts=30, sleep_s=5.0):
    """Writer connect that survives reader-held locks (2026-07-07 incident:
    a long-lived research process holding a read-only attach crashed the
    daemon's bare connect; the watchdog then crash-looped it while staging
    silently fell behind). Retries with patience; raises only after the
    window is truly exhausted."""
    last = None
    for i in range(attempts):
        try:
            return duckdb.connect(path)
        except Exception as e:  # duckdb.IOException has no stable import path
            msg = str(e).lower()
            lock_error = any(token in msg for token in
                             ("conflicting lock", "database is locked",
                              "could not set lock",
                              "different configuration than existing connections"))
            # Permission, corruption and invalid-path failures are not transient
            # reader locks.  Preserve their real exception and fail immediately.
            if not lock_error:
                raise
            last = e
            if i == 0:
                print("[ingest] staging lock (%s); retrying up to %ds"
                      % (e, int(attempts * sleep_s)), file=sys.stderr)
            time.sleep(sleep_s)
    raise last


def raw_files_to_scan(cfg):
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).date()
    files = []
    for d in (today - datetime.timedelta(days=1), today):
        # *.ndjson* also matches WsRecorder rotation shards (base.ndjson.1, .2, ...)
        # — each shard is its own append-only file, so per-file checkpoints hold.
        files.extend(sorted(glob.glob(
            os.path.join(wc.raw_day_dir(cfg["raw_root"], d.isoformat()), "*.ndjson*"))))
    return files


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", default=None, help="warehouse root (default from config)")
    ap.add_argument("--staging", default=None, help="staging duckdb path (default from config)")
    ap.add_argument("--loop", nargs="?", const=-1, type=int, default=None,
                    help="daemon mode: scan raw logs every N seconds (default from config)")
    ap.add_argument("--heartbeat-active-hours", type=int, default=None)
    ap.add_argument("inputs", nargs="*", help="capture NDJSON file(s) (one-shot mode)")
    args = ap.parse_args(argv[1:])
    import duckdb

    cfg = wc.load_config()
    warehouse = args.warehouse or cfg["warehouse_root"]
    staging = args.staging or cfg["staging_db"]
    active_h = args.heartbeat_active_hours or cfg["heartbeat_active_hours"]
    os.makedirs(os.path.dirname(staging), exist_ok=True)
    # The initial open must have the same lock tolerance as subsequent daemon
    # reconnects.  A bare connect here made the watchdog crash-loop whenever a
    # reader retained a staging ATTACH.
    con = connect_with_retry(duckdb, staging)
    ing = Ingester(con, warehouse, active_hours=active_h,
                   raw_root=cfg["raw_root"])
    print("classes loaded: %d series | staging=%s" % (len(ing.classes), staging))

    if args.loop is not None:
        period = cfg["ingest_loop_seconds"] if args.loop < 0 else args.loop
        print("ingest daemon: scanning %s every %ds" % (cfg["raw_root"], period))
        while True:
            # Hold the DuckDB write lock only inside the processing window so
            # readers (load(), dashboard, exports) get the DB between cycles.
            if ing.con is None:
                ing.con = connect_with_retry(duckdb, staging)
            n_l1 = n_tr = 0
            for path in raw_files_to_scan(cfg):
                l1, tr, _ = ing.process_file(path)
                n_l1 += l1; n_tr += tr
            ing.advance_wall_clock()
            ing.con.close()
            ing.con = None
            if n_l1 or n_tr:
                print("[ingest] +L1=%d +trades=%d%s"
                      % (n_l1, n_tr,
                         " (bad_ts dropped=%d)" % ing.bad_ts if ing.bad_ts else ""))
                sys.stdout.flush()
            time.sleep(period)

    if not args.inputs:
        print("nothing to do: pass capture files or --loop", file=sys.stderr)
        return 2
    tot = [0, 0, 0]
    for path in args.inputs:
        if not os.path.exists(path):
            print("skip (missing): %s" % path, file=sys.stderr)
            continue
        l1, tr, fu = ing.process_file(path)
        tot[0] += l1; tot[1] += tr; tot[2] += fu
        print("%-40s +L1=%d +trades=%d +full=%d" % (os.path.basename(path), l1, tr, fu))
    con.close()
    print("ingested: orderbooks_l1 +%d, trades +%d, orderbooks_full +%d" % tuple(tot))
    if ing.bad_ts:
        print("WARN: dropped %d frame(s) with corrupt/implausible timestamps"
              % ing.bad_ts, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
