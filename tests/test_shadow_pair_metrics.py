from __future__ import annotations

from decimal import Decimal

import pytest

from tools.research.crypto_mm import shadow_pair_metrics as M


def test_latest_start_excludes_warmup_and_computes_break_even(tmp_path):
    log = tmp_path / "shadow.ndjson"
    log.write_text(
        "\n".join(
            (
                '{"ev":"START","wall_ns":10}',
                '{"ev":"PAIR_LOCK","wall_ns":11,"ct":1,'
                '"locked_usd":0.50,"fee_usd":0}',
                '{"ev":"START","wall_ns":20}',
                '{"ev":"PAIR_LOCK","wall_ns":21,"ct":1,'
                '"locked_usd":0.01,"fee_usd":0}',
                '{"ev":"PAIR_LOCK","wall_ns":22,"ct":1,'
                '"locked_usd":-0.09,"fee_usd":0.01}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    out = M.report((log,))

    assert out["start_wall_ns"] == 20
    assert out["pair_lock_count"] == 2
    assert out["natural_maker_pair"]["pnl_usd"] == "0.01"
    assert out["forced_taker_close"]["pnl_usd"] == "-0.09"
    assert out["total_pnl_usd"] == "-0.08"
    assert Decimal(out["observed_natural_completion_rate"]) == Decimal("0.5")
    assert (
        Decimal(out["break_even_completion_rate_from_observed_severity"])
        == Decimal("0.9")
    )


def test_no_start_fails_closed(tmp_path):
    log = tmp_path / "shadow.ndjson"
    log.write_text(
        '{"ev":"PAIR_LOCK","wall_ns":1,"ct":1,'
        '"locked_usd":0.01,"fee_usd":0}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no START"):
        M.report((log,))


def test_malformed_pair_lock_fails_closed():
    with pytest.raises(ValueError, match="quantity/fee"):
        M.summarize(
            (
                {
                    "ev": "PAIR_LOCK",
                    "wall_ns": 10,
                    "ct": 0,
                    "locked_usd": "0.01",
                    "fee_usd": "0",
                },
            ),
            start_wall_ns=10,
        )
