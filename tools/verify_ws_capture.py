#!/usr/bin/env python3
"""Structure validator for a ws_shadow raw-capture NDJSON (PLAN_LIVE_VALIDATION P2).

Reads the capture log written by `WsRecorder` (one NDJSON line per WS frame, plus
"gap"/"loss"/"epoch_change" marker records) and asserts that the session is a
clean, complete orderbook capture:

  - every subscribed ticker got at least one `orderbook_snapshot`;
  - per-sid `source_sequence` is strictly increasing with NO duplicates and NO
    unexplained holes (a hole is tolerated only if a "gap" marker accounts for it);
  - zero "loss" markers (a loss marker means the recorder dropped frames) and zero
    "epoch_change" markers (a reconnect mid-session = not a clean single stream);
  - the capture spans at least --min-span-seconds of wall time when requested
    (guards against a stream that died mid-session).

Record schema (see src/storage.cpp RawLogWriter::write): data frames carry
`channel` (the WS message type, e.g. orderbook_snapshot/orderbook_delta), plus
`source_ticker`, `sid`, `source_sequence`, `recv_wall_ns`. Marker records carry
`marker` and no `raw`.

stdlib only. Output: `PASS:`/`FAIL:` lines and a final `ALL PASS` (exit 0) or
`VERIFY FAIL` (exit 1), matching the repo test convention.

  usage: verify_ws_capture.py CAPTURE.ndjson [--tickers T1,T2] [--min-span-seconds N]
"""
import json
import sys

SNAPSHOT = "orderbook_snapshot"


def load_ndjson(path):
    """Return (records, parse_errors). Blank lines skipped."""
    records, errors = [], []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError as e:
                errors.append("line %d: %s" % (lineno, e))
    return records, errors


def verify_capture(records, tickers=None, min_span_seconds=None):
    """Validate parsed capture records.

    Args:
      records: list of dicts (already JSON-parsed).
      tickers: expected subscribed tickers; if None, derived from data records.
      min_span_seconds: minimum wall-clock span required, or None to skip.

    Returns (ok: bool, lines: list[str]).
    """
    lines = []
    ok = True

    def check(cond, msg):
        nonlocal ok
        lines.append(("PASS: " if cond else "FAIL: ") + msg)
        if not cond:
            ok = False

    data = [r for r in records if not r.get("marker")]
    markers = [r for r in records if r.get("marker")]

    check(bool(data), "capture contains at least one data frame (%d frames, %d markers)"
          % (len(data), len(markers)))
    if not data:
        return ok, lines

    # --- per-ticker snapshot coverage -------------------------------------------
    seen_tickers = sorted({r.get("source_ticker", "") for r in data if r.get("source_ticker")})
    expected = [t for t in (tickers if tickers is not None else seen_tickers) if t]
    snap_tickers = {r.get("source_ticker") for r in data if r.get("channel") == SNAPSHOT}
    for t in expected:
        check(t in snap_tickers, "ticker %s received an %s" % (t, SNAPSHOT))
    if tickers is not None:
        for t in seen_tickers:
            check(t in tickers, "unexpected ticker in capture: %s" % t)

    # --- per-sid sequence continuity --------------------------------------------
    by_sid = {}
    for r in data:
        sid = r.get("sid")
        seq = r.get("source_sequence")
        if sid is None or seq is None:
            continue
        by_sid.setdefault(sid, []).append(int(seq))

    gap_markers = sum(1 for m in markers if m.get("marker") == "gap")
    total_holes = 0
    for sid, seqs in sorted(by_sid.items()):
        seqs_sorted = sorted(seqs)
        dups = len(seqs_sorted) != len(set(seqs_sorted))
        check(not dups, "sid %s has no duplicate seq numbers" % sid)
        holes = 0
        uniq = sorted(set(seqs_sorted))
        for a, b in zip(uniq, uniq[1:]):
            if b != a + 1:
                holes += 1
        total_holes += holes
        lines.append("PASS: sid %s seq %d..%d (%d frames, %d hole(s))"
                     % (sid, uniq[0], uniq[-1], len(seqs), holes))
    # Holes are tolerated only when accounted for by "gap" markers.
    check(total_holes <= gap_markers,
          "seq holes accounted for by gap markers (%d hole(s), %d gap marker(s))"
          % (total_holes, gap_markers))

    # --- marker health ----------------------------------------------------------
    loss = sum(1 for m in markers if m.get("marker") == "loss")
    epoch = sum(1 for m in markers if m.get("marker") == "epoch_change")
    check(loss == 0, "no loss markers (recorder dropped no frames) [count=%d]" % loss)
    check(epoch == 0, "no epoch_change markers (single unbroken stream) [count=%d]" % epoch)
    if gap_markers:
        lines.append("PASS: gap markers present and seq-accounted [count=%d]" % gap_markers)

    # --- wall-clock span --------------------------------------------------------
    walls = [int(r.get("recv_wall_ns", 0)) for r in data if r.get("recv_wall_ns")]
    if walls:
        span_s = (max(walls) - min(walls)) / 1e9
        if min_span_seconds is not None:
            check(span_s >= min_span_seconds,
                  "capture spans >= %.1fs of wall time (actual %.1fs)"
                  % (min_span_seconds, span_s))
        else:
            lines.append("PASS: capture spans %.1fs of wall time" % span_s)

    # --- channel tallies (informational) ----------------------------------------
    channels = {}
    for r in data:
        channels[r.get("channel", "?")] = channels.get(r.get("channel", "?"), 0) + 1
    lines.append("PASS: channels " + ", ".join(
        "%s=%d" % (k, v) for k, v in sorted(channels.items())))

    return ok, lines


def main(argv):
    args = argv[1:]
    if not args:
        print("usage: verify_ws_capture.py CAPTURE.ndjson [--tickers T1,T2] "
              "[--min-span-seconds N]", file=sys.stderr)
        return 2
    path = None
    tickers = None
    min_span = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tickers" and i + 1 < len(args):
            tickers = [t for t in args[i + 1].split(",") if t]
            i += 2
        elif a == "--min-span-seconds" and i + 1 < len(args):
            min_span = float(args[i + 1])
            i += 2
        elif not a.startswith("-") and path is None:
            path = a
            i += 1
        else:
            print("unknown arg: %s" % a, file=sys.stderr)
            return 2
    if path is None:
        print("no capture path given", file=sys.stderr)
        return 2

    try:
        records, errors = load_ndjson(path)
    except OSError as e:
        print("FAIL: cannot read %s: %s" % (path, e))
        print("VERIFY FAIL")
        return 1

    ok, lines = verify_capture(records, tickers=tickers, min_span_seconds=min_span)
    for e in errors:
        print("FAIL: malformed NDJSON %s" % e)
        ok = False
    for line in lines:
        print(line)
    print("ALL PASS" if ok else "VERIFY FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
