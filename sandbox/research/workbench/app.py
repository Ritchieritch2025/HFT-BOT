#!/usr/bin/env python3
"""Local, read-only HTTP workbench for sports-market research artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


WORKBENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = WORKBENCH_DIR.parents[2]
HYPOTHESES_PATH = WORKBENCH_DIR / "hypotheses.json"
INDEX_PATH = WORKBENCH_DIR / "index.html"
SPORTS = ("Basketball", "Tennis", "Baseball")
OVERVIEW_SCHEMA = "research-workbench-overview-v1"
HYPOTHESES_SCHEMA = "research-workbench-hypotheses-v1"
EXPERIMENTS_SCHEMA = "research-workbench-experiments-v1"
EXPERIMENT_SCHEMA = "research-experiment-v1"
RESULT_STATUSES = {"NO_RESULTS", "EXPLORATORY", "INCONCLUSIVE", "PASS", "FAIL"}
HYPOTHESIS_STATUSES = {
    "DRAFT_NEEDS_FREEZE",
    "FROZEN_EXPLORATORY",
    "FROZEN_TRAIN",
    "RETIRED",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _repo_paths(repo_root: Path) -> tuple[Path, Path, Path]:
    research = repo_root / "sandbox" / "research"
    return (
        repo_root / "work" / "warehouse" / "manifest.csv",
        research / "reports" / "experiments",
        research / "reports" / "workbench",
    )


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_hypotheses(path: Path = HYPOTHESES_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        registry = json.load(handle)
    if not isinstance(registry, dict) or registry.get("schema_version") != HYPOTHESES_SCHEMA:
        raise ValueError(f"invalid hypotheses registry: expected {HYPOTHESES_SCHEMA}")
    hypotheses = registry.get("hypotheses")
    if not isinstance(hypotheses, list):
        raise ValueError("invalid hypotheses registry: hypotheses must be a list")
    seen: set[str] = set()
    for item in hypotheses:
        if not isinstance(item, dict):
            raise ValueError("invalid hypotheses registry: each hypothesis must be an object")
        hypothesis_id = item.get("hypothesis_id")
        if not isinstance(hypothesis_id, str) or not hypothesis_id:
            raise ValueError("invalid hypotheses registry: hypothesis_id is required")
        if hypothesis_id in seen:
            raise ValueError(f"invalid hypotheses registry: duplicate {hypothesis_id}")
        if item.get("status") not in HYPOTHESIS_STATUSES:
            raise ValueError(
                f"invalid hypotheses registry: {hypothesis_id} status must be one of "
                + ", ".join(sorted(HYPOTHESIS_STATUSES))
            )
        seen.add(hypothesis_id)
    return registry


def aggregate_manifest(manifest_path: Path, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    cells: dict[str, dict[tuple[str, str], int]] = {
        sport: defaultdict(int) for sport in SPORTS
    }
    total_rows = 0
    matched_rows = 0
    invalid_rows = 0
    errors: list[str] = []
    available = manifest_path.is_file()
    digest: str | None = None

    if available:
        digest = _sha256(manifest_path)
        sport_lookup = {sport.casefold(): sport for sport in SPORTS}
        with manifest_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"date", "table", "category", "subcategory", "row_count"}
            missing = sorted(required.difference(reader.fieldnames or []))
            if missing:
                raise ValueError("manifest missing columns: " + ", ".join(missing))
            for row in reader:
                total_rows += 1
                if (row.get("category") or "").strip().casefold() != "sports":
                    continue
                sport = sport_lookup.get((row.get("subcategory") or "").strip().casefold())
                if sport is None:
                    continue
                matched_rows += 1
                day = (row.get("date") or "").strip()
                table = (row.get("table") or "").strip()
                try:
                    if date.fromisoformat(day).isoformat() != day or not table:
                        raise ValueError
                    count = int((row.get("row_count") or "").strip())
                    if count < 0:
                        raise ValueError
                except ValueError:
                    invalid_rows += 1
                    continue
                cells[sport][(day, table)] += count
    else:
        errors.append("source manifest not found")

    all_dates = sorted({day for sport in SPORTS for day, _ in cells[sport]})
    all_tables = sorted({table for sport in SPORTS for _, table in cells[sport]})
    sports = []
    for sport in SPORTS:
        by_date = [
            {"date": day, "table": table, "rows": rows}
            for (day, table), rows in sorted(cells[sport].items())
        ]
        rows_by_table: dict[str, int] = defaultdict(int)
        for item in by_date:
            rows_by_table[item["table"]] += item["rows"]
        sports.append(
            {
                "sport": sport,
                "total_rows": sum(item["rows"] for item in by_date),
                "rows_by_table": dict(sorted(rows_by_table.items())),
                "by_date": by_date,
            }
        )

    return {
        "path": _display_path(manifest_path, repo_root),
        "sha256": digest,
        "available": available,
        "total_manifest_rows": total_rows,
        "matched_rows": matched_rows,
        "invalid_rows": invalid_rows,
        "errors": errors,
        "dates": all_dates,
        "tables": all_tables,
        "sports": sports,
    }


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def discover_experiments(
    experiments_dir: Path,
    hypothesis_statuses: dict[str, str],
    generated_at_utc: str,
) -> dict[str, Any]:
    experiments: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = []

    for path in sorted(experiments_dir.glob("*.json")) if experiments_dir.is_dir() else []:
        errors: list[str] = []
        payload: dict[str, Any] = {}
        try:
            with path.open(encoding="utf-8") as handle:
                parsed = json.load(handle)
            if not isinstance(parsed, dict):
                errors.append("artifact root must be a JSON object")
            else:
                payload = parsed
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"unreadable JSON: {exc}")

        if payload.get("schema_version") != EXPERIMENT_SCHEMA:
            errors.append(f"schema_version must be {EXPERIMENT_SCHEMA}")
        for field in ("experiment_id", "title"):
            if not _nonempty_string(payload.get(field)):
                errors.append(f"{field} is required and must be a non-empty string")
        hypothesis_id = payload.get("hypothesis_id")
        if not _nonempty_string(hypothesis_id):
            errors.append("hypothesis_id is required and must reference the registry")
        elif hypothesis_id not in hypothesis_statuses:
            errors.append(f"unknown hypothesis_id: {hypothesis_id}")
        result_status = payload.get("result_status")
        if result_status not in RESULT_STATUSES:
            errors.append("result_status must be one of " + ", ".join(sorted(RESULT_STATUSES)))
        elif (
            result_status != "NO_RESULTS"
            and hypothesis_id in hypothesis_statuses
            and not hypothesis_statuses[hypothesis_id].startswith("FROZEN_")
        ):
            errors.append(
                "non-NO_RESULTS artifacts require a hypothesis status beginning FROZEN_"
            )
        metrics = payload.get("metrics", [])
        if not isinstance(metrics, list):
            errors.append("metrics must be a list when present")
            metrics = []
        charts = payload.get("charts", [])
        if not isinstance(charts, list):
            errors.append("charts must be a list when present")
            charts = []
        tables = payload.get("tables", [])
        if not isinstance(tables, list):
            errors.append("tables must be a list when present")
            tables = []

        entry = {
            "artifact": path.name,
            "status": "INVALID" if errors else "VALID",
            "validation_errors": errors,
            "schema_version": payload.get("schema_version"),
            "experiment_id": payload.get("experiment_id"),
            "hypothesis_id": hypothesis_id,
            "title": payload.get("title"),
            "result_status": result_status,
            "generated_at_utc": payload.get("generated_at_utc"),
            "summary": payload.get("summary"),
            "metrics": metrics,
            "charts": charts,
            "tables": tables,
        }
        experiments.append(entry)
        if errors:
            validation_errors.append({"artifact": path.name, "errors": errors})

    return {
        "schema_version": EXPERIMENTS_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "experiments": experiments,
        "validation_errors": validation_errors,
        "valid_count": sum(item["status"] == "VALID" for item in experiments),
        "invalid_count": sum(item["status"] == "INVALID" for item in experiments),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def build_workbench(
    repo_root: Path = REPO_ROOT,
    hypotheses_path: Path = HYPOTHESES_PATH,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build a frozen API snapshot; all writes are inside reports/workbench."""
    repo_root = Path(repo_root)
    manifest_path, experiments_dir, output_dir = _repo_paths(repo_root)
    generated_at_utc = generated_at_utc or _utc_now()
    hypotheses = load_hypotheses(Path(hypotheses_path))
    hypothesis_statuses = {
        item["hypothesis_id"]: item["status"] for item in hypotheses["hypotheses"]
    }
    coverage = aggregate_manifest(manifest_path, repo_root)
    experiments = discover_experiments(
        experiments_dir, hypothesis_statuses, generated_at_utc
    )
    overview = {
        "schema_version": OVERVIEW_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "source_manifest": {
            key: coverage[key]
            for key in (
                "path",
                "sha256",
                "available",
                "total_manifest_rows",
                "matched_rows",
                "invalid_rows",
                "errors",
            )
        },
        "dates": coverage["dates"],
        "tables": coverage["tables"],
        "sports": coverage["sports"],
        "experiment_count": len(experiments["experiments"]),
        "valid_experiment_count": experiments["valid_count"],
        "invalid_experiment_count": experiments["invalid_count"],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "overview.json", overview)
    _write_json(output_dir / "hypotheses.json", hypotheses)
    _write_json(output_dir / "experiments.json", experiments)
    return {"overview": overview, "hypotheses": hypotheses, "experiments": experiments}


def is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_handler(index_path: Path = INDEX_PATH, report_dir: Path | None = None):
    report_dir = report_dir or _repo_paths(REPO_ROOT)[2]
    api_files = {
        "/api/overview": report_dir / "overview.json",
        "/api/hypotheses": report_dir / "hypotheses.json",
        "/api/experiments": report_dir / "experiments.json",
    }

    class WorkbenchHandler(BaseHTTPRequestHandler):
        server_version = "ResearchWorkbench/1"

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self._headers(status, content_type, len(body))
            self.wfile.write(body)

        def _json_error(self, status: int, message: str) -> None:
            body = json.dumps({"error": message}).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            route = urlsplit(self.path).path
            if route in ("/", "/index.html"):
                path = index_path
                content_type = "text/html; charset=utf-8"
            elif route in api_files:
                path = api_files[route]
                content_type = "application/json; charset=utf-8"
            else:
                self._json_error(404, "not found")
                return
            try:
                body = path.read_bytes()
            except FileNotFoundError:
                self._json_error(503, "workbench snapshot not built")
                return
            except OSError:
                self._json_error(500, "unable to read workbench resource")
                return
            self._send(200, content_type, body)

        def _read_only(self) -> None:
            body = json.dumps({"error": "read-only server; only GET is allowed"}).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = _read_only
        do_PUT = _read_only
        do_PATCH = _read_only
        do_DELETE = _read_only

    return WorkbenchHandler


def serve(host: str = "127.0.0.1", port: int = 8791) -> None:
    if not is_loopback_host(host):
        raise ValueError("refusing non-loopback host; use 127.0.0.1, ::1, or localhost")
    server = ThreadingHTTPServer((host, port), make_handler())
    print(f"Research Workbench: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="build the read-only API snapshot")
    for name, help_text in (
        ("serve", "serve an existing snapshot on loopback only"),
        ("run", "build, then serve on loopback only"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8791)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in {"build", "run"}:
        snapshot = build_workbench()
        print(
            json.dumps(
                {
                    "generated_at_utc": snapshot["overview"]["generated_at_utc"],
                    "sports": {
                        item["sport"]: item["total_rows"]
                        for item in snapshot["overview"]["sports"]
                    },
                    "experiments": snapshot["overview"]["experiment_count"],
                },
                sort_keys=True,
            )
        )
    if args.command in {"serve", "run"}:
        serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
