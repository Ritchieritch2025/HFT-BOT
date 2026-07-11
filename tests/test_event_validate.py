"""W-E3: event pack validator (§6 V-EP checks) incl. the AF-1 obligation.

Builds a real W-E2 pack over a synthetic warehouse, then validates it. The
load-bearing case is `interior_gap`: a capture hole INSIDE the window (rows on
both sides) that V-EP1/3/12 all pass — only V-EP15 catches it and downgrades to
`degraded`. Without V-EP15 the pack would be a silent green lie (AF-1 / D2).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))  # sibling test module reuse
import event_pack as ep  # noqa: E402
import event_validate as ev  # noqa: E402
from test_event_pack import _make_warehouse, _index_row, _ts, NOW  # noqa: E402


def _pack(tmp_path):
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    out = str(tmp_path / "out")
    m = ep.build_pack(_index_row(), wh_root, out, NOW, archive_only=False)
    data_dir = os.path.join(out, "data", "unit=KX-SPORT-EV1")
    return m, data_dir, wh_root


def _append_trade(data_dir, market, ts_us):
    p = os.path.join(data_dir, "trades.csv")
    with open(p, "a") as f:
        # 13 columns per HEADERS['trades']
        f.write("%d,x,KX-SPORT-EV1,KXNBA,%s,Sports,NBA,game,tX,5000,5000,10000,yes\n"
                % (ts_us, market))


def test_clean_pack_passes(tmp_path):
    m, dd, wh_root = _pack(tmp_path)
    res = ev.validate_pack(m, dd, warehouse=wh_root, gaps=[], archive_only=False)
    assert res["verdict"] == "pass"
    assert all(c["status"] in ("pass", "skip") for c in res["checks"].values())


def test_interior_gap_degrades_af1(tmp_path):
    m, dd, wh_root = _pack(tmp_path)
    # a capture gap INSIDE the window (both sides retain rows).
    gaps = [(_ts("2026-07-07 00:00:00"), _ts("2026-07-07 00:15:00"))]
    res = ev.validate_pack(m, dd, warehouse=wh_root, gaps=gaps, archive_only=False)
    assert res["verdict"] == "degraded"
    assert res["checks"]["V-EP15"]["status"] == "degraded"
    # AF-1 crux: EVERY blocking check still passes — only V-EP15 caught the hole,
    # so without it this holed pack would be stamped `pass` (a green lie).
    assert all(res["checks"][c]["status"] == "pass"
               for c in ev._BLOCKING if res["checks"][c]["status"] != "skip")


def test_non_overlapping_gap_stays_pass(tmp_path):
    m, dd, wh_root = _pack(tmp_path)
    gaps = [(_ts("2026-07-05 00:00:00"), _ts("2026-07-05 01:00:00"))]  # outside window
    assert ev.validate_pack(m, dd, warehouse=wh_root, gaps=gaps,
                            archive_only=False)["verdict"] == "pass"


def test_foreign_market_row_fails(tmp_path):
    m, dd, wh_root = _pack(tmp_path)
    _append_trade(dd, "KX-OTHER-1", _ts("2026-07-07 00:40:00"))
    res = ev.validate_pack(m, dd, warehouse=wh_root, gaps=[], archive_only=False)
    assert res["checks"]["V-EP4"]["status"] == "fail"
    assert res["verdict"] == "fail"


def test_leaked_timestamp_fails(tmp_path):
    m, dd, wh_root = _pack(tmp_path)
    _append_trade(dd, "KX-SPORT-A", _ts("2026-07-08 05:00:00"))  # outside win_end
    res = ev.validate_pack(m, dd, warehouse=wh_root, gaps=[], archive_only=False)
    assert res["checks"]["V-EP3"]["status"] == "fail"
    assert res["verdict"] == "fail"


def test_missing_gap_record_is_uncertified_not_pass(tmp_path):
    # Defect-1 fix: no capture-gap record => V-EP15 skip, verdict degraded
    # (NEVER pass — you cannot certify completeness without the gap record).
    m, dd, wh_root = _pack(tmp_path)
    res = ev.validate_pack(m, dd, warehouse=wh_root, gaps=[], gaps_available=False,
                           archive_only=False)
    assert res["checks"]["V-EP15"]["status"] == "skip"
    assert res["verdict"] == "degraded"


def test_deferred_checks_emitted_as_skip(tmp_path):
    # Defect-2 fix: dependency checks are surfaced as skip, never omitted.
    m, dd, wh_root = _pack(tmp_path)
    checks = ev.validate_pack(m, dd, warehouse=wh_root, gaps=[],
                              archive_only=False)["checks"]
    for cid in ("V-EP5", "V-EP8", "V-EP9", "V-EP11", "V-EP13", "V-EP14"):
        assert checks[cid]["status"] == "skip" and checks[cid]["detail"]


def test_null_window_fails_not_crashes(tmp_path):
    # Defect-4: a null-window manifest fails cleanly instead of TypeError.
    m, dd, wh_root = _pack(tmp_path)
    m = dict(m, win_end_us=None)
    assert ev.validate_pack(m, dd, warehouse=wh_root, gaps=[],
                            archive_only=False)["verdict"] == "fail"


def test_empty_markets_fails_not_crashes(tmp_path):
    # Defect-5: empty market set fails cleanly instead of `IN ()` ParserException.
    m, dd, wh_root = _pack(tmp_path)
    m = dict(m, markets=[])
    assert ev.validate_pack(m, dd, warehouse=wh_root, gaps=[],
                            archive_only=False)["verdict"] == "fail"


def test_gaps_from_quality_log_parses_data_loss(tmp_path):
    ql = tmp_path / "ql.ndjson"
    ql.write_text(
        '{"wp":"x","window":"2026-07-07T06:00-09:00Z","finding":"WS capture gap / data_loss","action":"a"}\n'
        '{"wp":"y","window":"2026-07-06","finding":"GREEN daily check","action":"reviewed"}\n')
    gaps, available = ev.gaps_from_quality_log(str(ql))
    assert available is True
    assert gaps == [(_ts("2026-07-07 06:00:00"), _ts("2026-07-07 09:00:00"))]
