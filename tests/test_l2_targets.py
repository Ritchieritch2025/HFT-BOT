#!/usr/bin/env python3
"""PIPE-W06 Stage 1: l2_targets selector contract (D4/E1).

All REST is mocked (module-level fetch_json injection / monkeypatch); no
network. The contract under test:
  * per-group quotas + the global N=50 cap + cross-group dedupe;
  * pre-game window preference (<=24h beats bigger OI outside the band);
  * CTRL_BTC15m picks the soonest windows, not OI;
  * boundary validation: inactive / out-of-horizon / malformed tickers never
    reach the list (D3);
  * ATOMIC write, and fail-closed refresh: a failed or empty selection exits
    nonzero and leaves the previous CSV byte-untouched;
  * --check gate: stale/missing/empty/unreadable => nonzero (LAYER 1b must
    not start), fresh+valid => comma-joined tickers on stdout;
  * CSV contract stays readable by depth_probe.load_depth_target (the
    documented Stage-0 per-tier analysis path).
"""
import csv
import datetime
import json
import os
import sys

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import depth_probe  # noqa: E402
import l2_targets as l2t  # noqa: E402

NOW = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)


def _mk(ticker, hours_out, oi="10.00", status="active"):
    occ = (NOW + datetime.timedelta(hours=hours_out)).isoformat() \
        .replace("+00:00", "Z")
    return {"ticker": ticker, "status": status,
            "occurrence_datetime": occ, "open_interest_fp": oi}


def _fetch_for(data):
    """One page per series, no cursor."""
    def fetch(path, params):
        assert path == "/markets"
        return {"markets": data.get(params["series_ticker"], [])}
    return fetch


def test_plan_quotas_sum_to_stage1_cap_with_bounded_controls():
    quotas = {g: q for g, q, _s in l2t.PLAN}
    assert sum(quotas.values()) == l2t.N_CAP_DEFAULT == 50
    controls = sum(q for g, q in quotas.items() if g.startswith("CTRL_"))
    assert controls / 50 <= 0.20  # spec §2: ~15% budget, quota bounded at 20%
    assert set(quotas) == {"Tennis", "Baseball", "Soccer", "Basketball",
                           "CTRL_Esports", "CTRL_Golf", "CTRL_BTC15m"}


def test_pregame_window_preference_beats_open_interest():
    data = {"S": [_mk("S-26JUL13-BIGOI", 30, oi="999999.00"),
                  _mk("S-26JUL12-SOON", 2, oi="10.00")]}
    rows, _rep, errs = l2t.select_targets(
        NOW, plan=[("Tennis", 1, ["S"])], fetch=_fetch_for(data))
    assert errs == 0
    assert [r["market_ticker"] for r in rows] == ["S-26JUL12-SOON"]


def test_open_interest_ranks_within_the_pregame_band():
    data = {"S": [_mk("S-26JUL12-SMALL", 2, oi="10.00"),
                  _mk("S-26JUL12-BIG", 20, oi="500.00")]}
    rows, _rep, _e = l2t.select_targets(
        NOW, plan=[("Tennis", 1, ["S"])], fetch=_fetch_for(data))
    assert [r["market_ticker"] for r in rows] == ["S-26JUL12-BIG"]


def test_btc15m_picks_soonest_windows_not_oi():
    data = {"KXBTC15M": [_mk("KXBTC15M-26JUL12-3", 3.0, oi="900.00"),
                         _mk("KXBTC15M-26JUL12-1", 0.25, oi="0"),
                         _mk("KXBTC15M-26JUL12-2", 0.5, oi="5.00")]}
    rows, _rep, _e = l2t.select_targets(
        NOW, plan=[("CTRL_BTC15m", 2, ["KXBTC15M"])], fetch=_fetch_for(data))
    assert [r["market_ticker"] for r in rows] == \
        ["KXBTC15M-26JUL12-1", "KXBTC15M-26JUL12-2"]


def test_global_cap_dedupe_and_boundary_validation():
    data = {
        "A": [_mk("KXA-26JUL12-X", 2, oi="50.00"),
              _mk("KXA-26JUL12-Y", 3, oi="40.00"),
              _mk("KXA-26JUL12-DEAD", 2, status="settled"),      # inactive
              _mk("KXA-26JUL13-LATE", 48),                        # out of horizon
              _mk("bad ticker!!", 2),                             # malformed (D3)
              _mk("NODASH", 2)],                                  # no structural dash
        "B": [_mk("KXA-26JUL12-X", 2, oi="70.00"),                # cross-group dup
              _mk("KXB-26JUL12-Z", 4, oi="30.00"),
              _mk("KXB-26JUL12-W", 5, oi="20.00")],
    }
    plan = [("Tennis", 2, ["A"]), ("Baseball", 3, ["B"])]
    rows, _rep, _e = l2t.select_targets(NOW, plan=plan, cap=3,
                                        fetch=_fetch_for(data))
    picked = [r["market_ticker"] for r in rows]
    # quota 2 from A; the duplicate is skipped in B, cap=3 stops after one more
    assert picked == ["KXA-26JUL12-X", "KXA-26JUL12-Y", "KXB-26JUL12-Z"]
    tiers = {r["market_ticker"]: r["liquidity_tier"] for r in rows}
    assert tiers["KXA-26JUL12-X"] == "Tennis"  # first (quota'd) group wins the dup


def test_atomic_write_check_roundtrip_and_depth_probe_contract(tmp_path):
    out = str(tmp_path / "l2_targets.csv")
    rows = [{"market_ticker": "KXA-26JUL12-X", "liquidity_tier": "Tennis",
             "open_interest": 50.0, "occurrence": "2026-07-12T14:00:00+00:00",
             "series": "A"},
            {"market_ticker": "KXB-26JUL12-Z", "liquidity_tier": "CTRL_Golf",
             "open_interest": 1.0, "occurrence": "2026-07-12T15:00:00+00:00",
             "series": "B"}]
    l2t.write_atomic(rows, out, NOW)
    assert os.path.exists(out)
    assert not [p for p in os.listdir(str(tmp_path)) if ".tmp" in p]
    with open(out, newline="") as f:
        r = csv.DictReader(f)
        assert r.fieldnames == l2t.FIELDNAMES
        got = list(r)
    assert [g["market_ticker"] for g in got] == \
        ["KXA-26JUL12-X", "KXB-26JUL12-Z"]
    assert all(g["generated_at"] == "2026-07-12T12:00:00Z" for g in got)
    # the supervisor gate returns exactly the written tickers
    assert l2t.check(out, max_age_secs=7200) == \
        ["KXA-26JUL12-X", "KXB-26JUL12-Z"]
    # Stage-0 documented CSV contract: depth_probe reads tickers + tiers
    tickers, tiers = depth_probe.load_depth_target(out)
    assert tickers == ["KXA-26JUL12-X", "KXB-26JUL12-Z"]
    assert tiers == {"KXA-26JUL12-X": "Tennis", "KXB-26JUL12-Z": "CTRL_Golf"}


def test_check_fail_closed_missing_stale_empty_malformed(tmp_path):
    out = str(tmp_path / "l2_targets.csv")
    with pytest.raises(RuntimeError, match="missing"):
        l2t.check(out, max_age_secs=7200)
    # header-only file: no tickers
    with open(out, "w") as f:
        f.write(",".join(l2t.FIELDNAMES) + "\n")
    with pytest.raises(RuntimeError, match="no well-formed tickers"):
        l2t.check(out, max_age_secs=7200)
    # malformed rows are skipped; a surviving good row still passes
    with open(out, "a") as f:
        f.write("bad ticker!!,Tennis,1,t,s,g\n")
        f.write("KXA-26JUL12-X,Tennis,1,t,s,g\n")
    assert l2t.check(out, max_age_secs=7200) == ["KXA-26JUL12-X"]
    # stale file refused even though its content is valid
    old = os.path.getmtime(out) - 7201
    os.utime(out, (old, old))
    with pytest.raises(RuntimeError, match="stale"):
        l2t.check(out, max_age_secs=7200)


def test_main_check_mode_exit_codes(tmp_path, capsys):
    out = str(tmp_path / "l2_targets.csv")
    assert l2t.main(["--check", "--out", out]) == 3
    assert "CHECK FAIL" in capsys.readouterr().err
    l2t.write_atomic([{"market_ticker": "KXA-26JUL12-X",
                       "liquidity_tier": "Tennis", "open_interest": 1.0,
                       "occurrence": "t", "series": "A"}], out, NOW)
    assert l2t.main(["--check", "--out", out]) == 0
    assert capsys.readouterr().out.strip() == "KXA-26JUL12-X"


def test_main_refresh_fail_closed_leaves_previous_file_untouched(
        tmp_path, monkeypatch):
    out = str(tmp_path / "l2_targets.csv")
    good = {"KXMLBGAME": [_mk("KXMLB-26JUL12-BOS", 2, oi="42.00")]}

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    # main() intentionally uses wall-clock UTC.  Freeze it here so this
    # fail-closed regression test does not expire as its fixture ages.
    monkeypatch.setattr(l2t, "datetime", FrozenDatetime)

    def fetch_ok(path, params):
        return {"markets": good.get(params["series_ticker"], [])}

    monkeypatch.setattr(l2t, "fetch_json", fetch_ok)
    assert l2t.main(["--out", out]) == 0
    before = open(out, "rb").read()
    assert b"KXMLB-26JUL12-BOS" in before

    def fetch_boom(path, params):
        raise RuntimeError("exchange down")

    # total fetch failure => nonzero AND the previous good list is untouched
    monkeypatch.setattr(l2t, "fetch_json", fetch_boom)
    assert l2t.main(["--out", out]) == 1
    assert open(out, "rb").read() == before
    assert not [p for p in os.listdir(str(tmp_path)) if ".tmp" in p]


def test_main_zero_candidates_never_writes(tmp_path, monkeypatch):
    out = str(tmp_path / "l2_targets.csv")
    monkeypatch.setattr(l2t, "fetch_json", lambda p, q: {"markets": []})
    assert l2t.main(["--out", out]) == 1
    assert not os.path.exists(out)


def test_pagination_follows_cursor_and_stops(monkeypatch):
    calls = []

    def fetch(path, params):
        calls.append(dict(params))
        if "cursor" not in params:
            return {"markets": [_mk("KXA-26JUL12-P1", 2)], "cursor": "c2"}
        return {"markets": [_mk("KXA-26JUL12-P2", 3)]}

    cands, errs = l2t.collect_group(["A"], NOW, 36, fetch=fetch)
    assert errs == 0
    assert {c[2] for c in cands} == {"KXA-26JUL12-P1", "KXA-26JUL12-P2"}
    assert len(calls) == 2 and calls[1]["cursor"] == "c2"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
