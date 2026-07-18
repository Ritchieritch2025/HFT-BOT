#!/usr/bin/env python3
"""Bounded SPORTS-AUTORESEARCH Cycle-0 over verified W05 release caches.

This is deliberately a thin execution adapter, not a second research
framework.  It proves that a W09 worker can consume immutable W05 releases,
build an event-first coverage/market atlas, and register the first five
strategy-shaped exploratory studies.  It never connects to production, writes
S3, or produces a promotion/verdict claim.

The input is one or more directories produced by ``research_data fetch`` and
``research_data verify``.  Every input must contain MANIFEST.json and
.VERIFIED.json.  Outputs live under work/research/auto_research/<RUN_ID>/.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MISSION = (
    ROOT / "docs" / "plan_audits" /
    "SPORTS_AUTORESEARCH_01_MISSION_TEXT_V2_2026-07-15.md"
)
ALLOWED_TIERS = {
    "SEALED_CONFIRMATION",
    "SEALED_PENDING_QUALITY_ASSESSMENT",
    "SEALED_DEGRADED_EVIDENCE",
}
BANNER = {
    "data_evidence": "PER_RELEASE_MANIFEST",
    "timestamp_discipline": "PER_RELEASE_MANIFEST",
    "experiment_split": "EXPLORATORY_ONLY",
    "artifact_status": "DIAGNOSTIC_ONLY",
    "authorization": "NOT A LIVE-TRADING AUTHORIZATION",
}


class CycleError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        capture_output=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def _git_dirty_digest() -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=ROOT, text=True,
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return "UNAVAILABLE"
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def _json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise CycleError("expected JSON object: %s" % path)
    return value


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _list_sql(paths: Iterable[Path]) -> str:
    return "[" + ",".join(_quote(str(p)) for p in paths) + "]"


def _release(path_text: str, allow_degraded: bool = False) -> dict:
    path = Path(path_text).resolve()
    manifest_path = path / "MANIFEST.json"
    verified_path = path / ".VERIFIED.json"
    if not manifest_path.is_file() or not verified_path.is_file():
        raise CycleError("release is not locally verified: %s" % path)
    manifest = _json(manifest_path)
    verified = _json(verified_path)
    if manifest.get("schema_version") != "research-release-manifest-v2":
        raise CycleError("unsupported release manifest schema: %s" % path)
    rid = manifest.get("release_id")
    if not isinstance(rid, str) or not rid:
        raise CycleError("manifest release_id missing: %s" % path)
    if path.name != rid:
        raise CycleError("release directory/id mismatch: %s != %s" % (path.name, rid))
    if verified.get("release_id") != rid:
        raise CycleError("verified marker release_id mismatch: %s" % rid)
    binding = (
        verified.get("version_binding_mode") or
        (manifest.get("version_binding") or {}).get("mode")
    )
    if binding != "VERSION_BOUND":
        raise CycleError("release is not VERSION_BOUND: %s (%s)" % (rid, binding))
    tier = verified.get("evidence_tier") or manifest.get("evidence_tier")
    if tier not in ALLOWED_TIERS:
        raise CycleError("release evidence tier is not pilot-admissible: %s" % tier)
    if tier == "SEALED_DEGRADED_EVIDENCE" and not allow_degraded:
        raise CycleError(
            "degraded release requires explicit --allow-degraded: %s" % rid
        )
    if verified.get("evidence_tier") != manifest.get("evidence_tier"):
        raise CycleError("verified/manifest evidence tier mismatch: %s" % rid)
    for field in ("date", "publication_state_sha256"):
        if verified.get(field) != manifest.get(field):
            raise CycleError("verified/manifest %s mismatch: %s" % (field, rid))
    seal_hash = (manifest.get("seal") or {}).get("sha256")
    if not seal_hash or verified.get("seal_sha256") != seal_hash:
        raise CycleError("verified/manifest seal hash mismatch: %s" % rid)

    object_by_key = {}
    for item in manifest.get("objects") or []:
        if not isinstance(item, dict):
            raise CycleError("invalid manifest object entry: %s" % rid)
        key = item.get("key")
        if (not isinstance(key, str) or not key or key.startswith("/") or
                "\\" in key or ".." in Path(key).parts):
            raise CycleError("unsafe manifest object key in %s: %r" % (rid, key))
        if key in object_by_key:
            raise CycleError("duplicate manifest object key in %s: %s" % (rid, key))
        if not isinstance(item.get("version_id"), str) or not item["version_id"]:
            raise CycleError("consumable release object lacks VersionId: %s/%s" %
                             (rid, key))
        object_by_key[key] = item

    def bound_objects(
        prefix: str, suffixes: tuple[str, ...] | None = None
    ) -> list[Path]:
        selected = []
        for key, item in object_by_key.items():
            if not key.startswith(prefix):
                continue
            if suffixes is not None and not key.endswith(suffixes):
                continue
            local = (path / key).resolve()
            try:
                local.relative_to(path)
            except ValueError as exc:
                raise CycleError("manifest path escapes release: %s" % key) from exc
            if not local.is_file():
                raise CycleError("verified release object missing locally: %s/%s" %
                                 (rid, key))
            if local.stat().st_size != int(item.get("size", -1)):
                raise CycleError("release object size changed after verify: %s/%s" %
                                 (rid, key))
            if _sha256(local) != item.get("sha256"):
                raise CycleError("release object hash changed after verify: %s/%s" %
                                 (rid, key))
            selected.append(local)
        return sorted(selected)

    facts = {}
    for table in ("orderbooks_l1", "trades", "orderbooks_full"):
        facts[table] = bound_objects(
            "facts/%s/" % table, (".parquet", ".csv", ".csv.gz")
        )
    rfq = bound_objects("raw_rfq/")
    consumed = []
    for local in [p for values in facts.values() for p in values] + rfq:
        key = local.relative_to(path).as_posix()
        item = object_by_key[key]
        consumed.append({
            "key": key, "size": item["size"], "sha256": item["sha256"],
            "version_id": item["version_id"],
        })
    return {
        "path": path,
        "release_id": rid,
        "date": manifest.get("date"),
        "tier": tier,
        "tl1_status": verified.get("tl1_status") or manifest.get("tl1_status"),
        "manifest_sha256": _sha256(manifest_path),
        "manifest": manifest,
        "verified": verified,
        "facts": facts,
        "rfq": rfq,
        "consumed_objects": consumed,
    }


def _relation_sql(table: str, paths: list[Path]) -> str:
    if not paths:
        return ""
    parquet = [p for p in paths if p.suffix == ".parquet"]
    csvs = [p for p in paths if p.name.endswith(".csv.gz") or p.suffix == ".csv"]
    parts = []
    if parquet:
        parts.append("SELECT * FROM read_parquet(%s, union_by_name=true)" %
                     _list_sql(parquet))
    if csvs:
        # Identity and enum fields must never be inferred as BOOL/INTEGER.
        available_columns: set[str] = set()
        for path in csvs:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(
                path, "rt", encoding="utf-8", errors="replace", newline=""
            ) as handle:
                available_columns.update(next(csv.reader(handle), []))
        str_types = {
            "market_ticker": "VARCHAR", "series_ticker": "VARCHAR",
            "event_ticker": "VARCHAR", "category": "VARCHAR",
            "subcategory": "VARCHAR", "group": "VARCHAR",
            "trade_id": "VARCHAR", "taker_side": "VARCHAR",
        }
        str_types = {
            name: dtype for name, dtype in str_types.items()
            if name in available_columns
        }
        types = "{" + ",".join(
            "%s:%s" % (_quote(k), _quote(v)) for k, v in str_types.items()
        ) + "}"
        parts.append(
            "SELECT * FROM read_csv(%s, header=true, union_by_name=true, "
            "types=%s)" % (_list_sql(csvs), types)
        )
    return " UNION ALL BY NAME ".join(parts)


def _columns(con, view: str) -> set[str]:
    return {row[0] for row in con.execute("DESCRIBE %s" % view).fetchall()}


def _summary(values: list[float | int]) -> dict:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return {"n": 0, "p50": None, "p99": None, "max": None}
    def rank(pct: int):
        index = max(0, min(len(xs) - 1, (pct * len(xs) + 99) // 100 - 1))
        return xs[index]
    return {"n": len(xs), "p50": rank(50), "p99": rank(99), "max": xs[-1]}


def _table_coverage(con, view: str, cols: set[str]) -> dict:
    select = ["count(*) AS rows"]
    if "market_ticker" in cols:
        select.append("approx_count_distinct(market_ticker) AS markets")
    if "event_ticker" in cols:
        select.append("approx_count_distinct(event_ticker) AS root_events")
    if "ts_utc" in cols:
        select.extend(["min(ts_utc) AS first_ts", "max(ts_utc) AS last_ts"])
    row = con.execute("SELECT %s FROM %s" % (", ".join(select), view)).fetchone()
    out = dict(zip([part.split(" AS ")[-1] for part in select], row))
    dims = [c for c in ("category", "subcategory") if c in cols]
    if "ts_utc" in cols:
        dims += ["CAST(to_timestamp(ts_utc/1000000.0) AS DATE) AS utc_date",
                 "EXTRACT(hour FROM to_timestamp(ts_utc/1000000.0))::INTEGER AS utc_hour"]
    if dims:
        group = ", ".join(dims)
        cube_rows = con.execute(
            "SELECT %s, count(*) AS rows%s FROM %s GROUP BY ALL ORDER BY ALL" % (
                group,
                ", approx_count_distinct(market_ticker) AS markets"
                if "market_ticker" in cols else "",
                view,
            )
        ).fetchall()
        names = [d.split(" AS ")[-1] for d in dims] + ["rows"]
        if "market_ticker" in cols:
            names.append("markets")
        out["cube"] = [dict(zip(names, r)) for r in cube_rows]
    else:
        out["cube"] = []
    return out


def _overround(con, l1_cols: set[str]) -> dict:
    return {
        "status": "DATA_STARVED",
        "reason": (
            "no manifest-bound market-family map proving three mutually "
            "exclusive/exhaustive outcome roles; arbitrary 3-market groups "
            "are forbidden"
        ),
        "required_next": (
            "build a catalog-derived match-winner family map, then strict-as-of "
            "synchronize all three executable asks with a frozen quote-age cap"
        ),
    }


def _large_flow(con, trade_cols: set[str]) -> dict:
    needed = {"count_e4", "market_ticker"}
    if not needed.issubset(trade_cols):
        return {"status": "DATA_STARVED", "missing_columns": sorted(needed-trade_cols)}
    row = con.execute("""
      WITH valid AS (SELECT count_e4 FROM ar_trades WHERE count_e4 > 0)
      SELECT count(*), quantile_cont(count_e4/10000.0, .5),
             quantile_cont(count_e4/10000.0, .99), max(count_e4/10000.0)
      FROM valid
    """).fetchone()
    threshold = row[2]
    by_side = []
    if "taker_side" in trade_cols and threshold is not None:
        by_side = [
            {"taker_side": r[0], "n": r[1]}
            for r in con.execute(
                "SELECT coalesce(taker_side,'_missing'), count(*) FROM ar_trades "
                "WHERE count_e4/10000.0 >= ? GROUP BY 1 ORDER BY 2 DESC",
                [threshold],
            ).fetchall()
        ]
    return {
        "status": "DATA_STARVED",
        "reason": "continuation/reversal causal as-of outcomes were not computed in the path smoke",
        "diagnostic_input_profile": {
            "trade_contracts": {"n": row[0], "p50": row[1],
                                "p99": row[2], "max": row[3]},
            "p99_events_by_taker_side": by_side,
        },
        "large_flow_threshold": "full-sample descriptive p99; LOOK-AHEAD, not a strategy threshold",
        "continuation_test": "DEFERRED_TO_NEXT_CYCLE_CAUSAL_ASOF_JOIN",
        "actor_identity": "UNAVAILABLE — large flow is not a whale identity",
    }


def _l2(con, cols: set[str]) -> dict:
    if not cols:
        return {"status": "DATA_STARVED", "reason": "orderbooks_full absent"}
    select = ["count(*)", "approx_count_distinct(market_ticker)"
              if "market_ticker" in cols else "NULL"]
    row = con.execute("SELECT %s FROM ar_l2" % ",".join(select)).fetchone()
    out = {"status": "DATA_STARVED",
           "reason": "full L2 book replay and depletion/refill episode construction were not run",
           "rows": row[0], "markets": row[1], "columns": sorted(cols)}
    if "delta_e4" in cols:
        d = con.execute("""
          SELECT count(*) FILTER (WHERE delta_e4<0),
                 count(*) FILTER (WHERE delta_e4>0),
                 quantile_cont(abs(delta_e4)/10000.0,.5),
                 quantile_cont(abs(delta_e4)/10000.0,.99),
                 max(abs(delta_e4)/10000.0)
          FROM ar_l2 WHERE delta_e4 IS NOT NULL
        """).fetchone()
        out["delta_contracts"] = {
            "negative_n": d[0], "positive_n": d[1], "p50": d[2],
            "p99": d[3], "max": d[4],
        }
        out["depletion_refill"] = "DELTA_PROXY_ONLY — full book-state replay deferred"
    else:
        out["depletion_refill"] = "DATA_STARVED — delta_e4 absent"
    return out


def _fixed(value, places: int):
    if value in (None, ""):
        return None
    try:
        scaled = Decimal(str(value)) * (Decimal(10) ** places)
    except (InvalidOperation, ValueError):
        return None
    integral = scaled.to_integral_value()
    return int(integral) if scaled == integral else None


def _rfq(paths: list[Path], max_rows: int) -> dict:
    rows = malformed = created = deleted = 0
    recv = []
    costs = []
    contracts = []
    lifetimes = []
    creates = {}
    markets = set()
    stopped = False
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if rows >= max_rows:
                    stopped = True
                    break
                if not line.strip():
                    continue
                rows += 1
                try:
                    outer = json.loads(line)
                    wall = outer.get("recv_wall_ns")
                    raw = outer.get("raw")
                    if type(wall) is not int or not isinstance(raw, str):
                        continue
                    frame = json.loads(raw)
                    typ = frame.get("type")
                    msg = frame.get("msg")
                    if typ not in ("rfq_created", "rfq_deleted") or not isinstance(msg, dict):
                        continue
                    recv.append(wall)
                    rid = msg.get("id")
                    ticker = msg.get("market_ticker")
                    if isinstance(ticker, str) and ticker:
                        markets.add(ticker)
                    if typ == "rfq_created":
                        created += 1
                        if isinstance(rid, str) and rid:
                            creates[rid] = wall
                        value = _fixed(msg.get("target_cost_dollars"), 6)
                        if value is not None:
                            costs.append(value)
                        value = _fixed(msg.get("contracts_fp"), 2)
                        if value is not None:
                            contracts.append(value)
                    else:
                        deleted += 1
                        if isinstance(rid, str) and rid in creates and wall >= creates[rid]:
                            lifetimes.append((wall - creates.pop(rid)) / 1e9)
                except (ValueError, TypeError, json.JSONDecodeError):
                    malformed += 1
            if stopped:
                break
    recv.sort()
    inter_us = [(b-a)/1000.0 for a, b in zip(recv, recv[1:]) if b >= a]
    return {
        "status": "DATA_STARVED",
        "reason": "RFQ-to-CLOB as-of event study and matched controls were not run",
        "diagnostic_input_profile": {
            "rfq_created": created, "rfq_deleted": deleted,
            "markets": len(markets), "interarrival_us": _summary(inter_us),
            "target_cost_e6": _summary(costs),
            "contracts_e2": _summary(contracts),
            "create_to_delete_seconds": _summary(lifetimes),
            "right_censored_at_scan_end": len(creates),
        },
        "input_files": len(paths), "recorder_rows_scanned": rows,
        "bounded_scan": stopped, "max_rows": max_rows,
        "malformed_rows": malformed,
        "profitability_boundary": "broadcast lacks accepted quote/fill; RFQ PnL unavailable",
    }


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _markdown(run_id: str, coverage: dict, studies: list[dict]) -> str:
    lines = [
        "# SPORTS-AUTORESEARCH Cycle-0 — %s" % run_id, "",
        "> **EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION**",
        "", "This pilot validates the research path and generates hypotheses. It does not",
        "claim profitability, promotion readiness, or an unseen-data verdict.", "",
        "## Data coverage", "",
    ]
    for table, value in coverage.get("tables", {}).items():
        lines.append("- **%s:** %s rows; %s markets" %
                     (table, value.get("rows", 0), value.get("markets", "—")))
    lines += ["", "## First-cycle studies", ""]
    for study in studies:
        lines += ["### %s" % study["study_id"], "",
                  "- Status: `%s`" % study["result"].get("status", "DATA_STARVED"),
                  "- Hypothesis status: `%s`" % study["hypothesis_status"],
                  "- Evidence: `%s`" % study["evidence"], "",
                  "```json", json.dumps(study["result"], indent=2, default=str), "```", ""]
    return "\n".join(lines)


def _html_report(markdown_text: str, run_id: str) -> str:
    # Self-contained audit surface; JSON/Markdown remain the machine authority.
    escaped = html.escape(markdown_text)
    return """<!doctype html><meta charset='utf-8'>
<title>SPORTS-AUTORESEARCH Cycle-0 %s</title>
<style>body{margin:0;background:#071018;color:#dbe9f4;font:14px/1.55 ui-monospace,monospace}
.bar{position:sticky;top:0;padding:12px 20px;background:#5d210c;color:#fff;font-weight:700}
pre{white-space:pre-wrap;max-width:1180px;margin:24px auto;padding:24px;background:#0d1a24;border:1px solid #29404f;border-radius:10px}</style>
<div class='bar'>EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION</div>
<pre>%s</pre>""" % (html.escape(run_id), escaped)


def run(args) -> Path:
    started = time.time()
    mission = Path(args.mission).resolve()
    if not mission.is_file():
        raise CycleError("mission file missing: %s" % mission)
    releases = [_release(p, allow_degraded=args.allow_degraded)
                for p in args.release_dir]
    if len({r["release_id"] for r in releases}) != len(releases):
        raise CycleError("duplicate release_id supplied")
    dates = [r["date"] for r in releases]
    if None in dates or len(set(dates)) != len(dates):
        raise CycleError(
            "exactly one active release per data date is required; got %s" % dates
        )
    commit = _git_commit()
    run_id = args.run_id or "%s__%s" % (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"), commit[:12]
    )
    run_dir = Path(args.run_root).resolve() / run_id
    if run_dir.exists():
        raise CycleError("run directory already exists: %s" % run_dir)
    (run_dir / "REPORT" / "charts").mkdir(parents=True)
    (run_dir / "REPORT" / "tables").mkdir(parents=True)
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "DATA_INTEGRITY" / "manifests").mkdir(parents=True)
    (run_dir / "CANDIDATE_DOSSIERS").mkdir()
    (run_dir / "REJECTED_HYPOTHESES").mkdir()
    (run_dir / "queries").mkdir()
    temp_dir = run_dir / "tmp"
    temp_dir.mkdir()

    import duckdb
    if not re.fullmatch(r"[1-9][0-9]*(?:MB|GB)", args.memory_limit):
        raise CycleError("memory-limit must look like 4096MB or 40GB")
    con = duckdb.connect()
    con.execute("SET memory_limit=%s" % _quote(args.memory_limit))
    con.execute("SET threads=%d" % args.threads)
    con.execute("SET temp_directory=%s" % _quote(str(temp_dir)))
    views = {}
    columns = {}
    for table, view in (("orderbooks_l1", "ar_l1"),
                        ("trades", "ar_trades"),
                        ("orderbooks_full", "ar_l2")):
        paths = [p for r in releases for p in r["facts"][table]]
        sql = _relation_sql(table, paths)
        if sql:
            con.execute("CREATE TEMP VIEW %s AS %s" % (view, sql))
            views[table] = view
            columns[table] = _columns(con, view)
        else:
            columns[table] = set()

    coverage = {"releases": [], "tables": {}}
    for release in releases:
        coverage["releases"].append({
            "release_id": release["release_id"], "date": release["date"],
            "evidence_tier": release["tier"], "tl1_status": release["tl1_status"],
            "manifest_sha256": release["manifest_sha256"],
            "evidence_tier_basis": release["manifest"].get("evidence_tier_basis"),
            "fact_files": {k: len(v) for k, v in release["facts"].items()},
            "rfq_files": len(release["rfq"]),
            "consumed_objects": release["consumed_objects"],
        })
        shutil.copyfile(
            release["path"] / "MANIFEST.json",
            run_dir / "DATA_INTEGRITY" / "manifests" /
            (release["release_id"] + ".json"),
        )
    for table, view in views.items():
        coverage["tables"][table] = _table_coverage(con, view, columns[table])

    all_rfq = [p for r in releases for p in r["rfq"]]
    evidence = "+".join(sorted({r["tier"] for r in releases}))
    study_results = [
        ("STUDY-SPORT-ATLAS-01", None,
         {"status": "DATA_STARVED",
          "reason": "full event-first atlas and required coverage cube were not built in path smoke",
          "diagnostic_input_profile": {
              "coverage_tables": sorted(coverage["tables"]),
              "coverage_level": "CYCLE0_COARSE_COVERAGE",
          }}),
        ("THREE-WAY-OVERROUND", None,
         _overround(con, columns["orderbooks_l1"]) if "orderbooks_l1" in views else
         {"status": "DATA_STARVED", "reason": "L1 absent"}),
        ("LARGE-FLOW-CONTINUATION", None,
         _large_flow(con, columns["trades"]) if "trades" in views else
         {"status": "DATA_STARVED", "reason": "trades absent"}),
        ("L2-DEPLETION-REFILL", None,
         _l2(con, columns["orderbooks_full"]) if "orderbooks_full" in views else
         {"status": "DATA_STARVED", "reason": "L2 absent"}),
        ("RFQ-FLOW-CLOB", None, _rfq(all_rfq, args.rfq_max_rows)),
    ]
    studies = []
    for study_id, forced_status, result in study_results:
        status = forced_status or result.get("status", "DATA_STARVED")
        hypotheses_status = "DESCRIPTIVE_SURVIVOR" if status == "DESCRIPTIVE_SURVIVOR" else "DATA_STARVED"
        studies.append({
            "study_id": study_id, "hypothesis_status": hypotheses_status,
            "evidence": evidence, "split": "EXPLORATORY_ONLY",
            "artifact_status": "DIAGNOSTIC_ONLY", "result": result,
        })

    shutil.copyfile(mission, run_dir / "MISSION.md")
    pilot_scope = (
        "# Cycle-0 pilot scope\n\n"
        "Operator authorized one exploratory trial run. This run resolves the draft's "
        "pilot-only conflicts by forbidding PROMOTION_READY/VERDICT/live claims, "
        "writing no S3 objects, and keeping all method/report artifacts inside this run.\n"
    )
    (run_dir / "PILOT_SCOPE.md").write_text(pilot_scope, encoding="utf-8")
    manifest = {
        "schema_version": "sports-autoresearch-path-smoke-v1", "run_id": run_id,
        "created_at_utc": _now(), "repo_commit": commit,
        "repo_dirty_state_sha256": _git_dirty_digest(),
        "runner_sha256": _sha256(Path(__file__).resolve()),
        "mission_path": str(mission), "mission_sha256": _sha256(mission),
        "mode": "EXPLORATORY_PATH_SMOKE", "pilot": True,
        "banners": BANNER, "releases": coverage["releases"],
        "environment": {"python": platform.python_version(),
                        "duckdb": duckdb.__version__, "platform": platform.platform(),
                        "memory_limit": args.memory_limit, "threads": args.threads,
                        "temp_directory": str(temp_dir)},
    }
    _write_json(run_dir / "RUN_MANIFEST.json", manifest)
    _write_json(run_dir / "DATA_COVERAGE.json", coverage)
    _write_json(run_dir / "PRIOR_EXPOSURE.json", {
        "rule": "all releases available at pilot start are PRIOR_EXPOSED",
        "release_ids": [r["release_id"] for r in releases],
        "dates": [r["date"] for r in releases],
    })
    _write_json(run_dir / "HYPOTHESIS_LEDGER.json", {"hypotheses": studies})
    with (run_dir / "TRIAL_REGISTRY.jsonl").open("w", encoding="utf-8") as handle:
        for study in studies:
            handle.write(json.dumps(study, sort_keys=True, default=str) + "\n")
    with (run_dir / "INCLUSION_EXCLUSION.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["release_id", "date", "decision", "reason"])
        for release in releases:
            writer.writerow([release["release_id"], release["date"], "INCLUDE",
                             "VERSION_BOUND; exploratory-admissible tier"])
    full = _markdown(run_id, coverage, studies)
    (run_dir / "DATA_COVERAGE.md").write_text(
        "# Data coverage\n\n```json\n%s\n```\n" % json.dumps(coverage, indent=2, default=str),
        encoding="utf-8")
    (run_dir / "HYPOTHESIS_LEDGER.md").write_text(full, encoding="utf-8")
    (run_dir / "FEATURE_DICTIONARY.md").write_text(
        "# Cycle-0 features\n\nExecutable YES ask sum, trade count E4, L2 delta E4, "
        "RFQ receive inter-arrival and lifecycle. All are descriptive in this run.\n",
        encoding="utf-8")
    _write_json(run_dir / "FEATURE_DICTIONARY.json", {
        "features": ["threeway_executable_yes_ask_sum_e4", "trade_count_e4",
                     "l2_delta_e4", "rfq_interarrival_us", "rfq_lifetime_s"]})
    (run_dir / "REPORT" / "FULL_REPORT.md").write_text(full, encoding="utf-8")
    (run_dir / "REPORT" / "EXECUTIVE_SUMMARY.md").write_text(
        "# Executive summary\n\n**EXPLORATORY_ONLY.** Cycle-0 completed %d studies; "
        "%d were data-starved. No profitability or promotion claim was made.\n" %
        (len(studies), sum(s["hypothesis_status"] == "DATA_STARVED" for s in studies)),
        encoding="utf-8")
    (run_dir / "REPORT" / "index.html").write_text(
        _html_report(full, run_id), encoding="utf-8")
    _write_json(run_dir / "RESOURCE_USAGE.json", {
        "wall_seconds": round(time.time()-started, 3),
        "s3_bytes_read": 0,
        "note": "inputs were already-fetched W05 cache objects; fetch cost is external",
    })
    reproduce_run_id = run_id + "-reproduce-$(date -u +%Y%m%dT%H%M%SZ)"
    reproduce = (
        "python3 tools/research/autoresearch_cycle.py " +
        " ".join("--release-dir %s" % _shell_quote(str(r["path"]))
                 for r in releases) +
        " --mission %s --run-root %s --run-id %s --memory-limit %s "
        "--threads %d --rfq-max-rows %d%s\n" %
        (_shell_quote(str(mission)), _shell_quote(str(Path(args.run_root).resolve())),
         reproduce_run_id, args.memory_limit, args.threads, args.rfq_max_rows,
         " --allow-degraded" if args.allow_degraded else "")
    )
    (run_dir / "REPRODUCE.md").write_text("# Reproduce\n\n```bash\n%s```\n" % reproduce,
                                                  encoding="utf-8")
    script = run_dir / "reproduce.sh"
    script.write_text("#!/bin/sh\nset -eu\n" + reproduce, encoding="utf-8")
    script.chmod(0o755)
    shutil.rmtree(temp_dir, ignore_errors=True)
    con.close()
    print("AUTORESEARCH_PATH_SMOKE_COMPLETE run_id=%s report=%s" %
          (run_id, run_dir / "REPORT" / "index.html"))
    return run_dir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release-dir", action="append", required=True,
                        help="verified W05 cache release directory (repeatable)")
    parser.add_argument("--mission", default=str(DEFAULT_MISSION))
    parser.add_argument("--run-root", default=str(ROOT / "work" / "research" / "auto_research"))
    parser.add_argument("--run-id")
    parser.add_argument("--memory-limit", default="40GB")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--rfq-max-rows", type=int, default=2_000_000)
    parser.add_argument(
        "--allow-degraded", action="store_true",
        help="explicitly admit SEALED_DEGRADED_EVIDENCE for path smoke",
    )
    args = parser.parse_args(argv)
    if args.threads < 1 or args.rfq_max_rows < 1:
        parser.error("threads and rfq-max-rows must be positive")
    try:
        run(args)
    except (CycleError, OSError, ValueError) as exc:
        print("AUTORESEARCH_CYCLE0_BLOCKED: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
