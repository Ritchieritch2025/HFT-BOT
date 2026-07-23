#!/usr/bin/env python3
"""Render the shared PnL spine and three-experiment execution report.

This is an offline renderer.  It reads only redacted, tracked receipts and
never reads credentials, private fill rows, raw order identifiers, AWS, or the
venue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
REAL_DATA = (
    ROOT
    / "Deepresearch V3"
    / "pnl_spine"
    / "evidence"
    / "REAL_DATA_AUDIT_2026-07-23.json"
)
FEE_AGGREGATE = (
    ROOT
    / "Deepresearch V3"
    / "pnl_spine"
    / "evidence"
    / "PRIVATE_FEE_AGGREGATE_2026-07-23.json"
)
ACCOUNT_PRECISION = (
    ROOT
    / "Deepresearch V3"
    / "pnl_spine"
    / "evidence"
    / "ACCOUNT_PRECISION_2026-07-23.json"
)
LATENCY = (
    ROOT
    / "Deepresearch V3"
    / "registry"
    / "receipts"
    / "PNL_SPINE_LATENCY_BLOCKED_20260723.json"
)
DEFAULT_OUTPUT = (
    ROOT
    / "output"
    / "pdf"
    / "PNL_SPINE_THREE_FROZEN_EXPERIMENTS_EXECUTION_2026-07-23_CN.pdf"
)

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 36
FONT_BODY = "ArialUnicode"
FONT_HEAD = "STHeiti"
FONT_MONO = "Courier"
FONT_MONO_BOLD = "Courier-Bold"

NAVY = HexColor("#0B132B")
INK = HexColor("#17213A")
SLATE = HexColor("#42526E")
MUTED = HexColor("#718096")
PAPER = HexColor("#FBFCFE")
LIGHT = HexColor("#F4F7FB")
BORDER = HexColor("#DCE4EF")
BLUE = HexColor("#3478F6")
TEAL = HexColor("#16A394")
GREEN = HexColor("#2F9E74")
AMBER = HexColor("#ECA83E")
CORAL = HexColor("#F36B5D")
RED = HexColor("#D84B4B")
PURPLE = HexColor("#7657D5")
SKY = HexColor("#DCEBFF")
TEAL_LIGHT = HexColor("#DDF5F1")
GREEN_LIGHT = HexColor("#E2F4EA")
AMBER_LIGHT = HexColor("#FFF0D2")
RED_LIGHT = HexColor("#FCE5E5")
PURPLE_LIGHT = HexColor("#ECE8FB")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _register_fonts() -> None:
    body = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    head = Path("/System/Library/Fonts/STHeiti Medium.ttc")
    if not body.exists() or not head.exists():
        raise FileNotFoundError("Required Chinese fonts are unavailable")
    pdfmetrics.registerFont(TTFont(FONT_BODY, str(body)))
    pdfmetrics.registerFont(TTFont(FONT_HEAD, str(head)))


def _wrap(text: object, width: float, font: str, size: float) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and pdfmetrics.stringWidth(
                candidate, font, size
            ) > width:
                lines.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        lines.append(current.rstrip())
    return lines


def _text(
    c: canvas.Canvas,
    value: object,
    x: float,
    y: float,
    width: float,
    *,
    font: str = FONT_BODY,
    size: float = 9,
    color: Color = INK,
    leading: float | None = None,
    max_lines: int | None = None,
) -> float:
    leading = leading or size * 1.34
    lines = _wrap(value, width, font, size)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            suffix = "…"
            while lines[-1] and pdfmetrics.stringWidth(
                lines[-1] + suffix, font, size
            ) > width:
                lines[-1] = lines[-1][:-1]
            lines[-1] += suffix
    c.setFont(font, size)
    c.setFillColor(color)
    cursor = y
    for line in lines:
        c.drawString(x, cursor, line)
        cursor -= leading
    return cursor


def _round_box(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: Color = white,
    stroke: Color = BORDER,
    radius: float = 9,
    line_width: float = 0.8,
) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(line_width)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)


def _pill(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    *,
    fill: Color,
    color: Color,
    font: str = FONT_MONO_BOLD,
    size: float = 7.2,
    pad_x: float = 7,
    h: float = 17,
) -> float:
    w = pdfmetrics.stringWidth(text, font, size) + 2 * pad_x
    c.setFillColor(fill)
    c.setStrokeColor(fill)
    c.roundRect(x, y, w, h, h / 2, fill=1, stroke=0)
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawCentredString(x + w / 2, y + 5.2, text)
    return w


def _page(
    c: canvas.Canvas,
    page_no: int,
    section: str,
    *,
    status: str = "ENGINEERING / NO LIVE AUTHORITY",
) -> None:
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(NAVY)
    c.rect(0, PAGE_H - 28, PAGE_W, 28, fill=1, stroke=0)
    c.setFont(FONT_HEAD, 9)
    c.setFillColor(white)
    c.drawString(MARGIN, PAGE_H - 19, "SHARED PnL SPINE")
    c.setFont(FONT_BODY, 8)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 19, section)
    c.setFont(FONT_MONO_BOLD, 7)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - 19, status)
    c.setStrokeColor(BORDER)
    c.line(MARGIN, 24, PAGE_W - MARGIN, 24)
    c.setFont(FONT_BODY, 7.2)
    c.setFillColor(MUTED)
    c.drawString(
        MARGIN,
        12,
        "2026-07-23 · exact tracked receipts · identifiers and credentials excluded",
    )
    c.drawRightString(PAGE_W - MARGIN, 12, f"{page_no} / 7")


def _metric(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    value: str,
    label: str,
    *,
    color: Color = BLUE,
    note: str = "",
) -> None:
    _round_box(c, x, y, w, 76, fill=white, stroke=BORDER)
    c.setFillColor(color)
    c.roundRect(x, y, 5, 76, 3, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont(FONT_HEAD, 20)
    c.drawString(x + 15, y + 43, value)
    c.setFillColor(SLATE)
    c.setFont(FONT_BODY, 8.4)
    c.drawString(x + 15, y + 25, label)
    if note:
        c.setFillColor(MUTED)
        c.setFont(FONT_BODY, 6.8)
        c.drawString(x + 15, y + 10, note)


def _flow_arrow(
    c: canvas.Canvas, x1: float, y: float, x2: float, color: Color = MUTED
) -> None:
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(1.4)
    c.line(x1, y, x2 - 7, y)
    c.line(x2 - 7, y, x2 - 12, y + 4)
    c.line(x2 - 7, y, x2 - 12, y - 4)


def _title(c: canvas.Canvas, title: str, subtitle: str) -> None:
    c.setFillColor(NAVY)
    c.setFont(FONT_HEAD, 22)
    c.drawString(MARGIN, PAGE_H - 68, title)
    c.setFillColor(SLATE)
    c.setFont(FONT_BODY, 9.5)
    c.drawString(MARGIN, PAGE_H - 87, subtitle)


def _experiment_name(experiment_id: str) -> str:
    return {
        "A01-SPREAD-CAPTURE": "A01 · 价差捕获",
        "A11-ONE-SIDED-PROVISION": "A11 · 单边缺失补流动性",
        "B09-LISTING-TO-START-DRIFT": "B09 · 上市至开赛漂移",
    }[experiment_id]


def _short_blockers(experiment_id: str) -> list[str]:
    return {
        "A01-SPREAD-CAPTURE": [
            "缺精确 bid/ask dwell + warmup 状态",
            "TRAIN P90/P75 门未冻结",
            "缺 scheduled-start as-of 绑定",
        ],
        "A11-ONE-SIDED-PROVISION": [
            "C1 depletion ≠ 连续 30s 单边状态",
            "缺 root event / onset / survivor 直接字段",
            "缺 post-decision cancel lifecycle + exact exit",
        ],
        "B09-LISTING-TO-START-DRIFT": [
            "TRAIN/validation 日期尚未分配",
            "direction/cell/horizon 未训练封印",
            "缺 listing age + scheduled phase 状态行",
        ],
    }[experiment_id]


def render(
    output: Path,
    *,
    audit_status: str,
    audited_runtime_commit: str,
) -> None:
    _register_fonts()
    real = _load(REAL_DATA)
    fee = _load(FEE_AGGREGATE)
    account = _load(ACCOUNT_PRECISION)
    latency = _load(LATENCY)
    experiments = {
        row["experiment_id"]: row for row in real["experiments"]
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output), pagesize=(PAGE_W, PAGE_H))
    c.setTitle("PnL Spine 与三个冻结实验：端到端执行实况")
    c.setAuthor("HFT BOT Research")
    c.setSubject("Fail-closed research PnL execution report")

    # Page 1 — executive outcome.
    _page(c, 1, "EXECUTIVE OUTCOME")
    _title(
        c,
        "PnL Spine 与三个冻结实验：端到端执行实况",
        "先统一现金账本，再用真实证据运行；结论不因进度压力而降格。",
    )
    _round_box(c, MARGIN, PAGE_H - 155, PAGE_W - 2 * MARGIN, 47, fill=RED_LIGHT, stroke=RED)
    _pill(
        c,
        "3 / 3 NET PnL BLOCKED",
        MARGIN + 14,
        PAGE_H - 140,
        fill=RED,
        color=white,
        size=8,
        h=20,
    )
    _text(
        c,
        "这不是“策略亏损”的结论，而是输入契约尚未满足。当前任何净 PnL 数字都会混入未实测延迟、非精确退出或未冻结参数，因此系统正确拒绝发布。",
        MARGIN + 185,
        PAGE_H - 126,
        PAGE_W - 2 * MARGIN - 200,
        size=9,
        color=INK,
        max_lines=2,
    )
    gap = 10
    tile_w = (PAGE_W - 2 * MARGIN - 4 * gap) / 5
    y = PAGE_H - 250
    metrics = [
        ("1,116,054", "C1 场景行", BLUE, "3 个干净工程日"),
        ("171,816", "严格成交切片", TEAL, "strict-through 工程证据"),
        ("687,264", "gross markout 行", PURPLE, "明确不是现金 PnL"),
        ("372", "真实 fee_cost fills", AMBER, "$953.982830 历史实付"),
        ("0", "可发布 Net PnL 行", RED, "A01 / A11 / B09"),
    ]
    for index, (value, label, color, note) in enumerate(metrics):
        _metric(c, MARGIN + index * (tile_w + gap), y, tile_w, value, label, color=color, note=note)

    _round_box(c, MARGIN, 62, 360, 220, fill=white)
    c.setFont(FONT_HEAD, 12)
    c.setFillColor(INK)
    c.drawString(MARGIN + 16, 257, "本轮已经交付")
    done = [
        "MoneyE6 固定点现金、费用、仓位与终局账本",
        "A01 / A11 / B09 冻结 adapter 与 baseline",
        "严格穿价成交、全局 public volume 一次分配",
        "exact L2 IOC / FIFO 分段实现 / 唯一终局",
        "真实 fee_cost 聚合与只读延迟证据审计",
        "UTC 日风险 latch + root-pinned lineage 硬门",
    ]
    cursor = 235
    for item in done:
        c.setFillColor(GREEN)
        c.circle(MARGIN + 20, cursor + 2, 3.4, fill=1, stroke=0)
        cursor = _text(c, item, MARGIN + 31, cursor + 6, 310, size=8.4, max_lines=2) - 6

    _round_box(c, MARGIN + 376, 62, PAGE_W - 2 * MARGIN - 376, 220, fill=white)
    c.setFont(FONT_HEAD, 12)
    c.setFillColor(INK)
    c.drawString(MARGIN + 392, 257, "尚未满足，因而没有利润数字")
    blocked = [
        "真实 PLACE / CANCEL / IOC_EXIT 因果时钟：0 / 3",
        "真实 exact objects 尚未生成生产 extractor lineage receipt",
        "A01：训练门 + spread-dwell 状态尚未物化",
        "A11：direct fields + post-decision lifecycle 尚未物化",
        "B09：数据盲训练/验证分配及 TRAIN artifact 尚未建立",
        "三个实验的 normalized / exact-exit / net 行均为 0",
    ]
    cursor = 235
    for item in blocked:
        c.setFillColor(RED)
        c.rect(MARGIN + 396, cursor - 1, 6, 6, fill=1, stroke=0)
        cursor = _text(
            c,
            item,
            MARGIN + 411,
            cursor + 5,
            PAGE_W - 2 * MARGIN - 427,
            size=8.4,
            max_lines=2,
        ) - 6
    c.showPage()

    # Page 2 — architecture.
    _page(c, 2, "THE SHARED PnL SPINE")
    _title(
        c,
        "一条共同的 PnL Spine，而不是三套各说各话的回测",
        "每个实验只负责“何时做什么”；成交、费用、风险、退出和现金 PnL 由同一条脊柱负责。",
    )
    flow_y = 380
    flow_w = 112
    flow_h = 74
    flow_gap = 18
    stages = [
        ("01", "精确输入", "VersionId / SHA\n日期与对象绑定", BLUE, SKY),
        ("02", "冻结决策", "Signal → action\n含 NO_TRADE", PURPLE, PURPLE_LIGHT),
        ("03", "严格成交", "strict-through\n全局量守恒", TEAL, TEAL_LIGHT),
        ("04", "完整退出", "exact L2 IOC\n或唯一终局", AMBER, AMBER_LIGHT),
        ("05", "现金 PnL", "本金 − 费用\n− 变动成本", GREEN, GREEN_LIGHT),
        ("06", "可发布收据", "所有 gate PASS\n否则 BLOCKED", RED, RED_LIGHT),
    ]
    for index, (number, name, note, color, fill) in enumerate(stages):
        x = MARGIN + index * (flow_w + flow_gap)
        _round_box(c, x, flow_y, flow_w, flow_h, fill=fill, stroke=color)
        _pill(c, number, x + 9, flow_y + 47, fill=color, color=white, h=18)
        c.setFont(FONT_HEAD, 10)
        c.setFillColor(INK)
        c.drawString(x + 42, flow_y + 52, name)
        _text(c, note, x + 10, flow_y + 31, flow_w - 20, size=7.5, color=SLATE, max_lines=2)
        if index < len(stages) - 1:
            _flow_arrow(c, x + flow_w + 3, flow_y + flow_h / 2, x + flow_w + flow_gap - 3)

    c.setFont(FONT_HEAD, 12)
    c.setFillColor(INK)
    c.drawString(MARGIN, 337, "四道横向硬门")
    gates = [
        ("PROVENANCE", "exact release + root-pinned extractor lineage；记录集合须穷尽。", BLUE),
        ("FEE + LATENCY", "官方费率 + 实付 fee_cost；真实三路径因果时钟。", AMBER),
        ("RISK", "按时间回放 reserve/fill/cancel/exit；UTC 日亏损触发后锁死。", PURPLE),
        ("TERMINAL", "只认 FINALIZED exact payout；每市场唯一权威，残仓归零。", GREEN),
    ]
    gate_w = (PAGE_W - 2 * MARGIN - 3 * 12) / 4
    for index, (name, note, color) in enumerate(gates):
        x = MARGIN + index * (gate_w + 12)
        _round_box(c, x, 230, gate_w, 82, fill=white, stroke=BORDER)
        _pill(c, name, x + 12, 284, fill=color, color=white)
        _text(c, note, x + 12, 267, gate_w - 24, size=8, color=SLATE, max_lines=3)

    _round_box(c, MARGIN, 62, PAGE_W - 2 * MARGIN, 138, fill=NAVY, stroke=NAVY)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 13)
    c.drawString(MARGIN + 18, 174, "共同定义")
    formula = (
        "NetPnL = executed cash in − executed cash out − exact venue fees "
        "− realized variable costs\n+ terminal value(residual inventory)"
    )
    _text(c, formula, MARGIN + 18, 149, PAGE_W - 2 * MARGIN - 36, font=FONT_MONO_BOLD, size=10, color=white)
    _text(
        c,
        "零触发、触发未成交、baseline、部分成交、取消竞争、IOC 深度不足和最终结算都必须保留；mid markout 只能做诊断，永远不能替代现金退出。",
        MARGIN + 18,
        115,
        PAGE_W - 2 * MARGIN - 36,
        size=9,
        color=HexColor("#DDE6F5"),
        max_lines=2,
    )
    c.showPage()

    # Page 3 — real evidence.
    _page(c, 3, "REAL ARTIFACT INVENTORY")
    _title(
        c,
        "我们真正拥有什么数据，以及它能证明什么",
        "所有计数来自 hash-pinned C1 / DeepResearch V3 工件；分类优先于“看起来能算”。",
    )
    inventory = [
        ("CAMPAIGNS", 1_116_054, "候选工程场景", BLUE),
        ("FILL_SLICES", 171_816, "strict-through 成交切片", TEAL),
        ("MARKOUTS", 687_264, "gross 诊断；非现金退出", PURPLE),
    ]
    x0 = MARGIN
    bar_x = MARGIN + 135
    bar_w = 380
    y0 = 420
    max_value = max(row[1] for row in inventory)
    for index, (name, value, note, color) in enumerate(inventory):
        y = y0 - index * 65
        c.setFont(FONT_MONO_BOLD, 9)
        c.setFillColor(INK)
        c.drawString(x0, y + 14, name)
        c.setFont(FONT_BODY, 7)
        c.setFillColor(MUTED)
        c.drawString(x0, y, note)
        c.setFillColor(LIGHT)
        c.roundRect(bar_x, y + 3, bar_w, 22, 6, fill=1, stroke=0)
        filled = max(12, bar_w * value / max_value)
        c.setFillColor(color)
        c.roundRect(bar_x, y + 3, filled, 22, 6, fill=1, stroke=0)
        c.setFont(FONT_MONO_BOLD, 9)
        c.setFillColor(INK)
        c.drawRightString(bar_x + bar_w + 74, y + 10, f"{value:,}")

    _round_box(c, MARGIN + 600, 281, PAGE_W - MARGIN - (MARGIN + 600), 175, fill=RED_LIGHT, stroke=RED)
    c.setFont(FONT_HEAD, 12)
    c.setFillColor(RED)
    c.drawString(MARGIN + 616, 426, "严禁混淆")
    _text(
        c,
        "MARKOUTS 只是未来盘口观察值；没有精确 L2 逐档退出、实际吃单费用、真实 IOC 延迟或残仓终局。687,264 行只能做毛诊断，不能加总成净利润。",
        MARGIN + 616,
        400,
        PAGE_W - 2 * MARGIN - 632,
        size=8.5,
        color=INK,
        max_lines=6,
    )
    _pill(c, "ENGINEERING_INPUT_ONLY_NOT_NET_PNL", MARGIN + 616, 303, fill=RED, color=white, size=6.5)

    c.setFont(FONT_HEAD, 12)
    c.setFillColor(INK)
    c.drawString(MARGIN, 244, "证据边界")
    rows = [
        ("日期", "2026-07-12 / 07-15 / 07-17", "L2 clean 工程 cohort"),
        ("Release 绑定", real["source_binding_sha256"][:18] + "…", "对象/DQ 已 pin；真实 record lineage 待生成"),
        ("真实审计", real["audit_sha256"][:18] + "…", "语义内容 SHA"),
        ("推断等级", real["deep03_claim_tier"], "禁止盈利/推广结论"),
    ]
    col_x = [MARGIN, MARGIN + 125, MARGIN + 430]
    top = 214
    for index, row in enumerate(rows):
        y = top - index * 34
        c.setStrokeColor(BORDER)
        c.line(MARGIN, y - 8, PAGE_W - MARGIN, y - 8)
        c.setFont(FONT_BODY, 8)
        c.setFillColor(SLATE)
        c.drawString(col_x[0], y, row[0])
        c.setFillColor(INK)
        c.setFont(FONT_MONO if index in {1, 2} else FONT_BODY, 8)
        c.drawString(col_x[1], y, row[1])
        c.setFont(FONT_BODY, 8)
        c.setFillColor(MUTED)
        c.drawString(col_x[2], y, row[2])
    c.showPage()

    # Page 4 — experiment scoreboard.
    _page(c, 4, "THREE FROZEN EXPERIMENTS")
    _title(
        c,
        "三个冻结实验的真实端到端状态",
        "冻结策略定义保持不变；缺字段时不做推断，缺 TRAIN 时不偷看验证集。",
    )
    card_gap = 14
    card_w = (PAGE_W - 2 * MARGIN - 2 * card_gap) / 3
    order = [
        "A01-SPREAD-CAPTURE",
        "A11-ONE-SIDED-PROVISION",
        "B09-LISTING-TO-START-DRIFT",
    ]
    colors = [BLUE, TEAL, PURPLE]
    for index, experiment_id in enumerate(order):
        row = experiments[experiment_id]
        x = MARGIN + index * (card_w + card_gap)
        _round_box(c, x, 70, card_w, 415, fill=white, stroke=BORDER)
        c.setFillColor(colors[index])
        c.roundRect(x, 440, card_w, 45, 9, fill=1, stroke=0)
        c.rect(x, 440, card_w, 12, fill=1, stroke=0)
        c.setFont(FONT_HEAD, 12)
        c.setFillColor(white)
        c.drawString(x + 15, 456, _experiment_name(experiment_id))
        _pill(c, "BLOCKED_SOURCE_FIELDS", x + 15, 409, fill=RED_LIGHT, color=RED, size=6.5)

        metric_rows = [
            ("候选 source rows", row["source_candidate_rows"]),
            ("候选 opportunities", row["source_candidate_opportunities"]),
            ("normalized rows", row["normalized_state_rows"]),
            ("strict fill rows*", row["strict_fill_rows"]),
            ("exact exit rows", row["exact_exit_rows"]),
            ("net PnL rows", row["net_pnl_rows"]),
        ]
        cursor = 382
        for label, value in metric_rows:
            c.setFont(FONT_BODY, 7.7)
            c.setFillColor(SLATE)
            c.drawString(x + 15, cursor, label)
            c.setFont(FONT_MONO_BOLD, 8.5)
            c.setFillColor(RED if label in {"normalized rows", "exact exit rows", "net PnL rows"} and value == 0 else INK)
            c.drawRightString(x + card_w - 15, cursor, f"{value:,}")
            c.setStrokeColor(BORDER)
            c.line(x + 15, cursor - 7, x + card_w - 15, cursor - 7)
            cursor -= 30

        c.setFont(FONT_HEAD, 9.5)
        c.setFillColor(INK)
        c.drawString(x + 15, 190, "最短真实阻塞")
        cursor = 169
        for blocker in _short_blockers(experiment_id):
            c.setFillColor(RED)
            c.circle(x + 19, cursor + 2, 2.8, fill=1, stroke=0)
            cursor = _text(
                c,
                blocker,
                x + 29,
                cursor + 5,
                card_w - 44,
                size=7.7,
                max_lines=2,
            ) - 6
        c.setFont(FONT_BODY, 6.7)
        c.setFillColor(MUTED)
        c.drawString(x + 15, 82, "* C1 工程切片，不等于该策略真实成交")
    c.showPage()

    # Page 5 — fees and latency.
    _page(c, 5, "COST & EXECUTION TRUTH")
    _title(
        c,
        "真实费用已经取到；真实执行延迟仍然没有",
        "实际历史 fee_cost 与策略重放费率是两类证据；HTTP/ping 也不是下单延迟。",
    )
    left_w = 365
    _round_box(c, MARGIN, 72, left_w, 410, fill=white)
    c.setFont(FONT_HEAD, 14)
    c.setFillColor(INK)
    c.drawString(MARGIN + 18, 452, "真实费用")
    c.setFont(FONT_HEAD, 27)
    c.setFillColor(AMBER)
    c.drawString(
        MARGIN + 18,
        408,
        f"${fee['total_actual_fee_cost_e6'] / 1_000_000:,.6f}",
    )
    c.setFont(FONT_BODY, 8)
    c.setFillColor(SLATE)
    c.drawString(MARGIN + 18, 389, "2026-07-10 至 07-23 的 372 笔历史实际 fee_cost")
    fee_metrics = [
        ("Taker fills", fee["taker_fill_count"]),
        ("Maker fills", fee["maker_fill_count"]),
        ("Nonzero fee fills", fee["nonzero_fee_fill_count"]),
        ("Zero fee fills", fee["zero_fee_fill_count"]),
    ]
    cursor = 350
    for label, value in fee_metrics:
        c.setFont(FONT_BODY, 8)
        c.setFillColor(SLATE)
        c.drawString(MARGIN + 18, cursor, label)
        c.setFont(FONT_MONO_BOLD, 9)
        c.setFillColor(INK)
        c.drawRightString(MARGIN + left_w - 18, cursor, f"{value:,}")
        cursor -= 28
    account_pass = account["state"] == "PASS"
    _round_box(
        c,
        MARGIN + 18,
        124,
        left_w - 36,
        112,
        fill=(GREEN_LIGHT if account_pass else AMBER_LIGHT),
        stroke=(GREEN if account_pass else AMBER),
    )
    _pill(
        c,
        account["state"],
        MARGIN + 31,
        203,
        fill=(GREEN if account_pass else AMBER),
        color=white,
    )
    _text(
        c,
        (
            "账户精度已绑定：13/13 个认证订单响应均含官方 direct-user "
            "子账户编号字段；按官方费用取整契约采用 $0.0001 目标精度。"
            if account_pass
            else (
                "账户余额 target precision 尚未有实际 posted balance delta 或官方账户类别字段。"
                "本金+fee 算术只能做诊断，不能自证 direct/non-direct。"
            )
        ),
        MARGIN + 31,
        184,
        left_w - 62,
        size=8,
        color=INK,
        max_lines=4,
    )

    right_x = MARGIN + left_w + 16
    right_w = PAGE_W - MARGIN - right_x
    _round_box(c, right_x, 72, right_w, 410, fill=white)
    c.setFont(FONT_HEAD, 14)
    c.setFillColor(INK)
    c.drawString(right_x + 18, 452, "真实执行延迟")
    _pill(c, latency["state"], right_x + 18, 410, fill=RED, color=white)
    paths = latency["path_summaries"]
    cursor = 350
    for row in paths:
        _round_box(c, right_x + 18, cursor - 10, right_w - 36, 65, fill=RED_LIGHT, stroke=RED_LIGHT)
        c.setFont(FONT_MONO_BOLD, 10)
        c.setFillColor(RED)
        c.drawString(right_x + 31, cursor + 27, row["path"])
        c.setFont(FONT_HEAD, 18)
        c.drawRightString(right_x + right_w - 31, cursor + 20, "0")
        c.setFont(FONT_BODY, 7.3)
        c.setFillColor(SLATE)
        c.drawString(right_x + 31, cursor + 7, "valid causal samples")
        cursor -= 78
    _text(
        c,
        "已审 2,782 个 ping、1,600 个 authenticated GET、1,000 个 W09 GET、13 个真实订单快照；全部因缺 decision→sent→ack→effective 同钟因果链而拒收。",
        right_x + 18,
        132,
        right_w - 36,
        size=8,
        color=SLATE,
        max_lines=3,
    )
    c.showPage()

    # Page 6 — adversarial audit.
    _page(c, 6, "ADVERSARIAL SAFETY AUDIT")
    _title(
        c,
        "审计不是看测试绿，而是主动制造“假完成”",
        "任何一条攻击仍能产出 NET_PNL_COMPLETE，就禁止真实结果发布。",
    )
    _round_box(c, MARGIN, 418, PAGE_W - 2 * MARGIN, 62, fill=(GREEN_LIGHT if audit_status.startswith("PASS") else RED_LIGHT), stroke=(GREEN if audit_status.startswith("PASS") else RED))
    _pill(
        c,
        audit_status,
        MARGIN + 16,
        438,
        fill=(GREEN if audit_status.startswith("PASS") else RED),
        color=white,
        h=21,
        size=8,
    )
    _text(
        c,
        f"Audited runtime commit: {audited_runtime_commit}",
        MARGIN + 230,
        450,
        PAGE_W - 2 * MARGIN - 246,
        font=FONT_MONO,
        size=8,
        color=INK,
        max_lines=1,
    )
    attacks = [
        ("01", "零费自签", "把 maker/taker 费率改成 0", "外部 fee_facts + 实付 fee 绑定"),
        ("02", "日期脱钩", "旧行冒充新 release", "record / object / UTC date 同域"),
        ("03", "血缘自签", "改 L2 后重签普通 authority", "root-pinned extractor lineage"),
        ("04", "日损绕过", "先巨亏、后盈利再解锁", "UTC 日 breach latch"),
        ("05", "挑旧好簿", "忽略更新、更差的有效 L2", "latest eligible snapshot"),
        ("06", "伪终局", "VOID/双 authority 各给有利 payout", "仅 FINALIZED + market-global 唯一"),
        ("07", "延后认亏", "partial exit 亏损拖到次日结算", "FIFO lot PnL 按真实退出时点入账"),
    ]
    y = 375
    for number, name, exploit, fix in attacks:
        _round_box(c, MARGIN, y - 7, PAGE_W - 2 * MARGIN, 42, fill=white, stroke=BORDER)
        _pill(c, number, MARGIN + 12, y + 7, fill=NAVY, color=white, h=17)
        c.setFont(FONT_HEAD, 9)
        c.setFillColor(INK)
        c.drawString(MARGIN + 56, y + 12, name)
        _text(c, exploit, MARGIN + 150, y + 13, 250, size=7.5, color=SLATE, max_lines=1)
        _text(c, "→ " + fix, MARGIN + 430, y + 13, PAGE_W - MARGIN - (MARGIN + 444), size=7.5, color=GREEN, max_lines=1)
        y -= 47
    c.setFont(FONT_BODY, 7.2)
    c.setFillColor(MUTED)
    c.drawString(
        MARGIN,
        50,
        "审计状态来自独立对抗报告；若为 FAIL，本 PDF 只可作为缺口报告，不可作为 PnL 发布许可。",
    )
    c.showPage()

    # Page 7 — shortest execution path and hashes.
    _page(c, 7, "MINIMUM PATH TO A REAL NUMBER")
    _title(
        c,
        "下一步只做能让第一条真实 PnL 行出现的工作",
        "优先顺序由“离第一条可审计现金 PnL 行的距离”决定，不按代码数量决定。",
    )
    p0_done = audit_status.startswith("PASS")
    steps = [
        (
            "P0",
            (
                "已完成 runner P0 对抗复审"
                if p0_done
                else "完成 runner P0 对抗复审"
            ),
            "费用、血缘、日损、L2、终局、分段实现等攻击全部转为 fail-closed。",
            GREEN if p0_done else RED,
        ),
        ("P1", "生成真实 lineage receipt", "只读 extractor 实际校验 exact objects，并为四类 runtime records 生成穷尽 derivation 收据。", BLUE),
        ("P2", "补真实三路径延迟", "执行主机写 decision/sent/ack/effective 同钟 trace；PLACE/CANCEL/IOC_EXIT 各有真实样本。", AMBER),
        ("P3", "先跑 A01 首条现金 PnL", "物化 spread-dwell/warmup/TRAIN gates，绑定费用与完整 IOC/终局；否则保持 BLOCKED。", TEAL),
        ("P4", "依次开放 A11 / B09", "A11 先补 post-decision lifecycle；B09 先做数据盲 TRAIN/validation 分配与封印。", PURPLE),
    ]
    cursor = 428
    for priority, name, acceptance, color in steps:
        _round_box(c, MARGIN, cursor - 9, PAGE_W - 2 * MARGIN, 59, fill=white, stroke=BORDER)
        _pill(c, priority, MARGIN + 13, cursor + 16, fill=color, color=white, h=19)
        c.setFont(FONT_HEAD, 10)
        c.setFillColor(INK)
        c.drawString(MARGIN + 66, cursor + 23, name)
        _text(c, acceptance, MARGIN + 250, cursor + 24, PAGE_W - MARGIN - (MARGIN + 266), size=8, color=SLATE, max_lines=2)
        cursor -= 68

    c.setFont(FONT_HEAD, 10)
    c.setFillColor(INK)
    c.drawString(MARGIN, 91, "关键收据 SHA-256")
    hashes = [
        ("Real data file", _sha(REAL_DATA)),
        ("Fee aggregate file", _sha(FEE_AGGREGATE)),
        ("Account precision file", _sha(ACCOUNT_PRECISION)),
        ("Latency blocked file", _sha(LATENCY)),
    ]
    x = MARGIN
    for label, digest in hashes:
        c.setFont(FONT_BODY, 6.7)
        c.setFillColor(MUTED)
        c.drawString(x, 72, label)
        c.setFont(FONT_MONO, 6)
        c.setFillColor(INK)
        c.drawString(x, 59, digest[:20] + "…")
        x += 190
    c.save()


def _current_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--audit-status",
        default="PENDING_REPAIR_REAUDIT",
    )
    parser.add_argument("--audited-runtime-commit", default="")
    args = parser.parse_args()
    render(
        Path(args.output),
        audit_status=args.audit_status,
        audited_runtime_commit=(
            args.audited_runtime_commit or _current_commit()
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
