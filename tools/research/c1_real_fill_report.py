#!/usr/bin/env python3
"""Publish the audited C1 real-fill result as charts, HTML, and PDF.

The publisher is deliberately economics-conservative: it accepts only the
canonical aggregate schema emitted by ``c1_real_fill_runner.py`` and never
renames a gross markout as net profit.  Chart source CSVs are emitted next to
the images so every visual number can be independently checked.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


SCHEMA_VERSION = "c1-real-fill-aggregates-v1"
TRACK_ORDER = (
    "STRICT_THROUGH",
    "QUEUE_PESSIMISTIC",
    "OPTIMISTIC_AT_TOUCH",
)
TRACK_LABELS = {
    "STRICT_THROUGH": "Strict-through lower bound",
    "QUEUE_PESSIMISTIC": "Queue-pessimistic diagnostic",
    "OPTIMISTIC_AT_TOUCH": "Optimistic at-touch upper bound",
}
TRACK_COLORS = {
    "STRICT_THROUGH": "#16324F",
    "QUEUE_PESSIMISTIC": "#2A9D8F",
    "OPTIMISTIC_AT_TOUCH": "#E9C46A",
}
DATE_LABEL = {
    "2026-07-12": "Jul 12",
    "2026-07-15": "Jul 15",
    "2026-07-17": "Jul 17",
}


class C1ReportError(RuntimeError):
    """The aggregate bundle cannot support an honest report."""


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise C1ReportError(f"expected object in {path}")
    return value


def _plain_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise C1ReportError(f"{label} must be an integer")
    return value


def _ratio(num: object, den: object) -> float:
    numerator = _plain_int(num, "ratio numerator")
    denominator = _plain_int(den, "ratio denominator")
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise C1ReportError("invalid non-negative bounded ratio")
    return numerator / denominator if denominator else math.nan


def validate_aggregates(data: Mapping[str, Any]) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise C1ReportError(
            f"aggregate schema must be {SCHEMA_VERSION!r}, got "
            f"{data.get('schema_version')!r}"
        )
    if data.get("claim_tier") != "EXPLORATORY_NON_GATE":
        raise C1ReportError("claim_tier must remain EXPLORATORY_NON_GATE")
    if data.get("fee_state") != "NOT_ESTIMABLE_FAIL_CLOSED":
        raise C1ReportError("fee_state must fail closed")
    verdict = data.get("verdict")
    if not isinstance(verdict, Mapping) or verdict.get("positive_promotion") is not False:
        raise C1ReportError("verdict must explicitly refuse positive promotion")
    for name in (
        "fill_rates", "markouts", "attribution", "concentration", "exclusions"
    ):
        if not isinstance(data.get(name), list):
            raise C1ReportError(f"{name} must be an array")
    for index, row in enumerate(data["fill_rates"]):
        if not isinstance(row, Mapping) or row.get("track") not in TRACK_ORDER:
            raise C1ReportError(f"invalid fill_rates[{index}]")
        _ratio(row.get("fill_rate_order_num"), row.get("fill_rate_order_den"))
        _ratio(row.get("fill_rate_qty_num_e4"), row.get("fill_rate_qty_den_e4"))
    for index, row in enumerate(data["markouts"]):
        if not isinstance(row, Mapping) or row.get("track") not in TRACK_ORDER:
            raise C1ReportError(f"invalid markouts[{index}]")
        _plain_int(row.get("weighted_gross_sum_e8"), "weighted_gross_sum_e8")
        _plain_int(row.get("filled_count_e4"), "filled_count_e4")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({str(key) for row in rows for key in row})
    if not fields:
        fields = ["state"]
        rows = [{"state": "NO_ROWS"}]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _save_figure(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _empty_axis(ax: plt.Axes, title: str) -> None:
    ax.text(
        0.5, 0.5, "No eligible observations", ha="center", va="center",
        fontsize=13, color="#5B6573", transform=ax.transAxes,
    )
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_axis_off()


def _primary(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("latency_id") == "PRIMARY"]


def chart_fill_rates(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["fill_rates"])
        if row.get("cancel_timer_us") == 200_000
    ]
    source = []
    for row in rows:
        source.append({
            "date": row["date"],
            "track": row["track"],
            "filled_orders": row["fill_rate_order_num"],
            "eligible_orders": row["fill_rate_order_den"],
            "fill_rate": _ratio(
                row["fill_rate_order_num"], row["fill_rate_order_den"]
            ),
        })
    csv_path = out / "chart_sources" / "fill_rates_primary_200ms.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "01_fill_rates_primary.png"
    fig, ax = plt.subplots(figsize=(10.5, 5.3))
    dates = sorted({str(row["date"]) for row in source})
    if not dates:
        _empty_axis(ax, "Observed virtual-order fill rate")
    else:
        width = 0.24
        x = list(range(len(dates)))
        for offset, track in enumerate(TRACK_ORDER):
            lookup = {
                str(row["date"]): float(row["fill_rate"])
                for row in source if row["track"] == track
            }
            values = [100 * lookup.get(date, 0.0) for date in dates]
            positions = [value + (offset - 1) * width for value in x]
            ax.bar(
                positions, values, width=width, label=TRACK_LABELS[track],
                color=TRACK_COLORS[track],
            )
        ax.set_xticks(x, [DATE_LABEL.get(date, date) for date in dates])
        ax.set_ylabel("Orders with any fill (%)")
        ax.set_title(
            "Observed virtual-order fill rate — 50ms placement, 200ms timer",
            loc="left", fontweight="bold",
        )
        ax.legend(frameon=False, ncol=1)
        ax.grid(axis="y", alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_markouts(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["markouts"])
        if row.get("cancel_timer_us") == 200_000
        and row.get("track") == "STRICT_THROUGH"
    ]
    source = []
    for row in rows:
        filled = _plain_int(row["filled_count_e4"], "filled_count_e4")
        weighted = _plain_int(row["weighted_gross_sum_e8"], "weighted_gross_sum_e8")
        source.append({
            "date": row["date"],
            "horizon_us": row["horizon_us"],
            "observed_slices": row["observed_slices"],
            "censored_slices": row["censored_slices"],
            "filled_count_e4": filled,
            "mean_gross_e4": weighted / filled if filled else None,
        })
    csv_path = out / "chart_sources" / "strict_markouts_primary_200ms.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "02_strict_markouts.png"
    fig, ax = plt.subplots(figsize=(10.5, 5.3))
    dates = sorted({str(row["date"]) for row in source})
    if not dates:
        _empty_axis(ax, "Strict-through executable gross markout")
    else:
        for date in dates:
            values = sorted(
                (row for row in source if row["date"] == date),
                key=lambda row: int(row["horizon_us"]),
            )
            x = [int(row["horizon_us"]) / 1000 for row in values]
            y = [
                (float(row["mean_gross_e4"]) / 100 if row["mean_gross_e4"] is not None else math.nan)
                for row in values
            ]
            ax.plot(x, y, marker="o", linewidth=2, label=DATE_LABEL.get(date, date))
        ax.axhline(0, color="#A23E48", linewidth=1.2)
        ax.set_xscale("log")
        ax.set_xticks([100, 200, 500, 1000], ["100", "200", "500", "1000"])
        ax.set_xlabel("Milliseconds after fill")
        ax.set_ylabel("Gross executable markout (¢ / contract)")
        ax.set_title(
            "Strict-through fills: executable gross markout, before all fees",
            loc="left", fontweight="bold",
        )
        ax.legend(frameon=False, ncol=3)
        ax.grid(alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_attribution(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["attribution"])
        if row.get("cancel_timer_us") == 200_000
        and row.get("track") == "STRICT_THROUGH"
        and row.get("horizon_us") in (200_000, 1_000_000)
    ]
    source = []
    for row in rows:
        filled = _plain_int(row["filled_count_e4"], "filled_count_e4")
        weighted = _plain_int(row["weighted_gross_sum_e8"], "weighted_gross_sum_e8")
        source.append({
            "date": row["date"],
            "horizon_us": row["horizon_us"],
            "refill_group": row["refill_group"],
            "filled_count_e4": filled,
            "mean_gross_e4": weighted / filled if filled else None,
        })
    csv_path = out / "chart_sources" / "strict_refill_attribution.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "03_refill_attribution.png"
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.0), sharey=True)
    for ax, horizon in zip(axes, (200_000, 1_000_000)):
        subset = [row for row in source if row["horizon_us"] == horizon]
        groups = ("REFILL_BY_TIMER", "NON_REFILL_OR_CENSORED")
        values = []
        for group in groups:
            group_rows = [row for row in subset if row["refill_group"] == group]
            num = sum(
                float(row["mean_gross_e4"]) * int(row["filled_count_e4"])
                for row in group_rows if row["mean_gross_e4"] is not None
            )
            den = sum(
                int(row["filled_count_e4"])
                for row in group_rows if row["mean_gross_e4"] is not None
            )
            values.append(num / den / 100 if den else math.nan)
        if not subset:
            _empty_axis(ax, f"{horizon // 1000}ms")
            continue
        bars = ax.bar(
            ["Refilled", "No refill / censored"], values,
            color=["#2A9D8F", "#E76F51"],
        )
        ax.bar_label(bars, fmt="%.3f¢", padding=3, fontsize=9)
        ax.axhline(0, color="#A23E48", linewidth=1)
        ax.set_title(f"{horizon // 1000}ms after fill", loc="left", fontweight="bold")
        ax.grid(axis="y", alpha=0.2)
    axes[0].set_ylabel("Gross executable markout (¢ / contract)")
    fig.suptitle(
        "Adverse-selection attribution — descriptive, not a net-PnL ledger",
        x=0.04, ha="left", fontweight="bold",
    )
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_queue_band(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    rows = [
        row for row in _primary(data["fill_rates"])
        if row.get("cancel_timer_us") in (200_000, 300_000)
    ]
    grouped: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["cancel_timer_us"]), str(row["track"]))].append(row)
    source = []
    for (timer, track), parts in sorted(grouped.items()):
        num = sum(int(row["fill_rate_order_num"]) for row in parts)
        den = sum(int(row["fill_rate_order_den"]) for row in parts)
        source.append({
            "cancel_timer_us": timer,
            "track": track,
            "filled_orders": num,
            "eligible_orders": den,
            "fill_rate": num / den if den else None,
        })
    csv_path = out / "chart_sources" / "queue_band_primary.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "04_queue_band.png"
    fig, ax = plt.subplots(figsize=(10.5, 5.3))
    if not source:
        _empty_axis(ax, "Queue uncertainty band")
    else:
        x = [0, 1]
        timers = [200_000, 300_000]
        for track in TRACK_ORDER:
            lookup = {
                int(row["cancel_timer_us"]): row["fill_rate"]
                for row in source if row["track"] == track
            }
            y = [
                100 * float(lookup[timer]) if lookup.get(timer) is not None else math.nan
                for timer in timers
            ]
            ax.plot(
                x, y, marker="o", linewidth=2.4, color=TRACK_COLORS[track],
                label=TRACK_LABELS[track],
            )
        ax.set_xticks(x, ["Cancel at 200ms", "Cancel at 300ms"])
        ax.set_ylabel("Orders with any fill (%)")
        ax.set_title(
            "Queue-model band — the strict line is the binding lower bound",
            loc="left", fontweight="bold",
        )
        ax.legend(frameon=False, loc="center left", bbox_to_anchor=(1.01, 0.5))
        ax.grid(axis="y", alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def chart_exclusions(data: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    source = []
    for row in data["exclusions"]:
        if not isinstance(row, Mapping):
            raise C1ReportError("invalid exclusion row")
        source.append({
            "reason": str(row.get("reason", "UNKNOWN")),
            "count": _plain_int(row.get("count", 0), "exclusion count"),
        })
    source.sort(key=lambda row: (-int(row["count"]), str(row["reason"])))
    csv_path = out / "chart_sources" / "exclusion_waterfall.csv"
    _write_csv(csv_path, source)
    png_path = out / "charts" / "05_exclusions.png"
    shown = source[:12]
    fig, ax = plt.subplots(figsize=(10.5, 6.0))
    if not shown:
        _empty_axis(ax, "Fail-closed exclusion waterfall")
    else:
        labels = [str(row["reason"]).replace("_", " ").title() for row in shown][::-1]
        values = [int(row["count"]) for row in shown][::-1]
        ax.barh(labels, values, color="#6C8EAD")
        ax.set_xlabel("Campaign / observation count")
        ax.set_title("Fail-closed exclusion waterfall", loc="left", fontweight="bold")
        ax.grid(axis="x", alpha=0.2)
    _save_figure(fig, png_path)
    return png_path, csv_path


def generate_charts(data: Mapping[str, Any], out: Path) -> list[Path]:
    (out / "charts").mkdir(parents=True, exist_ok=True)
    (out / "chart_sources").mkdir(parents=True, exist_ok=True)
    functions = (
        chart_fill_rates,
        chart_markouts,
        chart_attribution,
        chart_queue_band,
        chart_exclusions,
    )
    return [function(data, out)[0] for function in functions]


def _image_data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _fmt_int(value: object) -> str:
    return f"{_plain_int(value, 'display integer'):,}"


def _primary_strict_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        row for row in data["fill_rates"]
        if row.get("latency_id") == "PRIMARY"
        and row.get("cancel_timer_us") == 200_000
        and row.get("track") == "STRICT_THROUGH"
    ]


def build_html(data: Mapping[str, Any], charts: Sequence[Path], path: Path) -> None:
    verdict = data["verdict"]
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else {}
    strict_rows = _primary_strict_rows(data)
    eligible = sum(int(row["fill_rate_order_den"]) for row in strict_rows)
    filled = sum(int(row["fill_rate_order_num"]) for row in strict_rows)
    fill_rate = 100 * filled / eligible if eligible else math.nan
    image_sections = []
    captions = (
        "Three fill tracks by clean date",
        "Strict-through executable gross markout",
        "Refill versus non-refill adverse-selection attribution",
        "Queue-model uncertainty band",
        "Fail-closed exclusion waterfall",
    )
    for chart, caption in zip(charts, captions):
        image_sections.append(
            f'<section class="chart"><h3>{html.escape(caption)}</h3>'
            f'<img src="{_image_data_uri(chart)}" alt="{html.escape(caption)}"></section>'
        )
    concentration_rows = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(row.get(key, '')))}</td>"
            for key in ("date", "events", "markets", "max_market_share", "hhi")
        ) + "</tr>"
        for row in data["concentration"]
    )
    generated = str(summary.get("generated_at_utc", data.get("generated_at_utc", "BOUND_TO_RUN")))
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>C1 Real-Fill Kill Test</title><style>
:root{{--ink:#17202a;--muted:#5b6573;--navy:#16324f;--teal:#2a9d8f;--sand:#f4f1ea;--red:#a23e48}}
*{{box-sizing:border-box}} body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:var(--ink);background:#edf1f4}}
main{{max-width:1120px;margin:24px auto;background:white;padding:48px 56px;box-shadow:0 6px 32px #10203020}}
.eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--teal);font-weight:700}}
h1{{font-size:38px;line-height:1.1;margin:8px 0 10px}} h2{{margin-top:40px;border-bottom:2px solid #e7ecef;padding-bottom:8px}} h3{{font-size:18px}}
.warning{{background:#fff1f0;border-left:5px solid var(--red);padding:18px 20px;margin:22px 0}}
.verdict{{background:var(--navy);color:white;padding:24px;margin:22px 0}} .verdict strong{{font-size:24px;display:block;margin-bottom:8px}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0}} .metric{{background:var(--sand);padding:16px}} .metric b{{font-size:24px;display:block}}
.chart{{margin:30px 0;border-top:1px solid #e7ecef;padding-top:18px}} .chart img{{width:100%;height:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{border-bottom:1px solid #dde3e8;padding:9px;text-align:right}} th:first-child,td:first-child{{text-align:left}}
code{{overflow-wrap:anywhere}} .muted{{color:var(--muted)}} ul{{line-height:1.55}} footer{{margin-top:42px;color:var(--muted);font-size:12px}}
@media(max-width:760px){{main{{margin:0;padding:24px}}.metrics{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main>
<div class="eyebrow">C1 · Deepresearch V3 follow-through · {html.escape(generated)}</div>
<h1>Real-Fill Kill Test<br><span class="muted">深度回补做市的真实成交下界</span></h1>
<p>本报告检验“显示深度回补”能否转化为一张真实可成交的被动单。输入只来自 2026-07-12、07-15、07-17 三个通过 L2 质量门的精确版本。</p>
<div class="warning"><b>EXPLORATORY_NON_GATE</b><br>所有金额均为手续费前的 gross 可执行 markout，不是净利润。历史动态费率、账户舍入、实测下单/撤单延迟和终值账本尚未闭合。</div>
<div class="verdict"><strong>{html.escape(str(verdict.get('code')))}</strong>{html.escape(str(verdict.get('reason', '')))}</div>
<div class="metrics">
<div class="metric"><span>Clean dates</span><b>3</b></div>
<div class="metric"><span>Primary strict eligible</span><b>{eligible:,}</b></div>
<div class="metric"><span>Primary strict filled</span><b>{filled:,}</b></div>
<div class="metric"><span>Observed fill rate</span><b>{fill_rate:.3f}%</b></div>
</div>
<h2>Executive reading</h2>
<ul><li>Headline uses only <b>strict-through</b>: an at-price print never counts as our fill.</li><li>Queue-pessimistic and optimistic-at-touch are diagnostics, not promotion evidence.</li><li>No p-value is reported: three clean dates are shown independently, not treated as hundreds of thousands of independent trials.</li><li>A positive pilot verdict can only retain C1 for a 20-day validation; it cannot authorize live trading.</li></ul>
<h2>Results</h2>{''.join(image_sections)}
<h2>Support and concentration</h2>
<table><thead><tr><th>Date</th><th>Events</th><th>Markets</th><th>Max market share</th><th>HHI</th></tr></thead><tbody>{concentration_rows}</tbody></table>
<h2>What remains blocked</h2><ul>
<li><b>Fee-after net PnL:</b> {html.escape(str(data.get('fee_state')))}.</li>
<li><b>Latency:</b> 5/50/500ms are preregistered diagnostics, not measured production distributions.</li>
<li><b>Queue truth:</b> public L2 cannot identify whether a negative delta canceled the front or back of the queue.</li>
<li><b>Validation:</b> only three clean dates; final train/validation gate needs at least 20.</li></ul>
<h2>Reproducibility</h2><pre>{html.escape(json.dumps(data.get('provenance', {}), ensure_ascii=False, indent=2, sort_keys=True))}</pre>
<footer>Experiment {html.escape(str(data.get('experiment_id')))} · live-order writes: 0 · positive_promotion: false</footer>
</main></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def _register_pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("C1Unicode", str(candidate)))
            return "C1Unicode"
        except Exception:
            continue
    return "Helvetica"


def build_pdf(data: Mapping[str, Any], charts: Sequence[Path], path: Path) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
            Spacer, Table, TableStyle,
        )
    except ImportError as exc:
        raise C1ReportError("ReportLab is required to publish the PDF") from exc

    font = _register_pdf_font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="C1Title", parent=styles["Title"], fontName=font, fontSize=26,
        leading=31, textColor=colors.HexColor("#16324F"), alignment=TA_LEFT,
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="C1H1", parent=styles["Heading1"], fontName=font, fontSize=17,
        leading=21, textColor=colors.HexColor("#16324F"), spaceBefore=14,
        spaceAfter=8,
    ))
    styles.add(ParagraphStyle(
        name="C1Body", parent=styles["BodyText"], fontName=font, fontSize=9.5,
        leading=14, spaceAfter=7,
    ))
    styles.add(ParagraphStyle(
        name="C1Small", parent=styles["BodyText"], fontName=font, fontSize=7.5,
        leading=10, textColor=colors.HexColor("#5B6573"),
    ))
    styles.add(ParagraphStyle(
        name="C1Warning", parent=styles["BodyText"], fontName=font, fontSize=10,
        leading=14, textColor=colors.HexColor("#7B1E24"), borderColor=colors.HexColor("#A23E48"),
        borderWidth=1, borderPadding=9, backColor=colors.HexColor("#FFF1F0"),
        spaceBefore=6, spaceAfter=12,
    ))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path), pagesize=letter, leftMargin=0.58 * inch,
        rightMargin=0.58 * inch, topMargin=0.55 * inch, bottomMargin=0.72 * inch,
        title="C1 Real-Fill Kill Test", author="HFT BOT Research",
    )
    story: list[Any] = []
    verdict = data["verdict"]
    strict = _primary_strict_rows(data)
    eligible = sum(int(row["fill_rate_order_den"]) for row in strict)
    filled = sum(int(row["fill_rate_order_num"]) for row in strict)
    rate = 100 * filled / eligible if eligible else math.nan
    story.extend([
        Paragraph("C1 REAL-FILL KILL TEST", styles["C1Title"]),
        Paragraph("深度回补做市：从显示回补到真实成交下界", styles["C1H1"]),
        Paragraph(
            "EXPLORATORY_NON_GATE — 所有金额均为手续费前的 gross 可执行 markout，"
            "不是净利润；本报告不授权任何实盘订单。", styles["C1Warning"],
        ),
        Paragraph(f"<b>Verdict: {html.escape(str(verdict.get('code')))}</b>", styles["C1H1"]),
        Paragraph(html.escape(str(verdict.get("reason", ""))), styles["C1Body"]),
    ])
    metrics = [
        ["Clean dates", "Primary eligible", "Strict filled", "Strict rate"],
        ["3", f"{eligible:,}", f"{filled:,}", f"{rate:.3f}%"],
    ]
    table = Table(metrics, colWidths=[1.7 * inch] * 4)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16324F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F4F1EA")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 10)])
    story.append(Paragraph(
        "主结果只使用 strict-through：等价成交不算填单。Queue-pessimistic 与 "
        "optimistic-at-touch 只展示模型带宽。三天逐日报告，不进行 t 检验或显著性声明。",
        styles["C1Body"],
    ))
    captions = (
        "1. 三轨成交率",
        "2. 严格穿价成交后的可执行毛 markout",
        "3. 回补 / 不回补的逆向选择分账",
        "4. 队列模型带宽",
        "5. Fail-closed 排除瀑布",
    )
    for index, (chart, caption) in enumerate(zip(charts, captions)):
        if index in (1, 3):
            story.append(PageBreak())
        image = Image(str(chart), width=7.1 * inch, height=3.38 * inch)
        story.append(KeepTogether([
            Paragraph(caption, styles["C1H1"]), image,
            Paragraph(
                "Source CSV 与本图同目录；gross ≠ net，空缺/跨 epoch/过期状态均已 censor。",
                styles["C1Small"],
            ),
        ]))
    story.append(PageBreak())
    story.extend([
        Paragraph("为什么现在仍不能说“赚钱”", styles["C1H1"]),
        Paragraph(
            "精确版本未闭合逐成交历史 fee change、全部 event override、账户舍入精度与"
            "订单级 accumulator；5/50/500ms 也是预注册压力值而不是生产实测。故 fee-after "
            "net PnL、容量、日利润和 G3 晋级全部 fail closed。", styles["C1Body"],
        ),
        Paragraph("下一决策", styles["C1H1"]),
        Paragraph(
            "若 verdict 保留候选：补齐费用证据与生产 place/cancel latency，继续累计至少 20 "
            "个干净 L2 日后进行按日 train/validation。若 verdict 淘汰：保留深度不回补作为"
            "撤单/退出信号，不再把 C1 当进攻性做市策略。", styles["C1Body"],
        ),
        Paragraph("Provenance", styles["C1H1"]),
        Paragraph(
            html.escape(json.dumps(data.get("provenance", {}), ensure_ascii=False, sort_keys=True)),
            styles["C1Small"],
        ),
    ])

    def footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#5B6573"))
        canvas.drawString(0.58 * inch, 0.34 * inch, str(data.get("experiment_id")))
        canvas.drawRightString(7.92 * inch, 0.34 * inch, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def publish(input_dir: Path, output_dir: Path, pdf_path: Path) -> dict[str, Any]:
    data = _load_json(input_dir / "C1_AGGREGATES.json")
    validate_aggregates(data)
    output_dir.mkdir(parents=True, exist_ok=True)
    charts = generate_charts(data, output_dir)
    html_path = output_dir / "index.html"
    build_html(data, charts, html_path)
    build_pdf(data, charts, pdf_path)
    return {
        "html": str(html_path),
        "pdf": str(pdf_path),
        "charts": [str(path) for path in charts],
        "chart_sources": sorted(
            str(path) for path in (output_dir / "chart_sources").glob("*.csv")
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pdf-path", type=Path, required=True)
    args = parser.parse_args(argv)
    result = publish(args.input_dir, args.output_dir, args.pdf_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
