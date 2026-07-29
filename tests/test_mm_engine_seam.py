"""Trip fix 2026-07-28: the hydrate-to-live seam blinded pricing 300.8s.

Field evidence: restart at 02:07:35Z hydrated up to source tick 02:07:34,
the first live tick landed ~2-3s later, and the 0.999-coverage sigma gate
(only 0.3s of hole allowed per 300s window) returned None until the hole
aged out -- first QUOTE_EVAL exactly 300.8s after START, with zero
receipts explaining why.  These cases pin both halves of the fix: the
bridge splices the recorder's real ticks into the seam, and starvation now
leaves a PRICING_UNREADY reason on the tape.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import E, reset as _base_reset  # noqa: E402

SER = "KXBTC15M"
IDX = E.IDX[SER]
CAP_DIR = Path("/tmp/_mm_test_seam")


def frame_line(source_ms, value):
    raw = json.dumps({"type": "value", "id": IDX, "time": source_ms,
                      "value": str(value)})
    payload = json.dumps({"type": "cfbenchmarks_value",
                          "msg": {"index_id": IDX,
                                  "received_at": source_ms + 400,
                                  "data": raw}})
    return json.dumps({"kind": "FRAME", "payload": payload})


def write_capture(name, ticks):
    CAP_DIR.mkdir(exist_ok=True)
    p = CAP_DIR / name
    p.write_text("".join(frame_line(t, v) + "\n" for t, v in ticks))
    return str(CAP_DIR / "cap_*.ndjson")


def wobble(i):
    """Non-degenerate values so the sigma windows are positive."""
    return 118000.0 + (i % 2) * 2.0


def fresh():
    _base_reset()
    E.S.rti_seam_from_ms.clear()
    E.S.rti_seam_attempts.clear()
    E.S.pricing_unready.clear()
    E.S.last_pricing_unready.clear()
    for f in CAP_DIR.glob("cap_*.ndjson") if CAP_DIR.exists() else []:
        f.unlink()


def hydrate_then_live(n_hydrated, seam_ms, t0_ms=1_785_204_000_000):
    """Hydrate n 1Hz ticks ending at last_ms, then land a live tick
    seam_ms later the way ingest_cf_value's commit would."""
    ticks = [(t0_ms + i * 1000, wobble(i)) for i in range(n_hydrated)]
    pattern = write_capture("cap_a.ndjson", ticks)
    with mock.patch.object(E, "CF_CAPTURE_GLOB", pattern):
        loaded = E.hydrate_rti_from_capture()
    assert loaded == n_hydrated
    last_ms = ticks[-1][0]
    assert E.S.rti_seam_from_ms[SER] == last_ms
    live_ms = last_ms + seam_ms
    E.S.rti[SER].append(wobble(n_hydrated))
    E.S.rti_src_ms[SER].append(live_ms)
    E.S.rti_t[SER] = time.time()
    E.S.cf_stream_ok = True
    return last_ms, live_ms


def test_bridge_splices_recorder_ticks_and_closes_seam():
    fresh()
    last_ms, live_ms = hydrate_then_live(n_hydrated=10, seam_ms=3000)
    # The recorder kept running: it has the two ticks the engine missed.
    pattern = write_capture(
        "cap_b.ndjson",
        [(last_ms + 1000, wobble(11)), (last_ms + 2000, wobble(12))])
    with mock.patch.object(E, "CF_CAPTURE_GLOB", pattern):
        E.bridge_rti_seam(SER)
    times = list(E.S.rti_src_ms[SER])
    assert E.S.rti_seam_from_ms[SER] is None, "seam must be closed"
    assert last_ms + 1000 in times and last_ms + 2000 in times
    assert times == sorted(times)
    deltas = [b - a for a, b in zip(times, times[1:])]
    assert max(deltas) <= 1100, "no residual hole"


def test_tight_seam_needs_no_splice():
    fresh()
    hydrate_then_live(n_hydrated=5, seam_ms=1000)
    with mock.patch.object(E, "CF_CAPTURE_GLOB",
                           str(CAP_DIR / "nothing_*.ndjson")):
        E.bridge_rti_seam(SER)
    assert E.S.rti_seam_from_ms[SER] is None
    assert len(E.S.rti_src_ms[SER]) == 6


def test_conflicting_recorder_tick_fails_closed():
    fresh()
    last_ms, _ = hydrate_then_live(n_hydrated=5, seam_ms=4000)
    # Attempt 1 splices one real tick; the seam stays open (2s hole left).
    pattern = write_capture("cap_b.ndjson", [(last_ms + 1000, wobble(6))])
    with mock.patch.object(E, "CF_CAPTURE_GLOB", pattern):
        E.bridge_rti_seam(SER)
    assert E.S.rti_seam_from_ms[SER] == last_ms, "2s hole remains"
    before = list(E.S.rti_src_ms[SER])
    # Attempt 2: the recorder now DISAGREES about the spliced tick.
    pattern = write_capture(
        "cap_b.ndjson",
        [(last_ms + 1000, 999999.0), (last_ms + 2000, wobble(7))])
    with mock.patch.object(E, "CF_CAPTURE_GLOB", pattern):
        E.bridge_rti_seam(SER)
    assert E.S.rti_seam_from_ms[SER] is None, "give up, sigma gate rules"
    assert list(E.S.rti_src_ms[SER]) == before, "no partial splice"


def test_bridge_retries_until_recorder_flushes_then_gives_up():
    fresh()
    last_ms, _ = hydrate_then_live(n_hydrated=5, seam_ms=3000)
    empty = str(CAP_DIR / "nothing_*.ndjson")
    with mock.patch.object(E, "CF_CAPTURE_GLOB", empty):
        for i in range(E.RTI_SEAM_MAX_ATTEMPTS):
            E.bridge_rti_seam(SER)
            assert E.S.rti_seam_from_ms[SER] == last_ms, "keep retrying"
        E.bridge_rti_seam(SER)
    assert E.S.rti_seam_from_ms[SER] is None, "bounded: stop trying"


def test_mid_session_gap_bridged_from_recorder():
    """2026-07-29T06:30 alert: a live CF hiccup left a hole INSIDE the
    sigma window (seam closed).  bridge_rti_gaps must splice the hole
    from the recorder and end the blindness immediately."""
    fresh()
    E.S.last_gap_bridge = -1e9
    t0 = 1_785_204_000_000
    n = 400
    hole = {t0 + 350 * 1000 + k * 1000 for k in (1, 2, 3)}
    ticks = [(t0 + i * 1000, wobble(i)) for i in range(n)
             if t0 + i * 1000 not in hole]
    pattern = write_capture("cap_a.ndjson", ticks)
    with mock.patch.object(E, "CF_CAPTURE_GLOB", pattern):
        E.hydrate_rti_from_capture()
    E.S.rti_seam_from_ms[SER] = None
    E.S.cf_stream_ok = True
    now_s = (t0 + (n - 1) * 1000) / 1000.0 + 0.2
    E.S.rti_t[SER] = now_s
    assert E.pricing_state(SER, now_s + 600.0, now_s=now_s) is None
    # recorder has the hole ticks
    pattern = write_capture(
        "cap_b.ndjson",
        [(t0 + 350 * 1000 + k * 1000, wobble(350 + k)) for k in (1, 2, 3)])
    with mock.patch.object(E, "CF_CAPTURE_GLOB", pattern):
        E.bridge_rti_gaps(SER)
    assert E.pricing_state(SER, now_s + 600.0, now_s=now_s) is not None


def test_golden_restart_blindness_ends_with_bridge():
    """THE 300.8s case: >300s of clean hydrated history, one 3s hole at
    the splice point.  pricing_state must be None (with the reason on
    record) before the bridge and ready immediately after."""
    fresh()
    last_ms, live_ms = hydrate_then_live(n_hydrated=400, seam_ms=3000)
    now_s = live_ms / 1000.0 + 0.2
    E.S.rti_t[SER] = now_s          # fixture epoch, not the test wall clock
    close_s = now_s + 600.0
    assert E.pricing_state(SER, close_s, now_s=now_s) is None
    assert "sigma_window" in E.S.pricing_unready[SER]
    assert "seam_open=True" in E.S.pricing_unready[SER]
    pattern = write_capture(
        "cap_b.ndjson",
        [(last_ms + 1000, wobble(401)), (last_ms + 2000, wobble(402))])
    with mock.patch.object(E, "CF_CAPTURE_GLOB", pattern):
        E.bridge_rti_seam(SER)
    ps = E.pricing_state(SER, close_s, now_s=now_s)
    assert ps is not None, "bridge must end the blindness immediately"
    assert ps["sigma"] > 0
    assert SER not in E.S.pricing_unready
