"""Offline tests for the H5 cross-market deviation runner.

Synthetic checkpoint namespaces mirror the Deep03 layout the loader
verifies (manifest -> receipt -> parquet, all content-addressed), plus a
synthetic MARKET_GRAPH parquet.  Fixtures are engineered so the causal
lead/lag is real: followers reprice 50ms after their family leader, while
unrelated families and the time-reversed stream show nothing inside the
100ms primary horizon.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Sequence

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.alpha_sprint.h5_cross_market_deviation import (  # noqa: E402
    ALL_DATES,
    H5StudyError,
    MODEL_SEAL_NAME,
    RECEIPT_NAME,
    REPORT_NAME,
    TRAIN_DATES,
    run_event_study,
)
import tools.research.alpha_sprint.h5_cross_market_deviation as h5  # noqa: E402


US = 1_000_000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


L2_COLUMNS = (
    "date",
    "t_us",
    "market_ticker",
    "ws_sid",
    "ws_seq",
    "classification",
    "snapshot_epoch",
    "book_valid",
    "topology",
    "mid_e4",
)


def _l2_row(
    date: str,
    ticker: str,
    t_us: int,
    seq: int,
    mid: float,
    *,
    classification: str = "DELTA_APPLIED",
    epoch: int = 1,
    book_valid: bool = True,
    topology: Any = "TWO_SIDED",
) -> List[Any]:
    return [date, t_us, ticker, 1, seq, classification, epoch, book_valid,
            topology, mid]


def _family_rows(
    date: str,
    leader: str,
    follower: str,
    *,
    offset_us: int,
    events: int = 25,
    follower_lag_us: int = 50_000,
    follower_reacts: bool = True,
) -> List[List[Any]]:
    rows: List[List[Any]] = []
    seq = {leader: 1, follower: 1}

    def emit(ticker: str, t: int, mid: float, **kw: Any) -> None:
        rows.append(_l2_row(date, ticker, t, seq[ticker], mid, **kw))
        seq[ticker] += 1

    emit(leader, offset_us, 5_000.0, classification="SNAPSHOT_APPLIED")
    emit(follower, offset_us, 5_000.0, classification="SNAPSHOT_APPLIED")
    emit(leader, offset_us + 100_000, 5_000.0)
    emit(follower, offset_us + 110_000, 5_000.0)
    # 4s spacing so no cross-family event ever lands inside another family's
    # 1s secondary horizon (min cross distance is 1.5s by construction).
    for index in range(events):
        t = offset_us + 10 * US + index * 4 * US
        mid = 5_100.0 if index % 2 == 0 else 5_000.0
        emit(leader, t, mid)
        if follower_reacts:
            emit(follower, t + follower_lag_us, mid)
    return rows


def _write_parquet(con: Any, path: Path, columns: Sequence[str],
                   rows: Sequence[Sequence[Any]]) -> int:
    con.execute("DROP TABLE IF EXISTS staging")
    decls = []
    for name in columns:
        if name in {"t_us", "ws_sid", "ws_seq", "snapshot_epoch", "l2_rows"}:
            decls.append(f"{name} BIGINT")
        elif name in {"mid_e4"}:
            decls.append(f"{name} DOUBLE")
        elif name in {"book_valid"}:
            decls.append(f"{name} BOOLEAN")
        else:
            decls.append(f"{name} VARCHAR")
    con.execute(f"CREATE TABLE staging ({', '.join(decls)})")
    if rows:
        placeholders = ", ".join(["?"] * len(columns))
        con.executemany(
            f"INSERT INTO staging VALUES ({placeholders})", list(rows)
        )
    con.execute(
        f"COPY staging TO '{path}' (FORMAT PARQUET)"
    )
    return len(rows)


def _build_namespace(
    root: Path,
    *,
    follower_reacts: bool = True,
    starve_admission: bool = False,
    corrupt_receipt_hash: bool = False,
) -> Dict[str, Path]:
    con = duckdb.connect()
    namespace = root / "checkpoint"
    stage_dir = namespace / "l2_replay"
    (stage_dir / "data").mkdir(parents=True)
    (stage_dir / "receipts").mkdir(parents=True)
    source_binding = "ab" * 32

    partitions = []
    row_total = 0
    events = 3 if starve_admission else 25
    for date in ALL_DATES:
        rows: List[List[Any]] = []
        rows += _family_rows(
            date, "FAMA-LEAD", "FAMA-FOLL", offset_us=1 * US,
            events=events, follower_reacts=follower_reacts,
        )
        rows += _family_rows(
            date, "FAMB-LEAD", "FAMB-FOLL", offset_us=2_500_000,
            events=events, follower_reacts=follower_reacts,
        )
        # Junk that must be ignored by validity gates.
        rows.append(_l2_row(date, "FAMA-LEAD", 900 * US, 999, 9_999.0,
                            book_valid=False))
        rows.append(_l2_row(date, "FAMA-FOLL", 901 * US, 999, 9_999.0,
                            topology=None))
        rows.append(_l2_row(date, "FAMB-LEAD", 902 * US, 999, 9_999.0,
                            classification="SNAPSHOT_APPLIED"))
        key = f"date={date}_bucket=000"
        data_path = stage_dir / "data" / f"{key}.parquet"
        count = _write_parquet(con, data_path, L2_COLUMNS, rows)
        row_total += count
        receipt = {
            "state": "COMPLETE",
            "stage": "l2_replay",
            "partition_key": key,
            "source_binding": source_binding,
            "schema": [{"name": name} for name in L2_COLUMNS],
            "data": {
                "path": f"l2_replay/data/{key}.parquet",
                "size_bytes": data_path.stat().st_size,
                "sha256": _sha256(data_path),
                "row_count": count,
            },
        }
        receipt_path = stage_dir / "receipts" / f"{key}.json"
        receipt_path.write_text(json.dumps(receipt))
        receipt_sha = _sha256(receipt_path)
        if corrupt_receipt_hash and date == ALL_DATES[0]:
            receipt_sha = "0" * 64
        partitions.append({
            "partition_key": key,
            "receipt_path": f"l2_replay/receipts/{key}.json",
            "receipt_sha256": receipt_sha,
            "data_sha256": receipt["data"]["sha256"],
        })
    manifest = {
        "state": "COMPLETE",
        "stage": "l2_replay",
        "source_binding": source_binding,
        "partitions": partitions,
        "partition_count": len(partitions),
        "row_count": row_total,
    }
    (stage_dir / "MANIFEST.json").write_text(json.dumps(manifest))

    graph_rows = []
    for date in ALL_DATES:
        for family, legs in (
            ("EV-FAMA", ("FAMA-LEAD", "FAMA-FOLL")),
            ("EV-FAMB", ("FAMB-LEAD", "FAMB-FOLL")),
        ):
            for leg in legs:
                graph_rows.append(
                    [date, leg, family, "CANONICAL_FAMILY_EVENT", 100]
                )
        # Rows the strict join must ignore.
        graph_rows.append([date, "GHOST-1", "EV-GHOST", "MISSING_MARKET_DIM", 5])
        graph_rows.append([date, "FAMA-LEAD", None, "MISSING_EVENT_DIM", 5])
    graph_path = root / "MARKET_GRAPH.parquet"
    _write_parquet(
        con,
        graph_path,
        ("date", "market_ticker", "event_ticker", "mapping_status", "l2_rows"),
        graph_rows,
    )
    con.close()
    return {"namespace": namespace, "graph": graph_path}


@pytest.fixture()
def pinned_graph(monkeypatch):
    def pin(graph_path: Path) -> None:
        monkeypatch.setattr(
            h5, "EXPECTED_MARKET_GRAPH_SHA256", _sha256(graph_path)
        )
    return pin


def test_full_study_survives_with_real_lead_lag(tmp_path, pinned_graph):
    paths = _build_namespace(tmp_path)
    pinned_graph(paths["graph"])
    out = tmp_path / "out"
    receipt = run_event_study(paths["namespace"], paths["graph"], out)

    assert (out / RECEIPT_NAME).is_file()
    assert (out / REPORT_NAME).is_file()
    assert (out / MODEL_SEAL_NAME).is_file()
    assert receipt["decision"]["survives_defensive_signal"] is True
    assert receipt["unrelated_root_control_authenticated"] is False
    assert receipt["claims"] == {
        "cash_pnl": False,
        "arbitrage": False,
        "payout_exhaustiveness": False,
        "fees_fills_latency": False,
        "defensive_lead_lag_only": True,
    }
    assert sorted(receipt["admitted_families"]) == ["EV-FAMA", "EV-FAMB"]
    validate = receipt["summaries"]["2026-07-17"]["100000"]
    assert validate["mean_real_rate"] == pytest.approx(1.0)
    for control in ("vs_reversed", "vs_proxy"):
        stats = validate[control]
        assert stats["mean_diff"] == pytest.approx(1.0)
        assert stats["excludes_zero"] is True

    seal = json.loads((out / MODEL_SEAL_NAME).read_text())
    assert seal["train_dates"] == list(TRAIN_DATES)
    assert seal["admitted_families"] == ["EV-FAMA", "EV-FAMB"]
    assert seal["constants"]["burst_dedup_us"] == 1_000_000


def test_burst_dedup_and_follower_events_are_not_double_counted(
    tmp_path, pinned_graph
):
    paths = _build_namespace(tmp_path)
    pinned_graph(paths["graph"])
    out = tmp_path / "out"
    receipt = run_event_study(paths["namespace"], paths["graph"], out)
    # The follower's +50ms echo falls inside the 1s family burst window, so
    # accepted leader events per family == the 25 leader-leg moves only.
    for date in ALL_DATES:
        diag = receipt["diagnostics"][date]
        assert diag["accepted_leader_events"] == {
            "EV-FAMA": 25,
            "EV-FAMB": 25,
        }
        assert diag["families_in_scope"] == 2
        assert diag["legs_in_scope"] == 4


def test_no_reaction_fixture_kills_the_signal(tmp_path, pinned_graph):
    paths = _build_namespace(tmp_path, follower_reacts=False)
    pinned_graph(paths["graph"])
    out = tmp_path / "out"
    receipt = run_event_study(paths["namespace"], paths["graph"], out)
    assert receipt["decision"]["survives_defensive_signal"] is False
    validate = receipt["summaries"]["2026-07-17"]["100000"]
    assert validate["mean_real_rate"] == pytest.approx(0.0)


def test_starved_train_admission_refuses_before_validate(
    tmp_path, pinned_graph
):
    paths = _build_namespace(tmp_path, starve_admission=True)
    pinned_graph(paths["graph"])
    out = tmp_path / "out"
    with pytest.raises(H5StudyError, match="TRAIN-only admission"):
        run_event_study(paths["namespace"], paths["graph"], out)
    assert not (out / MODEL_SEAL_NAME).exists()
    assert not (out / RECEIPT_NAME).exists()


def test_market_graph_hash_mismatch_refuses(tmp_path):
    paths = _build_namespace(tmp_path)
    with pytest.raises(H5StudyError, match="sealed binding"):
        run_event_study(paths["namespace"], paths["graph"], tmp_path / "out")


def test_corrupt_receipt_hash_refuses(tmp_path, pinned_graph):
    paths = _build_namespace(tmp_path, corrupt_receipt_hash=True)
    pinned_graph(paths["graph"])
    with pytest.raises(H5StudyError, match="receipt hash mismatch"):
        run_event_study(paths["namespace"], paths["graph"], tmp_path / "out")


def test_prereg_drift_refuses(tmp_path, pinned_graph, monkeypatch):
    paths = _build_namespace(tmp_path)
    pinned_graph(paths["graph"])
    bad = tmp_path / "prereg.md"
    bad.write_text("# not the preregistration\n")
    monkeypatch.setattr(h5, "PREREG_RELATIVE", bad)
    with pytest.raises(H5StudyError, match="preregistration drift"):
        run_event_study(paths["namespace"], paths["graph"], tmp_path / "out")


def test_validate_summary_only_covers_admitted_families(
    tmp_path, pinned_graph
):
    paths = _build_namespace(tmp_path)
    pinned_graph(paths["graph"])
    out = tmp_path / "out"
    receipt = run_event_study(paths["namespace"], paths["graph"], out)
    for date, summary in receipt["summaries"].items():
        for horizon, block in summary.items():
            assert block["families_with_real_trials"] <= 2
    report = (out / REPORT_NAME).read_text()
    assert "DIFFERENT_EVENT_TICKER_PROXY" in report
    assert "unrelated_root_control_authenticated=false" in report
