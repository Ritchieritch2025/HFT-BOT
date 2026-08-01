"""Toxicity-table guards.

The table learns, per (side, price zone), what recent fills of that kind
actually cost us and raises only that side's entry threshold.  The
properties that matter are: it is causal, it never invents a number from a
cold start, a bucket that stops hurting relaxes on its own, and it can only
ever make us MORE demanding (never cheaper).
"""
from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import E, reset as _base_reset  # noqa: E402


def fresh():
    _base_reset()
    E.S.tox = {}
    E.S.tox_pending = []
    E.S.last_fair_c = {}


def test_cold_table_adds_nothing():
    """With no observations the surcharge is exactly zero — a fresh engine
    must quote like the table does not exist."""
    fresh()
    with mock.patch.object(E, "TOX_ENABLE", True):
        assert E.tox_addon_c("bid", 40.0) == 0.0
        assert E.tox_addon_c("ask_no", 60.0) == 0.0


def test_thin_bucket_is_shrunk_not_trusted():
    """A 1-fill bucket must not price itself: shrinkage weight n/(n+prior)
    makes its surcharge a small fraction of what the same mean earns once
    the sample is large.  (The old contract was a hard n<MIN_N cliff; the
    continuous form is strictly better behaved at the boundary.)"""
    fresh()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 25.0),
          mock.patch.object(E, "TOX_HALFLIFE_FILLS", 30.0)):
        E.tox_observe("bid|35-65", -4.0)
        thin = E.tox_addon_c("bid", 40.0)
        fresh()
        for _ in range(200):
            E.tox_observe("bid|35-65", -4.0)
        thick = E.tox_addon_c("bid", 40.0)
        assert thin < thick / 3.0, (thin, thick)
        assert thin >= 0.0


def test_noisy_evidence_earns_less_than_tight_evidence():
    """Same mean loss, different scatter: the noisy bucket must charge less,
    because certainty = |mean|/(|mean|+stderr)."""
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 5.0),
          mock.patch.object(E, "TOX_HALFLIFE_FILLS", 40.0)):
        fresh()
        for _ in range(80):
            E.tox_observe("bid|35-65", -3.0)          # zero scatter
        tight = E.tox_addon_c("bid", 40.0)
        fresh()
        for i in range(80):
            E.tox_observe("bid|35-65", -3.0 + (25.0 if i % 2 else -25.0))
        noisy = E.tox_addon_c("bid", 40.0)
        assert noisy < tight, (noisy, tight)


def test_redundancy_is_not_billed_twice():
    """What sigma/anchor/flow already charge for is subtracted: handing in
    the redundancy must lower the surcharge one-for-one until it floors."""
    fresh()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 5.0),
          mock.patch.object(E, "TOX_HALFLIFE_FILLS", 40.0)):
        for _ in range(80):
            E.tox_observe("bid|35-65", -3.0)
        alone = E.tox_addon_c("bid", 40.0)
        partly = E.tox_addon_c("bid", 40.0, redundancy=alone / 2.0)
        fully = E.tox_addon_c("bid", 40.0, redundancy=alone * 2.0)
        assert partly < alone
        assert fully == 0.0


def test_surcharge_cannot_dwarf_contract_volatility():
    """Relative cap: the charge may not exceed a fraction of sigma_contract,
    so a quiet contract never inherits a huge surcharge."""
    fresh()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 5.0),
          mock.patch.object(E, "TOX_MAX_SIGMA_FRAC", 0.5),
          mock.patch.object(E, "TOX_HALFLIFE_FILLS", 40.0)):
        for _ in range(80):
            E.tox_observe("bid|35-65", -8.0)
        assert E.tox_addon_c("bid", 40.0, sigma_c=1.0) <= 0.5 + 1e-9


def test_losing_bucket_raises_only_its_own_side():
    """A bucket that has been losing money raises that side's threshold and
    leaves the other side untouched."""
    fresh()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 5.0),
          mock.patch.object(E, "TOX_HALFLIFE_FILLS", 40.0)):
        for _ in range(80):
            E.tox_observe("bid|35-65", -3.0)
        addon_bid = E.tox_addon_c("bid", 40.0)
        assert addon_bid > 1.0, addon_bid
        # the NO side has no observations of its own; it may inherit the
        # pooled view but must never exceed the cap
        assert E.tox_addon_c("ask_no", 60.0) <= E.TOX_MAX_ADDON_C


def test_profitable_bucket_adds_nothing():
    """Positive markout is not a discount: the surcharge floors at zero."""
    fresh()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 5.0)):
        for _ in range(40):
            E.tox_observe("bid|0-10", +2.5)
        assert E.tox_addon_c("bid", 5.0) == 0.0


def test_surcharge_decays_when_a_bucket_stops_hurting():
    """Self-healing: after the losses stop, the EWMA walks back to zero and
    the surcharge disappears without anyone editing a parameter."""
    fresh()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 5.0),
          mock.patch.object(E, "TOX_HALFLIFE_FILLS", 20.0)):
        for _ in range(80):
            E.tox_observe("bid|35-65", -4.0)
        hurt = E.tox_addon_c("bid", 40.0)
        for _ in range(60):
            E.tox_observe("bid|35-65", 0.0)
        healed = E.tox_addon_c("bid", 40.0)
        assert healed < hurt / 2.0, (hurt, healed)


def test_surcharge_is_capped():
    fresh()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_N", 5.0),
          mock.patch.object(E, "TOX_MAX_ADDON_C", 3.0)):
        for _ in range(100):
            E.tox_observe("bid|35-65", -50.0)
        assert E.tox_addon_c("bid", 40.0) == 3.0


def test_pending_fill_is_scored_only_after_its_horizon():
    """Causality: a parked fill contributes nothing before the horizon and
    is scored against the CURRENT fair once it elapses."""
    fresh()
    now = E.time.time()
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_HORIZON_S", 30.0)):
        E.S.tox_pending.append({"t": now, "mt": "MKT", "side": "bid",
                                "entry_c": 40.0,
                                "bucket": "bid|35-65"})
        E.S.last_fair_c["MKT"] = 38.0          # fair fell: this fill was toxic
        E.tox_settle_pending(now + 10)         # horizon not reached
        assert E.S.tox == {}
        assert len(E.S.tox_pending) == 1
        E.tox_settle_pending(now + 31)         # horizon passed
        assert "bid|35-65" in E.S.tox
        assert E.S.tox["bid|35-65"]["ewma"] < 0      # -2c markout
        assert E.S.tox_pending == []


def test_zone_keys_are_stable_across_markets():
    """Bucket keys must be fixed decades x fixed sigma_c bands, or the
    table stops being comparable between runs."""
    assert E.tox_bucket("bid", 5.0, 0.3) == "bid|0-10|g1(<0.5c)"
    assert E.tox_bucket("bid", 40.0, 2.0) == "bid|35-65|g3(1.5-3)"
    assert E.tox_bucket("ask_no", 95.0, 8.0) == "ask_no|90-100|g5(>6c)"
    assert E.tox_bucket("bid", 40.0) == "bid|35-65|gna"
    assert E.tox_bucket("bid", None) is None


def test_prior_mirror_mapping_and_yield_to_live_data(tmp_path):
    """The tape prior keys by the TAKER; the engine keys by OUR entry.
    A study row (no, 90-100, <2m) must land on engine bucket
    (bid, 0-10, <2m): when we bid 5c for YES, the taker who hits us is
    buying NO at 95c.  The prior's effective n is capped so a few hundred
    live fills can overrule history."""
    fresh()
    prior = {
        "no|90-100|g5(>6c)": {"n": 8418, "maker_markout_c": 17.36,
                              "se": 0.26},
        "yes|10-20|g5(>6c)": {"n": 7262, "maker_markout_c": -17.74,
                              "se": 0.39},
    }
    f = tmp_path / "prior.json"
    f.write_text(__import__("json").dumps(prior))
    with (mock.patch.object(E, "TOX_ENABLE", True),
          mock.patch.object(E, "TOX_PRIOR_EFF_N", 100.0)):
        loaded = E.tox_load_prior(str(f))
        assert loaded == 2
        assert E.S.tox["bid|0-10|g5(>6c)"]["ewma"] == 17.36
        assert E.S.tox["ask_no|80-90|g5(>6c)"]["ewma"] == -17.74
        assert E.S.tox["bid|0-10|g5(>6c)"]["n"] == 100.0
        # sigma_c routes the lookup AND caps the charge; use a high band
        # value so the relative cap does not bind the assertion.
        assert E.tox_addon_c("bid", 5.0, sigma_c=8.0) == 0.0
        assert E.tox_addon_c("ask_no", 85.0, sigma_c=8.0) > 3.0


def test_prior_missing_file_is_harmless():
    fresh()
    with mock.patch.object(E, "TOX_ENABLE", True):
        assert E.tox_load_prior("/nonexistent/prior.json") == 0
        assert E.S.tox == {}


def test_disabled_table_is_inert():
    fresh()
    with mock.patch.object(E, "TOX_ENABLE", False):
        E.tox_observe("bid|35-65", -9.0)
        assert E.S.tox == {}
        assert E.tox_addon_c("bid", 40.0) == 0.0
