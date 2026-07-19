#!/usr/bin/env python3
"""Render a safe, self-contained HTML report from generic research results."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
from typing import Any


SCHEMA_RESULTS = "research-results-v1"
SCHEMA_RECEIPT = "research-report-receipt-v1"


class ReportError(ValueError):
    """Generic results do not satisfy the report contract."""


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return html.escape(str(value), quote=True)


def _list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportError("%s must be a list" % label)
    return value


def _table(value: dict[str, Any]) -> str:
    title = _text(value.get("title", "Table"))
    columns = value.get("columns")
    rows = value.get("rows")
    if (
        not isinstance(columns, list)
        or not columns
        or any(not isinstance(column, str) for column in columns)
        or not isinstance(rows, list)
    ):
        raise ReportError("report table has invalid columns/rows")
    body = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ReportError("report table row width differs from columns")
        body.append("<tr>%s</tr>" % "".join("<td>%s</td>" % _text(cell) for cell in row))
    return (
        "<section><h2>%s</h2><div class=scroll><table><thead><tr>%s</tr></thead>"
        "<tbody>%s</tbody></table></div></section>"
        % (title, "".join("<th>%s</th>" % _text(column) for column in columns), "".join(body))
    )


def render_report(results: dict[str, Any]) -> str:
    if not isinstance(results, dict) or results.get("schema_version") != SCHEMA_RESULTS:
        raise ReportError("results schema must be research-results-v1")
    job_id = results.get("job_id")
    title = results.get("title")
    if not isinstance(job_id, str) or not job_id or not isinstance(title, str) or not title:
        raise ReportError("results require job_id and title")
    summary = _list(results.get("summary"), "summary")
    metrics = _list(results.get("metrics"), "metrics")
    tables = _list(results.get("tables"), "tables")
    limitations = _list(results.get("limitations"), "limitations")
    receipts = results.get("receipts") or {}
    if not isinstance(receipts, dict):
        raise ReportError("receipts must be an object")
    metric_cards = []
    for metric in metrics:
        if not isinstance(metric, dict) or not isinstance(metric.get("name"), str):
            raise ReportError("metric is invalid")
        metric_cards.append(
            "<div class=metric><div class=label>%s</div><div class=value>%s%s</div>"
            "<div class=note>%s</div></div>"
            % (
                _text(metric["name"]),
                _text(metric.get("value")),
                (" <small>%s</small>" % _text(metric.get("unit"))) if metric.get("unit") else "",
                _text(metric.get("interpretation", "")),
            )
        )
    summary_html = "".join("<p>%s</p>" % _text(item) for item in summary)
    limits_html = "".join("<li>%s</li>" % _text(item) for item in limitations)
    receipts_html = "".join(
        "<tr><th>%s</th><td><code>%s</code></td></tr>" % (_text(key), _text(value))
        for key, value in sorted(receipts.items())
    )
    tables_html = "".join(_table(table) for table in tables)
    state = results.get("state", "UNKNOWN")
    mode = results.get("execution_class", "READONLY_EXPLORATORY")
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#c9d1d9;--dim:#8b949e;--blue:#58a6ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,sans-serif}}
main{{max-width:1160px;margin:auto;padding:28px}}h1{{margin:0 0 8px}}h2{{font-size:16px;margin:0 0 12px}}
.meta{{color:var(--dim);margin-bottom:18px}}.badge{{display:inline-block;border:1px solid #1f6feb;background:#0d2a4a;
color:#79c0ff;border-radius:999px;padding:2px 9px;margin-right:6px}}section{{background:var(--panel);border:1px solid var(--line);
border-radius:9px;padding:16px;margin:12px 0}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}}
.metric{{border:1px solid var(--line);border-radius:7px;padding:11px}}.label,.note{{color:var(--dim)}}.value{{font-size:23px;margin:3px 0}}
small{{font-size:12px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:7px;border-bottom:1px solid var(--line)}}
.scroll{{overflow:auto}}code{{color:var(--blue);word-break:break-all}}li{{margin:5px 0}}
</style></head><body><main>
<h1>{title}</h1><div class=meta><span class=badge>{state}</span><span class=badge>{mode}</span> job <code>{job}</code></div>
<section><h2>Summary</h2>{summary}</section>
<section><h2>Metrics</h2><div class=metrics>{metrics}</div></section>
{tables}
<section><h2>Limitations</h2><ul>{limitations}</ul></section>
<section><h2>Receipts</h2><table>{receipts}</table></section>
</main></body></html>""".format(
        title=_text(title), state=_text(state), mode=_text(mode), job=_text(job_id),
        summary=summary_html or "<p>—</p>", metrics="".join(metric_cards) or "<p>—</p>",
        tables=tables_html, limitations=limits_html or "<li>None declared.</li>",
        receipts=receipts_html or "<tr><td>—</td></tr>",
    )


def write_report(results_path: Path, output_dir: Path) -> Path:
    results_path = Path(results_path)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ReportError("report output already exists")
    try:
        raw = results_path.read_bytes()
        results = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReportError("results file is unreadable") from exc
    rendered = render_report(results).encode("utf-8")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = output_dir.parent / (".%s.preparing.%d" % (output_dir.name, os.getpid()))
    if stage.exists():
        raise ReportError("report staging path already exists")
    stage.mkdir(mode=0o750)
    receipt = {
        "schema_version": SCHEMA_RECEIPT,
        "job_id": results["job_id"],
        "results_sha256": hashlib.sha256(raw).hexdigest(),
        "report_sha256": hashlib.sha256(rendered).hexdigest(),
    }
    try:
        report = stage / "index.html"
        with report.open("xb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        receipt_raw = (
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with (stage / "REPORT_RECEIPT.json").open("xb") as handle:
            handle.write(receipt_raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, output_dir)
        directory = os.open(output_dir.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return output_dir / "index.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = write_report(args.results, args.output_dir)
    except ReportError as exc:
        print("RESEARCH_REPORT_REFUSED: %s" % exc)
        return 2
    print("RESEARCH_REPORT_READY %s" % report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
