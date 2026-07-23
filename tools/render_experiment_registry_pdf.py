#!/usr/bin/env python3
"""Render the Deep03 experiment registry as a visual Chinese PDF.

This is an offline reporting tool.  It reads the immutable registry JSON and
writes a management-facing PDF.  It performs no AWS, network, venue, account,
deployment, or trading operation.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = (
    ROOT / "Deepresearch V3" / "registry" / "EXPERIMENT_REGISTRY_V1.json"
)
OUTPUT_PATH = (
    ROOT / "output" / "pdf" / "DEEP03_EXPERIMENT_FACTORY_TO_REAL_PNL_V1_CN.pdf"
)

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 34
DETAIL_PAGE_W, DETAIL_PAGE_H = landscape(A3)
SUMMARY_PAGES = 15
TOTAL_PAGES = 0

FONT_BODY = "ArialUnicode"
FONT_HEAD = "STHeiti"
FONT_MONO = "Courier"
FONT_MONO_BOLD = "Courier-Bold"

NAVY = HexColor("#0B132B")
INK = HexColor("#17213A")
SLATE = HexColor("#42526E")
MUTED = HexColor("#718096")
LIGHT = HexColor("#F4F7FB")
PAPER = HexColor("#FBFCFE")
BORDER = HexColor("#DCE4EF")
BLUE = HexColor("#3478F6")
TEAL = HexColor("#16A394")
CORAL = HexColor("#F36B5D")
AMBER = HexColor("#ECA83E")
RED = HexColor("#D84B4B")
PURPLE = HexColor("#7657D5")
GREEN = HexColor("#2F9E74")
SKY = HexColor("#DCEBFF")
TEAL_LIGHT = HexColor("#DDF5F1")
AMBER_LIGHT = HexColor("#FFF0D2")
RED_LIGHT = HexColor("#FCE5E5")
PURPLE_LIGHT = HexColor("#ECE8FB")

FAMILY_LABELS = {
    "A": "微观结构与做市",
    "B": "方向与波动",
    "C": "相对价值",
    "D": "RFQ 与 MVE",
    "E": "结算与生命周期",
    "F": "体育生态与外部数据",
    "G": "模型方法",
    "P": "组合与运营",
}

FAMILY_COLORS = {
    "A": BLUE,
    "B": TEAL,
    "C": PURPLE,
    "D": AMBER,
    "E": CORAL,
    "F": RED,
    "G": HexColor("#5375A6"),
    "P": HexColor("#5C6475"),
}

TARGET_LABELS = {
    "EXECUTABLE_STRATEGY": "动作型实验",
    "SIGNAL": "信号实验",
    "RESEARCH_QUESTION": "研究问题",
}

ACTION_LABELS = {
    "NO_ACTION": "无动作",
    "MODEL_COMPONENT": "模型组件",
    "CONTROL_ONLY": "控制实验",
    "MAKER_SPREAD": "双边做市",
    "MAKER_FILTER": "做市风控过滤",
    "MAKER_REFILL": "补单/回补",
    "T90_RANGE": "T90 区间做市",
    "T90_REBUILD": "T90 重建",
    "PASSIVE_FADE": "被动反转",
    "TAKER_DIRECTIONAL": "主动方向单",
    "RELATIVE_VALUE": "多腿相对价值",
    "EXPIRY_ROLL": "到期迁移",
    "CHILD_HEDGE": "子策略对冲",
    "RFQ_GUARD": "RFQ CLOB 风控",
    "RFQ_MAKER": "RFQ 直接报价",
    "MVE_DIRECT": "MVE 直接执行",
    "TERMINAL_POLICY": "持仓终局策略",
    "TERMINAL_ENTRY": "终局开仓",
    "CS_ASK": "CS-ASK",
    "EARLY_EXIT": "提前退出",
    "EXTERNAL_ACTION": "外部数据动作",
    "ROUTER": "策略路由",
    "SIZE_RISK": "仓位与风险",
    "COMMERCIAL_ROC": "商业 ROC",
}

DATA_LABELS = {
    "L1": "L1/成交",
    "L2": "L2",
    "L1_L2": "L1+L2",
    "L1_PRIVATE": "L1+私有仓位",
    "FAMILY": "市场图谱",
    "TERMINAL": "结算/生命周期",
    "RFQ_PUBLIC": "公开 RFQ",
    "RFQ_PRIVATE": "私有 RFQ",
    "MVE_PRIVATE": "私有 MVE",
    "EXTERNAL": "外部体育",
    "CATALOG": "目录/规则",
    "MODEL": "模型 owner",
    "META": "组合/路由",
    "OWNER_EVPI": "Owner EVPI",
    "PRIVATE": "私有账户",
    "OUT_OF_SCOPE": "范围外",
    "INTERNAL_CONTROL": "内部控制",
}

BLOCKER_LABELS = {
    "SETTLEMENT_MISSING": "结算与异常状态",
    "FEE_UNKNOWN": "精确费用",
    "FILL_MODEL_MISSING": "严格成交模型",
    "LATENCY_UNMEASURED": "实测延迟",
    "RISK_GATES_MISSING": "风险门",
    "INSUFFICIENT_QUALITY_DATES": "独立干净日期",
    "CONFIRMED_CHILD_STRATEGIES_MISSING": "已确认 child",
    "OWN_ORDER_CALIBRATION_MISSING": "自有订单校准",
    "VENUE_PERMISSION_MISSING": "执行权限",
    "INSTRUMENT_MAPPING_UNKNOWN": "合约映射",
    "MULTI_LEG_ENGINE_MISSING": "多腿引擎",
    "PAYOUT_EXHAUSTIVENESS_MISSING": "支付状态穷尽",
    "ACTION_INCOMPLETE": "动作未闭合",
    "RFQ_PUBLIC_DATA_UNRELEASED": "RFQ 新鲜发布",
    "EXTERNAL_API_UNVERIFIED": "外部 API 未验证",
    "EXTERNAL_SPORTS_DATA_MISSING": "外部体育数据",
    "TIMESTAMP_SEMANTICS_UNKNOWN": "外部时钟语义",
    "PRIVATE_ACCOUNT_STATE_MISSING": "私有账户状态",
    "MISSING_SIGNAL_INPUT": "信号输入",
    "SAMPLE_TOO_SMALL": "样本不足",
    "RFQ_PRIVATE_EVENTS_MISSING": "RFQ 私有事件",
    "OUT_OF_SCOPE_CURRENT_PROGRAM": "当前范围外",
    "MVE_PRIVATE_EVENTS_MISSING": "MVE 私有事件",
    "OPERATING_COST_UNKNOWN": "运营成本",
    "BASELINE_INCOMPLETE": "基准未闭合",
}


def _register_fonts() -> None:
    body = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    head = Path("/System/Library/Fonts/STHeiti Medium.ttc")
    if not body.exists() or not head.exists():
        raise FileNotFoundError("Required Chinese fonts are unavailable")
    pdfmetrics.registerFont(TTFont(FONT_BODY, str(body)))
    pdfmetrics.registerFont(TTFont(FONT_HEAD, str(head)))


def _load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _split_lines(
    text: str,
    max_width: float,
    font: str,
    size: float,
) -> list[str]:
    """Width-aware wrapping that works for mixed Chinese and ASCII text."""

    if text is None:
        return []
    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and pdfmetrics.stringWidth(candidate, font, size) > max_width:
                lines.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        lines.append(current.rstrip())
    return lines


def _draw_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    *,
    font: str = FONT_BODY,
    size: float = 10,
    color: Color = INK,
    leading: float | None = None,
    max_lines: int | None = None,
) -> float:
    leading = leading or size * 1.35
    lines = _split_lines(text, max_width, font, size)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = lines[-1]
        while tail and pdfmetrics.stringWidth(tail + "...", font, size) > max_width:
            tail = tail[:-1]
        lines[-1] = tail + "..."
    c.setFont(font, size)
    c.setFillColor(color)
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def _draw_centered(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    *,
    font: str = FONT_BODY,
    size: float = 10,
    color: Color = INK,
) -> None:
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(x, y, text)


def _round_rect(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: Color = white,
    stroke: Color = BORDER,
    radius: float = 10,
    line_width: float = 0.7,
) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(line_width)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)


def _badge(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    *,
    fill: Color,
    color: Color,
    width: float | None = None,
) -> float:
    width = width or max(52, pdfmetrics.stringWidth(text, FONT_HEAD, 8) + 18)
    c.setFillColor(fill)
    c.roundRect(x, y, width, 20, 9, fill=1, stroke=0)
    _draw_centered(c, text, x + width / 2, y + 6.2, font=FONT_HEAD, size=8, color=color)
    return width


def _metric_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    value: str,
    label: str,
    note: str,
    accent: Color,
) -> None:
    _round_rect(c, x, y, w, h, fill=white, stroke=BORDER, radius=12)
    c.setFillColor(accent)
    c.roundRect(x, y, 6, h, 3, fill=1, stroke=0)
    c.setFillColor(accent)
    c.setFont(FONT_HEAD, 28)
    c.drawString(x + 20, y + h - 40, value)
    c.setFillColor(INK)
    c.setFont(FONT_HEAD, 11)
    c.drawString(x + 20, y + h - 62, label)
    _draw_text(c, note, x + 20, y + h - 82, w - 34, size=7.8, color=MUTED, leading=10, max_lines=2)


def _page_header(
    c: canvas.Canvas,
    page_no: int,
    title: str,
    subtitle: str,
    *,
    section: str,
    accent: Color = BLUE,
) -> float:
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(accent)
    c.roundRect(MARGIN, PAGE_H - 38, 92, 18, 8, fill=1, stroke=0)
    _draw_centered(c, section, MARGIN + 46, PAGE_H - 32.5, font=FONT_HEAD, size=7.5, color=white)
    c.setFillColor(INK)
    c.setFont(FONT_HEAD, 23)
    c.drawString(MARGIN, PAGE_H - 76, title)
    _draw_text(c, subtitle, MARGIN, PAGE_H - 98, PAGE_W - 2 * MARGIN, size=10, color=SLATE, leading=13, max_lines=2)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.8)
    c.line(MARGIN, PAGE_H - 112, PAGE_W - MARGIN, PAGE_H - 112)
    c.bookmarkPage(f"page-{page_no}")
    c.addOutlineEntry(title, f"page-{page_no}", level=0, closed=False)
    return PAGE_H - 132


def _footer(c: canvas.Canvas, page_no: int) -> None:
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(MARGIN, 27, PAGE_W - MARGIN, 27)
    c.setFont(FONT_BODY, 6.8)
    c.setFillColor(MUTED)
    c.drawString(
        MARGIN,
        14,
        "Deep03 Registry V1 | Snapshot 2026-07-23 | Research directory only - no trading authority",
    )
    c.drawRightString(PAGE_W - MARGIN, 14, f"{page_no} / {TOTAL_PAGES}")


def _finish_page(c: canvas.Canvas, page_no: int) -> None:
    _footer(c, page_no)
    c.showPage()


def _hbar_chart(
    c: canvas.Canvas,
    items: list[tuple[str, int]],
    x: float,
    y: float,
    w: float,
    *,
    row_h: float = 25,
    color: Color = BLUE,
    label_w: float = 150,
    max_value: int | None = None,
    value_suffix: str = "",
) -> None:
    max_value = max_value or max(v for _, v in items)
    bar_w = w - label_w - 38
    for index, (label, value) in enumerate(items):
        yy = y - index * row_h
        c.setFont(FONT_BODY, 8)
        c.setFillColor(SLATE)
        c.drawRightString(x + label_w - 8, yy + 4, label)
        c.setFillColor(BORDER)
        c.roundRect(x + label_w, yy, bar_w, 11, 5, fill=1, stroke=0)
        c.setFillColor(color)
        c.roundRect(
            x + label_w,
            yy,
            max(2, bar_w * value / max_value),
            11,
            5,
            fill=1,
            stroke=0,
        )
        c.setFillColor(INK)
        c.setFont(FONT_MONO_BOLD, 8)
        c.drawRightString(x + w, yy + 2.5, f"{value}{value_suffix}")


def _donut(
    c: canvas.Canvas,
    cx: float,
    cy: float,
    radius: float,
    values: list[int],
    colors: list[Color],
    *,
    center_top: str,
    center_bottom: str,
) -> None:
    total = sum(values)
    start = 90
    for value, color in zip(values, colors):
        extent = -360 * value / total
        c.setFillColor(color)
        c.wedge(
            cx - radius,
            cy - radius,
            cx + radius,
            cy + radius,
            start,
            extent,
            fill=1,
            stroke=0,
        )
        start += extent
    c.setFillColor(white)
    c.circle(cx, cy, radius * 0.58, fill=1, stroke=0)
    _draw_centered(c, center_top, cx, cy + 2, font=FONT_HEAD, size=19, color=INK)
    _draw_centered(c, center_bottom, cx, cy - 17, font=FONT_BODY, size=7.5, color=MUTED)


def _arrow(
    c: canvas.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: Color = MUTED,
    width: float = 1.2,
) -> None:
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(width)
    c.line(x1, y1, x2, y2)
    angle = 5
    c.line(x2, y2, x2 - angle, y2 + angle / 2)
    c.line(x2, y2, x2 - angle, y2 - angle / 2)


def _short_blockers(card: dict[str, Any], n: int = 3) -> str:
    blockers = card["blockers"][:n]
    labels = [BLOCKER_LABELS.get(blocker, blocker) for blocker in blockers]
    remaining = len(card["blockers"]) - len(blockers)
    return " / ".join(labels) + (f" +{remaining}" if remaining > 0 else "")


def _card_by_id(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        card["identity"]["experiment_id"]: card
        for card in registry["experiments"]
    }


def _page_cover(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    c.setFillColor(NAVY)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(BLUE)
    c.circle(PAGE_W - 65, PAGE_H - 30, 150, fill=1, stroke=0)
    c.setFillColor(TEAL)
    c.circle(PAGE_W - 10, 70, 115, fill=1, stroke=0)
    c.setFillColor(NAVY)
    c.circle(PAGE_W - 35, 84, 73, fill=1, stroke=0)

    _badge(c, "REGISTRY V1", MARGIN, PAGE_H - 68, fill=Color(1, 1, 1, alpha=0.16), color=white, width=92)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 36)
    c.drawString(MARGIN, PAGE_H - 135, "从“一团浆糊”到实验工厂")
    c.setFont(FONT_HEAD, 22)
    c.setFillColor(HexColor("#CFE1FF"))
    c.drawString(MARGIN, PAGE_H - 172, "DeepResearch V3 统一实验变现 Registry")
    _draw_text(
        c,
        "把 106 个研究方向统一成“假设 - 信号 - 动作 - 场所 - 基准 - 成本 - PnL - 数据缺口”的可审计操作系统。",
        MARGIN,
        PAGE_H - 210,
        555,
        size=12,
        color=white,
        leading=18,
        max_lines=2,
    )

    labels = [
        ("假设", BLUE),
        ("信号", TEAL),
        ("动作", AMBER),
        ("成交", PURPLE),
        ("成本", CORAL),
        ("PnL", RED),
    ]
    x = MARGIN
    y = 230
    box_w = 88
    for index, (label, color) in enumerate(labels):
        c.setFillColor(Color(color.red, color.green, color.blue, alpha=0.20))
        c.setStrokeColor(color)
        c.roundRect(x, y, box_w, 54, 10, fill=1, stroke=1)
        _draw_centered(c, label, x + box_w / 2, y + 20, font=FONT_HEAD, size=12, color=white)
        if index < len(labels) - 1:
            _arrow(c, x + box_w + 4, y + 27, x + box_w + 22, y + 27, color=white)
        x += 108

    metrics = [
        ("106", "全部实验"),
        ("80", "动作型目标"),
        ("0", "当前实盘合格"),
    ]
    x = MARGIN
    for value, label in metrics:
        c.setFillColor(Color(1, 1, 1, alpha=0.08))
        c.roundRect(x, 82, 150, 90, 13, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont(FONT_HEAD, 27)
        c.drawString(x + 18, 126, value)
        c.setFont(FONT_BODY, 9)
        c.setFillColor(HexColor("#C9D7EE"))
        c.drawString(x + 18, 104, label)
        x += 166

    c.setFillColor(white)
    c.setFont(FONT_HEAD, 9)
    c.drawRightString(PAGE_W - MARGIN, 43, "独立审计 PASS | P0=0 | P1=0")
    c.setFont(FONT_MONO, 6.5)
    c.setFillColor(HexColor("#C9D7EE"))
    c.drawRightString(
        PAGE_W - MARGIN,
        28,
        f"registry_sha256 {registry['registry_sha256']}",
    )
    c.setFont(FONT_BODY, 6.8)
    c.setFillColor(HexColor("#C9D7EE"))
    c.drawRightString(PAGE_W - MARGIN, 14, f"{page_no} / {TOTAL_PAGES}")
    c.showPage()


def _page_dashboard(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "答案先说：目录完成，策略尚未完成",
        "80 张卡已经写出动作与费用后 PnL 合约，但没有一张通过当前全部证据门。",
        section="管理摘要",
        accent=BLUE,
    )
    metrics = [
        ("106", "全部实验", "8 个家族完整覆盖", BLUE),
        ("80", "动作型目标", "目标类别，不等于已验证策略", TEAL),
        ("0", "当前合格策略", "未知 PnL 绝不当成 0", RED),
        ("105 + 1", "当前分类", "105 BLOCKED / 1 研究问题", AMBER),
    ]
    gap = 12
    card_w = (PAGE_W - 2 * MARGIN - gap * 3) / 4
    for index, item in enumerate(metrics):
        _metric_card(c, MARGIN + index * (card_w + gap), y - 104, card_w, 92, *item)

    left_x = MARGIN
    left_y = 70
    left_w = 470
    left_h = 260
    _round_rect(c, left_x, left_y, left_w, left_h, fill=white)
    c.setFont(FONT_HEAD, 12)
    c.setFillColor(INK)
    c.drawString(left_x + 18, left_y + left_h - 28, "目标类别 vs 当前事实")
    target = registry["summary"]["target_class_counts"]
    rows = [
        ("动作型目标", target["EXECUTABLE_STRATEGY"], TEAL),
        ("信号目标", target["SIGNAL"], BLUE),
        ("研究问题", target["RESEARCH_QUESTION"], PURPLE),
    ]
    bar_x = left_x + 28
    bar_y = left_y + left_h - 75
    max_w = 205
    for idx, (label, value, color) in enumerate(rows):
        yy = bar_y - idx * 43
        c.setFillColor(color)
        c.roundRect(bar_x, yy, max_w * value / 80, 17, 7, fill=1, stroke=0)
        c.setFont(FONT_BODY, 8.5)
        c.setFillColor(SLATE)
        c.drawString(bar_x, yy - 14, label)
        c.setFont(FONT_MONO_BOLD, 9)
        c.setFillColor(INK)
        c.drawRightString(bar_x + max_w + 28, yy + 4, str(value))
    c.setStrokeColor(BORDER)
    c.line(left_x + 280, left_y + 30, left_x + 280, left_y + left_h - 45)
    _donut(
        c,
        left_x + 370,
        left_y + 145,
        68,
        [105, 1],
        [RED, PURPLE],
        center_top="0",
        center_bottom="当前可实盘",
    )
    _badge(c, "BLOCKED 105", left_x + 310, left_y + 42, fill=RED_LIGHT, color=RED, width=92)
    _badge(c, "研究问题 1", left_x + 310, left_y + 16, fill=PURPLE_LIGHT, color=PURPLE, width=92)

    right_x = left_x + left_w + 14
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, left_y, right_w, left_h, fill=NAVY, stroke=NAVY)
    c.setFont(FONT_HEAD, 12)
    c.setFillColor(white)
    c.drawString(right_x + 18, left_y + left_h - 28, "管理者必须记住的三句话")
    guardrails = [
        ("01", "106 项实验 ≠ 106 个策略。"),
        ("02", "BLOCKED ≠ 假设失败，也 ≠ PnL 为 0。"),
        ("03", "没有动作、场所、基准和费用后 PnL，就不算策略。"),
    ]
    yy = left_y + left_h - 70
    for number, text in guardrails:
        c.setFillColor(BLUE)
        c.circle(right_x + 34, yy + 4, 14, fill=1, stroke=0)
        _draw_centered(c, number, right_x + 34, yy, font=FONT_MONO_BOLD, size=7, color=white)
        _draw_text(c, text, right_x + 58, yy + 9, right_w - 78, size=10, color=white, leading=14, max_lines=2)
        yy -= 55
    _draw_text(
        c,
        "下一步不是继续堆想法，而是先关闭共享执行门，再让少数卡进入 Shadow。",
        right_x + 20,
        left_y + 42,
        right_w - 40,
        font=FONT_HEAD,
        size=10,
        color=HexColor("#CFE1FF"),
        leading=14,
        max_lines=2,
    )
    _finish_page(c, page_no)


def _page_operating_change(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "运营方式发生了什么改变",
        "从“解释市场”改成“用现金闭环证伪动作”；任一硬门失败都回退或关闭。",
        section="操作系统",
        accent=TEAL,
    )
    box_y = 250
    box_h = 220
    col_w = (PAGE_W - 2 * MARGIN - 22) / 2
    _round_rect(c, MARGIN, box_y, col_w, box_h, fill=RED_LIGHT, stroke=HexColor("#F5CACA"))
    _badge(c, "以前", MARGIN + 18, box_y + box_h - 37, fill=RED, color=white, width=58)
    old = [
        "看到盘口形态，讲一个可能的故事",
        "用 markout、mid return 或毛利代替现金收益",
        "模型、RFQ、体育状态与执行权限混在一起",
        "缺数据时用价格或时间代理补全",
        "失败标准、基准和成本经常后补",
    ]
    yy = box_y + box_h - 75
    for item in old:
        c.setFillColor(RED)
        c.circle(MARGIN + 28, yy + 2, 3.2, fill=1, stroke=0)
        yy = _draw_text(c, item, MARGIN + 42, yy + 7, col_w - 62, size=9.2, color=INK, leading=13, max_lines=2) - 12

    right_x = MARGIN + col_w + 22
    _round_rect(c, right_x, box_y, col_w, box_h, fill=TEAL_LIGHT, stroke=HexColor("#B7E6DE"))
    _badge(c, "现在", right_x + 18, box_y + box_h - 37, fill=TEAL, color=white, width=58)
    new = [
        "先写可证伪假设和点时可得信号",
        "明确看到信号后在哪、以什么价格和数量行动",
        "冻结可执行基准与逐路径费用后增量 PnL",
        "每个未知数据和成本直接变成 BLOCKER",
        "Registry 不授予 AWS、下单或资金权限",
    ]
    yy = box_y + box_h - 75
    for item in new:
        c.setFillColor(TEAL)
        c.circle(right_x + 28, yy + 2, 3.2, fill=1, stroke=0)
        yy = _draw_text(c, item, right_x + 42, yy + 7, col_w - 62, size=9.2, color=INK, leading=13, max_lines=2) - 12

    stages = [
        ("假设", "经济机制"),
        ("信号", "过去时钟"),
        ("动作", "边/价/量/退出"),
        ("场所", "真实接口"),
        ("基准", "同机会集"),
        ("成本", "不准填 0"),
        ("PnL", "现金路径"),
        ("证伪", "失败即停"),
    ]
    start_x = MARGIN
    stage_y = 112
    stage_w = (PAGE_W - 2 * MARGIN - 7 * 8) / 8
    for index, (title, sub) in enumerate(stages):
        color = [BLUE, TEAL, AMBER, PURPLE, BLUE, CORAL, RED, INK][index]
        _round_rect(c, start_x, stage_y, stage_w, 76, fill=white, stroke=color, radius=9, line_width=1.2)
        _draw_centered(c, title, start_x + stage_w / 2, stage_y + 44, font=FONT_HEAD, size=10, color=color)
        _draw_centered(c, sub, start_x + stage_w / 2, stage_y + 22, size=6.8, color=MUTED)
        if index < 7:
            _arrow(c, start_x + stage_w + 1, stage_y + 38, start_x + stage_w + 7, stage_y + 38, color=MUTED)
        start_x += stage_w + 8
    _draw_text(
        c,
        "STOP 规则：缺任何一格，computed_class 只能是 BLOCKED、SIGNAL 或 RESEARCH_QUESTION。",
        MARGIN,
        75,
        PAGE_W - 2 * MARGIN,
        font=FONT_HEAD,
        size=9.5,
        color=RED,
        max_lines=1,
    )
    _finish_page(c, page_no)


def _page_card_anatomy(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    by_id = _card_by_id(registry)
    card = by_id["A01-SPREAD-CAPTURE"]
    y = _page_header(
        c,
        page_no,
        "一张合格实验卡长什么样",
        "A01 已经有清楚动作与 PnL 定义，但描述性 markout 仍不能证明盈利。",
        section="卡片解剖",
        accent=AMBER,
    )
    card_x = MARGIN
    card_y = 62
    card_w = 520
    card_h = y - card_y
    _round_rect(c, card_x, card_y, card_w, card_h, fill=white, stroke=BORDER, radius=14)
    _badge(c, "A01", card_x + 18, card_y + card_h - 42, fill=BLUE, color=white, width=48)
    _badge(c, "BLOCKED", card_x + 76, card_y + card_h - 42, fill=RED_LIGHT, color=RED, width=72)
    _badge(c, "NO PNL CLAIM", card_x + 158, card_y + card_h - 42, fill=AMBER_LIGHT, color=HexColor("#9C6507"), width=100)
    _badge(c, "NO TRADING AUTHORITY", card_x + 268, card_y + card_h - 42, fill=PURPLE_LIGHT, color=PURPLE, width=140)

    fields = [
        ("假设", "在合格双边盘口中，被动赚取点差在严格成交、逆向选择、强制退出和精确费用后仍为正。"),
        ("信号", "可执行点差超过预先冻结的费用、markout、退出和安全缓冲。"),
        ("动作", "在允许的两边各挂 1 张 post-only；信号消失、盘口断档或数据过期时立即撤单并对账。"),
        ("场所", "Kalshi CLOB。Registry 本身不授予下单权限。"),
        ("基准", "相同机会集保持空仓，现金流记 0；所有零触发和零成交机会保留。"),
        ("PnL", "策略逐路径现金 PnL - 相同机会不交易基准；扣除费用、滑点、延迟、库存和资本成本。"),
        ("失败", "untouched 样本的费用后增量 NetPnL <= 0，或容量/尾部风险未通过冻结门。"),
    ]
    yy = card_y + card_h - 72
    label_w = 56
    for label, value in fields:
        c.setFillColor(LIGHT)
        c.roundRect(card_x + 18, yy - 17, label_w, 22, 7, fill=1, stroke=0)
        _draw_centered(c, label, card_x + 18 + label_w / 2, yy - 10, font=FONT_HEAD, size=8, color=SLATE)
        new_y = _draw_text(
            c,
            str(value),
            card_x + 88,
            yy,
            card_w - 108,
            size=7.6,
            color=INK,
            leading=10,
            max_lines=2,
        )
        yy = min(yy - 34, new_y - 9)

    side_x = card_x + card_w + 16
    side_w = PAGE_W - MARGIN - side_x
    _round_rect(c, side_x, 305, side_w, 165, fill=NAVY, stroke=NAVY, radius=12)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 11)
    c.drawString(side_x + 18, 442, "当前真实观测")
    obs = card["current_result"]["observed_signal"]["values"]
    markout = obs["pooled_signed_markout_logodds"]
    yy = 407
    for horizon in ("1s", "5s", "30s"):
        value = markout[horizon]
        c.setFont(FONT_MONO_BOLD, 11)
        c.setFillColor(HexColor("#CFE1FF"))
        c.drawString(side_x + 18, yy, horizon)
        c.setFillColor(white)
        c.drawRightString(side_x + side_w - 20, yy, f"{value:.3f}")
        yy -= 28
    _draw_text(
        c,
        "仅表示 pooled half-spread 与 signed markout 的描述关系；尚未扣费用、模拟成交、延迟和退出。",
        side_x + 18,
        326,
        side_w - 36,
        size=7.6,
        color=HexColor("#C9D7EE"),
        leading=10,
        max_lines=3,
    )

    _round_rect(c, side_x, 62, side_w, 226, fill=RED_LIGHT, stroke=HexColor("#F2C5C5"), radius=12)
    c.setFillColor(RED)
    c.setFont(FONT_HEAD, 11)
    c.drawString(side_x + 18, 260, "为什么还不能下单")
    yy = 230
    for blocker in card["blockers"]:
        c.setFillColor(RED)
        c.circle(side_x + 24, yy + 2, 2.8, fill=1, stroke=0)
        yy = _draw_text(
            c,
            BLOCKER_LABELS.get(blocker, blocker),
            side_x + 36,
            yy + 6,
            side_w - 55,
            size=7.8,
            color=INK,
            leading=10,
            max_lines=1,
        ) - 8
    _finish_page(c, page_no)


def _page_families(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "广度已经铺开：8 个实验家族",
        "覆盖面是研究目录的广度，不是价值排名，也不是 106 次独立下注。",
        section="实验版图",
        accent=PURPLE,
    )
    cards = registry["experiments"]
    family_counts = Counter(card["identity"]["family_code"] for card in cards)
    action_counts = Counter(
        card["identity"]["family_code"]
        for card in cards
        if card["classification"]["target_class"] == "EXECUTABLE_STRATEGY"
    )
    items = [(family, family_counts[family]) for family in "ABCFGPDE"]
    left_x = MARGIN
    _round_rect(c, left_x, 70, 390, y - 75, fill=white)
    c.setFont(FONT_HEAD, 11)
    c.setFillColor(INK)
    c.drawString(left_x + 18, y - 28, "家族规模")
    _hbar_chart(
        c,
        [(f"{f}  {FAMILY_LABELS[f]}", value) for f, value in items],
        left_x + 15,
        y - 63,
        360,
        row_h=37,
        color=PURPLE,
        label_w=145,
        max_value=23,
    )

    right_x = left_x + 404
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, 70, right_w, y - 75, fill=white)
    c.setFont(FONT_HEAD, 11)
    c.setFillColor(INK)
    c.drawString(right_x + 18, y - 28, "动作型目标 / 非策略条目")
    bar_x = right_x + 120
    bar_w = right_w - 170
    yy = y - 64
    for family in "ABCDEFGP":
        total = family_counts[family]
        action = action_counts[family]
        non = total - action
        c.setFillColor(SLATE)
        c.setFont(FONT_BODY, 8)
        c.drawRightString(bar_x - 12, yy + 2, f"{family} {FAMILY_LABELS[family]}")
        c.setFillColor(TEAL)
        c.rect(bar_x, yy, bar_w * action / 23, 12, fill=1, stroke=0)
        c.setFillColor(PURPLE_LIGHT)
        c.rect(bar_x + bar_w * action / 23, yy, bar_w * non / 23, 12, fill=1, stroke=0)
        c.setFont(FONT_MONO_BOLD, 7.5)
        c.setFillColor(INK)
        c.drawString(bar_x + bar_w + 8, yy + 1.5, f"{action}/{non}")
        yy -= 37
    _badge(c, "动作型目标 80", right_x + 18, 90, fill=TEAL_LIGHT, color=TEAL, width=102)
    _badge(c, "信号/研究 26", right_x + 130, 90, fill=PURPLE_LIGHT, color=PURPLE, width=102)
    _finish_page(c, page_no)


def _page_target_vs_current(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "“想做什么”与“现在能做什么”必须分开",
        "target_class 记录目标；computed_class 只认当前已闭合证据。",
        section="成熟度",
        accent=BLUE,
    )
    left_x = MARGIN
    mid_x = PAGE_W / 2
    _round_rect(c, left_x, 205, 300, 260, fill=white)
    _round_rect(c, PAGE_W - MARGIN - 300, 205, 300, 260, fill=white)
    c.setFont(FONT_HEAD, 13)
    c.setFillColor(INK)
    c.drawString(left_x + 20, 435, "目标类别")
    c.drawString(PAGE_W - MARGIN - 280, 435, "当前类别")
    target = registry["summary"]["target_class_counts"]
    left_rows = [
        ("动作型实验", target["EXECUTABLE_STRATEGY"], TEAL),
        ("信号实验", target["SIGNAL"], BLUE),
        ("研究问题", target["RESEARCH_QUESTION"], PURPLE),
    ]
    yy = 380
    for label, value, color in left_rows:
        c.setFillColor(color)
        c.circle(left_x + 35, yy + 4, 8, fill=1, stroke=0)
        c.setFillColor(INK)
        c.setFont(FONT_HEAD, 10)
        c.drawString(left_x + 55, yy, label)
        c.setFont(FONT_MONO_BOLD, 18)
        c.setFillColor(color)
        c.drawRightString(left_x + 275, yy - 3, str(value))
        yy -= 62
    right_x = PAGE_W - MARGIN - 300
    _donut(c, right_x + 150, 337, 91, [105, 1], [RED, PURPLE], center_top="0", center_bottom="EXECUTABLE")
    _badge(c, "BLOCKED 105", right_x + 68, 225, fill=RED_LIGHT, color=RED, width=100)
    _badge(c, "研究问题 1", right_x + 172, 225, fill=PURPLE_LIGHT, color=PURPLE, width=92)
    _arrow(c, left_x + 315, 337, right_x - 15, 337, color=AMBER, width=2)
    c.setFillColor(AMBER)
    c.setFont(FONT_HEAD, 9)
    c.drawCentredString(mid_x, 355, "证据门重新计算")

    signal_counts = Counter(
        card["data"]["can_compute_now"]["signal_event"]
        for card in registry["experiments"]
    )
    _round_rect(c, MARGIN, 65, PAGE_W - 2 * MARGIN, 115, fill=NAVY, stroke=NAVY)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 11)
    c.drawString(MARGIN + 20, 151, "当前信号可计算性")
    statuses = [
        ("YES", signal_counts["YES"], TEAL, "信号可描述"),
        ("PARTIAL", signal_counts["PARTIAL"], AMBER, "仅部分输入/机制"),
        ("NO", signal_counts["NO"], RED, "当前不可计算"),
    ]
    start_x = MARGIN + 190
    for status, value, color, note in statuses:
        c.setFillColor(color)
        c.roundRect(start_x, 98, 122, 44, 10, fill=1, stroke=0)
        _draw_centered(c, str(value), start_x + 25, 114, font=FONT_HEAD, size=17, color=white)
        _draw_text(c, note, start_x + 51, 123, 64, size=7, color=white, leading=9, max_lines=2)
        start_x += 150
    _draw_text(
        c,
        "即使 signal_event=YES，也不代表严格成交、费用后 shadow PnL 或 realized PnL 可算。",
        MARGIN + 20,
        82,
        PAGE_W - 2 * MARGIN - 40,
        size=8.2,
        color=HexColor("#C9D7EE"),
        max_lines=1,
    )
    _finish_page(c, page_no)


def _page_data_truth(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "当前数据到底能支持什么",
        "L1/成交有 8 个 exact release；L2 估计只能用 7/12、7/15、7/17。",
        section="数据事实",
        accent=TEAL,
    )
    _round_rect(c, MARGIN, 315, PAGE_W - 2 * MARGIN, 145, fill=white)
    c.setFillColor(INK)
    c.setFont(FONT_HEAD, 11)
    c.drawString(MARGIN + 18, 433, "2026-07-10 至 2026-07-17 数据带")
    dates = [f"7/{day}" for day in range(10, 18)]
    start_x = MARGIN + 25
    cell_w = (PAGE_W - 2 * MARGIN - 50) / 8
    l2_clean = {"7/12", "7/15", "7/17"}
    l2_excluded = {"7/13", "7/14"}
    l2_conditional = {"7/16"}
    l2_missing = {"7/10", "7/11"}
    for date in dates:
        x = start_x
        c.setFillColor(SKY)
        c.roundRect(x, 370, cell_w - 8, 44, 8, fill=1, stroke=0)
        _draw_centered(c, date, x + (cell_w - 8) / 2, 395, font=FONT_HEAD, size=9, color=BLUE)
        _draw_centered(c, "L1 / trades", x + (cell_w - 8) / 2, 378, size=6.7, color=SLATE)
        if date in l2_clean:
            fill, color, label = TEAL_LIGHT, TEAL, "L2 CLEAN"
        elif date in l2_excluded:
            fill, color, label = RED_LIGHT, RED, "L2 排除"
        elif date in l2_conditional:
            fill, color, label = AMBER_LIGHT, AMBER, "V1 排除*"
        else:
            fill, color, label = LIGHT, MUTED, "L2 缺失"
        c.setFillColor(fill)
        c.roundRect(x, 332, cell_w - 8, 29, 7, fill=1, stroke=0)
        _draw_centered(c, label, x + (cell_w - 8) / 2, 342, font=FONT_HEAD, size=6.8, color=color)
        start_x += cell_w
    _draw_text(
        c,
        "* 7/16 后续根因审计判定为“维护窗条件可救”；待逐日质量门、窗口剔除、新收据与 Registry 新 revision 后才能纳入。",
        MARGIN + 18,
        321,
        PAGE_W - 2 * MARGIN - 36,
        size=6.4,
        color=AMBER,
        max_lines=1,
    )

    left_w = 370
    _round_rect(c, MARGIN, 70, left_w, 225, fill=NAVY, stroke=NAVY)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 11)
    c.drawString(MARGIN + 18, 268, "现有数据可以做")
    allowed = [
        "L1/成交信号发生率与条件 markout",
        "3 个干净 L2 日的深度、回补、撤退机制",
        "目录、市场图谱与生命周期覆盖检查",
        "保留全部零触发、零成交机会",
    ]
    yy = 235
    for item in allowed:
        c.setFillColor(TEAL)
        c.circle(MARGIN + 25, yy + 3, 3, fill=1, stroke=0)
        yy = _draw_text(c, item, MARGIN + 38, yy + 8, left_w - 56, size=8.4, color=white, leading=11, max_lines=2) - 11

    right_x = MARGIN + left_w + 15
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, 70, right_w, 225, fill=RED_LIGHT, stroke=HexColor("#F3C8C8"))
    c.setFillColor(RED)
    c.setFont(FONT_HEAD, 11)
    c.drawString(right_x + 18, 268, "现有数据不能证明")
    forbidden = [
        "自己的队列位置、真实成交与撤单竞态",
        "精确手续费、实测延迟与费用后现金 PnL",
        "完整结算、作废、延期和残余库存现金流",
        "外部比分、局/盘、阵容、伤病或真实 in-play",
        "untouched confirmation cohort 上的可复制收益",
    ]
    yy = 235
    for item in forbidden:
        c.setFillColor(RED)
        c.circle(right_x + 25, yy + 3, 3, fill=1, stroke=0)
        yy = _draw_text(c, item, right_x + 38, yy + 8, right_w - 56, size=8.2, color=INK, leading=11, max_lines=2) - 9
    _finish_page(c, page_no)


def _page_blockers(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "最大问题不是 80 个策略各自坏了",
        "前五个共享执行门同时阻断约 80 张卡；先修公共能力，复用价值最高。",
        section="阻断 Pareto",
        accent=RED,
    )
    counts = Counter(
        blocker
        for card in registry["experiments"]
        for blocker in card["blockers"]
    )
    top = counts.most_common(10)
    left_w = 520
    _round_rect(c, MARGIN, 70, left_w, y - 75, fill=white)
    c.setFont(FONT_HEAD, 11)
    c.setFillColor(INK)
    c.drawString(MARGIN + 18, y - 28, "卡片-阻断关系次数")
    _hbar_chart(
        c,
        [(BLOCKER_LABELS.get(k, k), v) for k, v in top],
        MARGIN + 18,
        y - 62,
        left_w - 36,
        row_h=33,
        color=RED,
        label_w=155,
        max_value=83,
    )

    right_x = MARGIN + left_w + 15
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, 280, right_w, 182, fill=NAVY, stroke=NAVY)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 11)
    c.drawString(right_x + 18, 434, "Wave 0：共享执行核心")
    core = [
        ("费用", "exact fee / rounding / rebate"),
        ("成交", "strict fill / queue / cancel race"),
        ("延迟", "place / cancel-effective / exit"),
        ("结算", "0/1/void/retire/postpone"),
        ("风险", "capital / inventory / tail / stop"),
    ]
    yy = 404
    for label, note in core:
        _badge(c, label, right_x + 18, yy - 9, fill=Color(1, 1, 1, alpha=0.12), color=white, width=46)
        _draw_text(c, note, right_x + 74, yy + 3, right_w - 92, font=FONT_MONO, size=6.7, color=HexColor("#C9D7EE"), max_lines=1)
        yy -= 27

    _round_rect(c, right_x, 70, right_w, 195, fill=AMBER_LIGHT, stroke=HexColor("#F0D49D"))
    c.setFillColor(HexColor("#8A5A06"))
    c.setFont(FONT_HEAD, 11)
    c.drawString(right_x + 18, 236, "怎么读这张图")
    notes = [
        "次数不是独立故障数，不能相加。",
        "关闭公共门只让 PnL 可测，不保证为正。",
        "每张卡仍需自己的信号、动作与基准验证。",
        "先修共同门，比逐卡写新代码更快接近现金验证。",
    ]
    yy = 204
    for note in notes:
        c.setFillColor(AMBER)
        c.circle(right_x + 24, yy + 3, 3, fill=1, stroke=0)
        yy = _draw_text(c, note, right_x + 37, yy + 8, right_w - 55, size=7.8, color=INK, leading=10, max_lines=2) - 8
    _finish_page(c, page_no)


def _page_pnl(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "真正的 PnL 是现金瀑布，不是漂亮的 markout",
        "唯一主指标：相对可执行基准的逐路径费用后增量现金 PnL。",
        section="PNL 合约",
        accent=CORAL,
    )
    formula_y = 430
    c.setFont(FONT_HEAD, 14)
    c.setFillColor(INK)
    c.drawString(MARGIN, formula_y, "DeltaPnL = NetPnL(strategy) - NetPnL(executable baseline)")
    _draw_text(
        c,
        "所有 eligible no-trigger、no-fill、no-accept 机会都保留为 0；容量不得超过测试过的可执行深度。",
        MARGIN,
        formula_y - 25,
        PAGE_W - 2 * MARGIN,
        size=8.5,
        color=SLATE,
        max_lines=1,
    )

    waterfall = [
        ("成交现金流", BLUE, "+"),
        ("交易所费用", RED, "-"),
        ("点差/滑点/冲击", CORAL, "-"),
        ("下单/撤单/退出延迟", AMBER, "-"),
        ("库存/作废/结算", PURPLE, "-"),
        ("抵押与资本", SLATE, "-"),
        ("费用后 NetPnL", TEAL, "="),
    ]
    start_x = MARGIN
    box_w = (PAGE_W - 2 * MARGIN - 6 * 9) / 7
    yy = 282
    heights = [105, 88, 76, 67, 58, 48, 40]
    for index, ((label, color, sign), height) in enumerate(zip(waterfall, heights)):
        x = start_x + index * (box_w + 9)
        c.setFillColor(color)
        c.roundRect(x, yy, box_w, height, 8, fill=1, stroke=0)
        if height < 60:
            sign_y = yy + height - 16
            label_y = yy + 13
            label_size = 6.5
            label_lines = 1
        else:
            sign_y = yy + height - 25
            label_y = yy + height - 43
            label_size = 7.3
            label_lines = 3
        _draw_centered(c, sign, x + 15, sign_y, font=FONT_HEAD, size=15, color=white)
        _draw_text(
            c,
            label,
            x + 13,
            label_y,
            box_w - 24,
            size=label_size,
            color=white,
            leading=9,
            max_lines=label_lines,
        )

    _round_rect(c, MARGIN, 72, 480, 178, fill=NAVY, stroke=NAVY)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 11)
    c.drawString(MARGIN + 18, 222, "P10 商业 ROC 额外扣除")
    extras = [
        "数据源 / 许可 / API / 历史回填",
        "云计算 / 存储 / 请求 / 出网",
        "运营人工 / 值班 / 事故损失与修复",
        "完整资本与抵押时间成本",
    ]
    yy = 191
    for item in extras:
        c.setFillColor(CORAL)
        c.circle(MARGIN + 25, yy + 3, 3, fill=1, stroke=0)
        yy = _draw_text(c, item, MARGIN + 38, yy + 8, 430, size=8.4, color=white, leading=11, max_lines=1) - 10

    right_x = MARGIN + 495
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, 72, right_w, 178, fill=RED_LIGHT, stroke=HexColor("#F3C8C8"))
    c.setFillColor(RED)
    c.setFont(FONT_HEAD, 11)
    c.drawString(right_x + 18, 222, "只允许做诊断，不是现金 PnL")
    diagnostics = ["mid return", "markout", "microprice", "spread opportunity"]
    yy = 188
    for item in diagnostics:
        _badge(c, item, right_x + 18, yy - 8, fill=white, color=RED, width=120)
        yy -= 28
    _draw_text(
        c,
        "成本未知时必须 BLOCK，不得填 0。",
        right_x + 150,
        191,
        right_w - 168,
        font=FONT_HEAD,
        size=9,
        color=RED,
        leading=13,
        max_lines=3,
    )
    _finish_page(c, page_no)


def _page_rfq(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "RFQ：公开信息能做风险控制，不能猜方向",
        "公开 create/delete 是 unsigned；方向与直接报价 PnL 只能来自私有 accepted_side 和真实成交链。",
        section="RFQ 边界",
        accent=PURPLE,
    )
    lane_y = 238
    lane_h = 225
    lane_w = (PAGE_W - 2 * MARGIN - 18) / 2
    _round_rect(c, MARGIN, lane_y, lane_w, lane_h, fill=SKY, stroke=HexColor("#BFD6F7"))
    _badge(c, "PUBLIC - UNSIGNED", MARGIN + 18, lane_y + lane_h - 40, fill=BLUE, color=white, width=132)
    public_steps = [
        "create / delete / size / lifetime / requester hash",
        "只测到达率、规模、寿命、绝对波动/毒性",
        "允许动作：撤单、放宽、缩量，同时保护两边",
        "D02 / D03 / D05 / D07 -> RFQ_GUARD",
    ]
    yy = lane_y + lane_h - 78
    for step in public_steps:
        c.setFillColor(BLUE)
        c.circle(MARGIN + 27, yy + 3, 3, fill=1, stroke=0)
        yy = _draw_text(c, step, MARGIN + 40, yy + 8, lane_w - 60, size=8.2, color=INK, leading=11, max_lines=2) - 12

    right_x = MARGIN + lane_w + 18
    _round_rect(c, right_x, lane_y, lane_w, lane_h, fill=PURPLE_LIGHT, stroke=HexColor("#CDC3F0"))
    _badge(c, "PRIVATE - SIGNED", right_x + 18, lane_y + lane_h - 40, fill=PURPLE, color=white, width=132)
    private_steps = [
        "accepted_side / quote / confirmation / executed",
        "fills / fees / hedge / inventory / settlement",
        "D06 仅做私有方向信号分析，无动作",
        "D08 才能定义 RFQ quote + hedge 的现金 PnL",
    ]
    yy = lane_y + lane_h - 78
    for step in private_steps:
        c.setFillColor(PURPLE)
        c.circle(right_x + 27, yy + 3, 3, fill=1, stroke=0)
        yy = _draw_text(c, step, right_x + 40, yy + 8, lane_w - 60, size=8.2, color=INK, leading=11, max_lines=2) - 12

    _round_rect(c, MARGIN, 72, PAGE_W - 2 * MARGIN, 142, fill=NAVY, stroke=NAVY)
    c.setFillColor(RED)
    c.roundRect(MARGIN + 22, 105, 245, 70, 11, fill=1, stroke=0)
    _draw_centered(c, "combo leg side", MARGIN + 144, 151, font=FONT_HEAD, size=11, color=white)
    _draw_centered(c, "≠ requester 买卖方向", MARGIN + 144, 126, font=FONT_HEAD, size=10, color=white)
    _arrow(c, MARGIN + 288, 140, MARGIN + 365, 140, color=white, width=1.8)
    _draw_text(
        c,
        "当前 RFQ_PUBLIC_DATA_UNRELEASED 和 RFQ_PRIVATE_EVENTS_MISSING 仍在阻断；D08 不能宣称可交易。",
        MARGIN + 390,
        154,
        PAGE_W - MARGIN - (MARGIN + 410),
        font=FONT_HEAD,
        size=9.2,
        color=white,
        leading=14,
        max_lines=3,
    )
    _finish_page(c, page_no)


def _page_mve(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "MVE：独立执行产品，不是 RFQ 的别名",
        "D10 使用专用 MVE 私有数据契约；RFQ 事件不能跨线替代 package 执行。",
        section="MVE 边界",
        accent=AMBER,
    )
    steps = [
        ("01", "Exact MVE catalog", "产品版本与规则"),
        ("02", "Leg / orientation", "支付矩阵穷尽"),
        ("03", "同步 leg L2", "真实可执行深度"),
        ("04", "Private package", "order / ack / fill / fee"),
        ("05", "Hedge + settlement", "孤腿与部分包风险"),
        ("06", "Joint NetPnL", "全腿费用后现金"),
    ]
    x = MARGIN
    box_y = 320
    box_w = (PAGE_W - 2 * MARGIN - 5 * 10) / 6
    for index, (num, title, note) in enumerate(steps):
        color = [BLUE, PURPLE, TEAL, AMBER, CORAL, RED][index]
        _round_rect(c, x, box_y, box_w, 125, fill=white, stroke=color, radius=10, line_width=1.2)
        c.setFillColor(color)
        c.circle(x + 22, box_y + 100, 13, fill=1, stroke=0)
        _draw_centered(c, num, x + 22, box_y + 96, font=FONT_MONO_BOLD, size=7, color=white)
        _draw_text(c, title, x + 16, box_y + 70, box_w - 30, font=FONT_HEAD, size=8.5, color=INK, leading=11, max_lines=2)
        _draw_text(c, note, x + 16, box_y + 36, box_w - 30, size=7, color=MUTED, leading=9, max_lines=2)
        if index < 5:
            _arrow(c, x + box_w + 1, box_y + 62, x + box_w + 8, box_y + 62, color=MUTED)
        x += box_w + 10

    _round_rect(c, MARGIN, 72, 465, 215, fill=AMBER_LIGHT, stroke=HexColor("#F0D49D"))
    c.setFillColor(HexColor("#8A5A06"))
    c.setFont(FONT_HEAD, 11)
    c.drawString(MARGIN + 18, 258, "D10 已经定义的动作")
    by_id = _card_by_id(registry)
    d10 = by_id["D10-MVE-DIRECT-TRADING"]
    details = [
        ("动作", ACTION_LABELS[d10["action"]["profile_id"]]),
        ("场所", "Kalshi MVE private execution"),
        ("基准", "相同机会不做 MVE package"),
        ("PnL", "package + hedge 现金 - 全腿费用 - 孤腿成本 + joint terminal"),
        ("状态", "BLOCKED / NO AUTHORITY"),
    ]
    yy = 224
    for label, value in details:
        c.setFillColor(AMBER)
        c.setFont(FONT_HEAD, 8)
        c.drawString(MARGIN + 20, yy, label)
        yy = _draw_text(c, value, MARGIN + 70, yy + 2, 370, size=7.4, color=INK, leading=10, max_lines=2) - 10

    right_x = MARGIN + 480
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, 72, right_w, 215, fill=RED_LIGHT, stroke=HexColor("#F3C8C8"))
    c.setFillColor(RED)
    c.setFont(FONT_HEAD, 11)
    c.drawString(right_x + 18, 258, "仍然缺失")
    missing = [
        "MVE private package lifecycle",
        "exact leg/orientation/payout mapping",
        "multi-leg atomic/orphan-safe engine",
        "fees, latency, risk and settlement",
    ]
    yy = 224
    for item in missing:
        c.setFillColor(RED)
        c.circle(right_x + 25, yy + 2, 3, fill=1, stroke=0)
        yy = _draw_text(c, item, right_x + 38, yy + 7, right_w - 56, size=7.8, color=INK, leading=10, max_lines=2) - 12
    _finish_page(c, page_no)


def _page_external(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "外部体育数据：没有就明确写没有",
        "当前没有直接体育数据 adapter；缺失必须 BLOCK_NO_IMPUTE，候选 API 不等于已接入。",
        section="外部数据",
        accent=RED,
    )
    missing = ["比分", "局/盘/节", "阵容", "伤病", "真实开赛状态", "外部赔率"]
    x = MARGIN
    for item in missing:
        _badge(c, f"{item}  MISSING", x, 426, fill=RED_LIGHT, color=RED, width=112)
        x += 122

    _round_rect(c, MARGIN, 250, PAGE_W - 2 * MARGIN, 150, fill=white)
    c.setFont(FONT_HEAD, 11)
    c.setFillColor(INK)
    c.drawString(MARGIN + 18, 372, "候选来源 - 全部待验证")
    sources = [
        ("Kalshi Live Data", "direct labels / live state"),
        ("The Odds API", "external odds"),
        ("OpticOdds", "odds / results / lineup"),
        ("Sportradar", "play-by-play / roster"),
        ("NOAA / NCEI", "weather control"),
    ]
    col_w = (PAGE_W - 2 * MARGIN - 36) / 5
    x = MARGIN + 10
    for name, kind in sources:
        _round_rect(c, x, 277, col_w, 72, fill=AMBER_LIGHT, stroke=HexColor("#F0D49D"), radius=8)
        _draw_text(c, name, x + 10, 329, col_w - 20, font=FONT_HEAD, size=7.5, color=HexColor("#8A5A06"), leading=9, max_lines=2)
        _draw_text(c, kind, x + 10, 298, col_w - 20, font=FONT_MONO, size=5.8, color=SLATE, leading=8, max_lines=2)
        x += col_w + 9

    stages = [
        "授权/许可",
        "字段语义",
        "source + receive clocks",
        "append-only capture",
        "event/market mapping",
        "point-in-time 验证",
    ]
    x = MARGIN
    for index, stage in enumerate(stages):
        box_w = (PAGE_W - 2 * MARGIN - 5 * 9) / 6
        _round_rect(c, x, 145, box_w, 72, fill=NAVY, stroke=NAVY, radius=8)
        _draw_centered(c, str(index + 1), x + 18, 190, font=FONT_MONO_BOLD, size=8, color=TEAL)
        _draw_text(c, stage, x + 12, 175, box_w - 24, size=6.8, color=white, leading=9, max_lines=3)
        if index < 5:
            _arrow(c, x + box_w + 1, 181, x + box_w + 7, 181, color=MUTED)
        x += box_w + 9

    _round_rect(c, MARGIN, 72, PAGE_W - 2 * MARGIN, 50, fill=RED_LIGHT, stroke=HexColor("#F3C8C8"))
    _draw_text(
        c,
        "禁止：从盘口价格推断比分/伤病；把当前 snapshot 伪装成历史 point-in-time；把 scheduled-start proxy 改名为真实 in-play。",
        MARGIN + 18,
        100,
        PAGE_W - 2 * MARGIN - 36,
        font=FONT_HEAD,
        size=8.5,
        color=RED,
        leading=11,
        max_lines=2,
    )
    _finish_page(c, page_no)


def _page_priority(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "优先队列：先解公共门，再测最接近现金的卡",
        "这是“离可审计 PnL 有多远”的运营排序，不是预期收益排名或交易推荐。",
        section="验证队列",
        accent=AMBER,
    )
    by_id = _card_by_id(registry)
    priority = [
        ("A01-SPREAD-CAPTURE", "已有 gross 描述", "strict fill + fee"),
        ("A11-ONE-SIDED-PROVISION", "已有窄 pre-start 描述", "fail-fast 严格成交"),
        ("A12-GENERIC-PASSIVE-SCALPING", "L1 信号可构造", "被动成交与退出"),
        ("B09-LISTING-TO-START-DRIFT", "只需内部时钟", "方向成交与权限"),
        ("B14-VOL-HARVEST", "past-only vol", "maker 费用后 replay"),
        ("F03-NEW-MARKET-COLD-START", "内部 age/uptime", "何时报价的 shadow"),
        ("F11-GOLF-THIN-LONG-LIFECYCLE", "外部数据仅 optional", "资本与终局尾部"),
        ("A03-DEPLETION-REFILL", "3 个 clean L2 日", "own-order 校准"),
        ("A04-QUEUE-LAMBDA-DISTANCE", "L1 信号可构造", "queue/fill 校准"),
        ("B01-MOMENTUM-CONTINUATION", "L1 方向信号", "venue authority"),
    ]
    table_x = MARGIN
    table_y = 100
    table_w = 535
    table_h = y - 110
    _round_rect(c, table_x, table_y, table_w, table_h, fill=white)
    headers = [("#", 28), ("实验", 205), ("现有接近度", 145), ("第一新增门", 135)]
    x = table_x + 10
    for title, width in headers:
        c.setFillColor(LIGHT)
        c.rect(x, table_y + table_h - 34, width, 24, fill=1, stroke=0)
        _draw_text(c, title, x + 5, table_y + table_h - 20, width - 10, font=FONT_HEAD, size=7, color=SLATE, max_lines=1)
        x += width
    row_h = 29
    yy = table_y + table_h - 58
    for index, (card_id, reason, first_gate) in enumerate(priority, 1):
        if index % 2 == 0:
            c.setFillColor(LIGHT)
            c.rect(table_x + 10, yy - 9, table_w - 20, row_h, fill=1, stroke=0)
        c.setFont(FONT_MONO_BOLD, 7)
        c.setFillColor(AMBER)
        c.drawString(table_x + 18, yy + 7, f"{index:02d}")
        _draw_text(c, card_id, table_x + 48, yy + 8, 195, font=FONT_MONO_BOLD, size=6.3, color=INK, max_lines=1)
        _draw_text(c, reason, table_x + 253, yy + 8, 135, size=6.8, color=SLATE, max_lines=2)
        _draw_text(c, first_gate, table_x + 398, yy + 8, 125, size=6.8, color=RED, max_lines=2)
        yy -= row_h

    right_x = table_x + table_w + 15
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, 300, right_w, y - 310, fill=NAVY, stroke=NAVY)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 11)
    c.drawString(right_x + 18, y - 28, "Wave 0 先做")
    wave0 = [
        "exact fee facts",
        "shadow order ledger",
        "strict fill + latency",
        "settlement + risk gates",
    ]
    yy = y - 61
    for index, item in enumerate(wave0, 1):
        c.setFillColor(TEAL)
        c.circle(right_x + 26, yy + 3, 10, fill=1, stroke=0)
        _draw_centered(c, str(index), right_x + 26, yy, font=FONT_MONO_BOLD, size=6.8, color=white)
        _draw_text(c, item, right_x + 45, yy + 6, right_w - 63, font=FONT_MONO, size=6.6, color=white, max_lines=1)
        yy -= 31

    _round_rect(c, right_x, 100, right_w, 185, fill=RED_LIGHT, stroke=HexColor("#F3C8C8"))
    c.setFillColor(RED)
    c.setFont(FONT_HEAD, 10)
    c.drawString(right_x + 18, 257, "继续保持红色阻断")
    blocked = [
        "D08 RFQ direct quote",
        "D10 MVE direct trading",
        "C05 / F12 / F13 external",
        "G/P 依赖已确认 child",
    ]
    yy = 226
    for item in blocked:
        c.setFillColor(RED)
        c.circle(right_x + 25, yy + 2, 3, fill=1, stroke=0)
        yy = _draw_text(
            c,
            item,
            right_x + 38,
            yy + 7,
            right_w - 56,
            font=FONT_BODY,
            size=6.7,
            color=INK,
            leading=9,
            max_lines=2,
        ) - 12
    _draw_text(
        c,
        "A11 当前广义 one-sided fade 被反驳；进入队列是为了低成本证伪窄 cell，不是看多。",
        right_x + 18,
        130,
        right_w - 36,
        size=7.2,
        color=RED,
        leading=10,
        max_lines=3,
    )
    _finish_page(c, page_no)


def _page_roadmap(c: canvas.Canvas, registry: dict[str, Any], page_no: int) -> None:
    y = _page_header(
        c,
        page_no,
        "下一轮如何真正走到实盘",
        "每张卡按收据逐级升级：Registry -> Signal -> Shadow -> Micro-live -> Live；任何一级不得自动越级。",
        section="落地路线",
        accent=TEAL,
    )
    stages = [
        ("1", "Registry", "卡片冻结\n无权限"),
        ("2", "Signal", "样本外信号\n保留零机会"),
        ("3", "Shadow", "无下单\n路径级 PnL"),
        ("4", "Micro-live", "一次性授权\n1 contract"),
        ("5", "Live", "真实对账\n另行扩容"),
    ]
    x = MARGIN
    box_y = 305
    box_w = (PAGE_W - 2 * MARGIN - 4 * 15) / 5
    for index, (num, title, note) in enumerate(stages):
        color = [BLUE, TEAL, AMBER, CORAL, RED][index]
        _round_rect(c, x, box_y, box_w, 140, fill=white, stroke=color, radius=12, line_width=1.4)
        c.setFillColor(color)
        c.circle(x + box_w / 2, box_y + 110, 19, fill=1, stroke=0)
        _draw_centered(c, num, x + box_w / 2, box_y + 104, font=FONT_HEAD, size=12, color=white)
        _draw_centered(c, title, x + box_w / 2, box_y + 71, font=FONT_HEAD, size=10, color=INK)
        _draw_text(c, note, x + 20, box_y + 48, box_w - 40, size=7, color=MUTED, leading=10, max_lines=3)
        if index < 4:
            _arrow(c, x + box_w + 2, box_y + 70, x + box_w + 13, box_y + 70, color=MUTED)
        x += box_w + 15

    _round_rect(c, MARGIN, 72, 475, 202, fill=NAVY, stroke=NAVY)
    c.setFillColor(white)
    c.setFont(FONT_HEAD, 11)
    c.drawString(MARGIN + 18, 246, "下一轮三件实物交付")
    deliverables = [
        ("01", "共享执行账本", "fee / order / fill / latency / settlement / risk"),
        ("02", "Top-3 Shadow 报告", "路径级 DeltaPnL + 全部零机会 + 容量"),
        ("03", "Go / No-Go 表", "失败也出版，不自动进入 micro-live"),
    ]
    yy = 208
    for num, title, note in deliverables:
        c.setFillColor(TEAL)
        c.circle(MARGIN + 31, yy + 4, 13, fill=1, stroke=0)
        _draw_centered(c, num, MARGIN + 31, yy, font=FONT_MONO_BOLD, size=6.5, color=white)
        c.setFillColor(white)
        c.setFont(FONT_HEAD, 8.5)
        c.drawString(MARGIN + 53, yy + 5, title)
        _draw_text(
            c,
            note,
            MARGIN + 170,
            yy + 7,
            315,
            font=FONT_BODY,
            size=6.2,
            color=HexColor("#C9D7EE"),
            max_lines=2,
        )
        yy -= 49

    right_x = MARGIN + 490
    right_w = PAGE_W - MARGIN - right_x
    _round_rect(c, right_x, 72, right_w, 202, fill=white)
    c.setFillColor(INK)
    c.setFont(FONT_HEAD, 11)
    c.drawString(right_x + 18, 246, "质量与权限收口")
    facts = [
        ("独立审计", "PASS / P0=0 / P1=0"),
        ("Registry tests", "21 / 21 PASS"),
        ("完整 make check", "PASS"),
        ("AWS / 下单写入", "NONE"),
        ("唯一 P2", "B08/E04 深度卡冻结退出规则"),
    ]
    yy = 213
    for label, value in facts:
        c.setFont(FONT_BODY, 7.6)
        c.setFillColor(MUTED)
        c.drawString(right_x + 18, yy, label)
        c.setFont(FONT_BODY, 6.5)
        c.setFillColor(TEAL if "PASS" in value or value == "NONE" else AMBER)
        c.drawRightString(right_x + right_w - 18, yy, value)
        yy -= 28
    _finish_page(c, page_no)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_provenance(
    c: canvas.Canvas,
    registry: dict[str, Any],
    page_no: int,
) -> None:
    y = _page_header(
        c,
        page_no,
        "证据锚：这份报告从哪里来",
        "Registry、来源文件与版本提交均给出不可变指纹；会话独立审计没有伪装成仓库内持久收据。",
        section="PROVENANCE",
        accent=PURPLE,
    )
    registry_file_sha = _sha256_file(REGISTRY_PATH)
    commit = "ea6246a44f4d4f6a57f36931fc445810f740b899"

    _round_rect(c, MARGIN, 372, PAGE_W - 2 * MARGIN, 88, fill=NAVY, stroke=NAVY)
    anchors = [
        ("Registry commit", commit),
        ("Canonical registry SHA", registry["registry_sha256"]),
        ("Registry JSON file SHA", registry_file_sha),
    ]
    yy = 433
    for label, value in anchors:
        c.setFont(FONT_HEAD, 7.8)
        c.setFillColor(HexColor("#9FB7DA"))
        c.drawString(MARGIN + 18, yy, label)
        c.setFont(FONT_MONO, 7.2)
        c.setFillColor(white)
        c.drawString(MARGIN + 150, yy, value)
        yy -= 23

    _round_rect(c, MARGIN, 164, PAGE_W - 2 * MARGIN, 192, fill=white)
    c.setFillColor(INK)
    c.setFont(FONT_HEAD, 10.5)
    c.drawString(MARGIN + 16, 334, "5 个本地来源绑定")
    headers = [("角色", 150), ("本地路径", 390), ("SHA-256", 225)]
    x = MARGIN + 14
    for label, width in headers:
        c.setFillColor(LIGHT)
        c.rect(x, 298, width, 23, fill=1, stroke=0)
        _draw_text(c, label, x + 5, 311, width - 10, font=FONT_HEAD, size=7, color=SLATE, max_lines=1)
        x += width
    yy = 274
    for index, binding in enumerate(registry["source_bindings"]):
        if index % 2:
            c.setFillColor(LIGHT)
            c.rect(MARGIN + 14, yy - 11, PAGE_W - 2 * MARGIN - 28, 31, fill=1, stroke=0)
        x = MARGIN + 19
        _draw_text(c, binding["role"], x, yy + 6, 140, font=FONT_MONO_BOLD, size=6.6, color=PURPLE, max_lines=2)
        x += 150
        _draw_text(c, binding["path"], x, yy + 6, 380, font=FONT_BODY, size=6.7, color=INK, max_lines=2)
        x += 390
        _draw_text(c, binding["sha256"], x, yy + 6, 215, font=FONT_MONO, size=6.3, color=SLATE, max_lines=2)
        yy -= 31

    gap = 14
    box_w = (PAGE_W - 2 * MARGIN - gap) / 2
    _round_rect(c, MARGIN, 44, box_w, 88, fill=TEAL_LIGHT, stroke=HexColor("#B9E4DB"))
    c.setFillColor(TEAL)
    c.setFont(FONT_HEAD, 9.2)
    c.drawString(MARGIN + 15, 112, "校验事实与诚实边界")
    _draw_text(
        c,
        "独立会话审计：PASS / P0=0 / P1=0 / P2=1；Registry tests 21/21，make check PASS。"
        "目前没有仓库内独立审计收据，因此本页不以 PDF 自身作循环证明。",
        MARGIN + 15,
        93,
        box_w - 30,
        size=7.3,
        color=INK,
        leading=10,
        max_lines=5,
    )

    right_x = MARGIN + box_w + gap
    _round_rect(c, right_x, 44, box_w, 88, fill=AMBER_LIGHT, stroke=HexColor("#F0D7A7"))
    c.setFillColor(AMBER)
    c.setFont(FONT_HEAD, 9.2)
    c.drawString(right_x + 15, 112, "7/16 的版本化口径")
    _draw_text(
        c,
        "Registry V1 按旧审计排除；后续根因审计判定“维护窗条件可救”。"
        "只有逐日质量门、窗口剔除、新收据和新 Registry revision 全部落地后才可纳入。",
        right_x + 15,
        93,
        box_w - 30,
        size=7.3,
        color=INK,
        leading=10,
        max_lines=5,
    )
    _finish_page(c, page_no)


DETAIL_MARGIN = 36
DETAIL_COLS = 4
DETAIL_COL_GAP = 12
DETAIL_COL_W = (
    DETAIL_PAGE_W
    - 2 * DETAIL_MARGIN
    - (DETAIL_COLS - 1) * DETAIL_COL_GAP
) / DETAIL_COLS
DETAIL_TOP = DETAIL_PAGE_H - 132
DETAIL_BOTTOM = 44
DETAIL_CONTENT_H = DETAIL_TOP - DETAIL_BOTTOM
DETAIL_FONT_SIZE = 8.4
DETAIL_LEADING = 10.7
DETAIL_SECTION_H = 21


def _detail_value(value: Any, *, null_text: str = "NOT_APPLICABLE / 不是策略") -> str:
    if value is None:
        return null_text
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dict):
        if not value:
            return "NONE"
        return "; ".join(
            f"{key}={_detail_value(item)}"
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        if not value:
            return "NONE"
        return " | ".join(_detail_value(item) for item in value)
    return " ".join(str(value).split())


def _detail_entries(
    mapping: dict[str, Any],
    labels: dict[str, str] | None = None,
) -> list[str]:
    labels = labels or {}
    return [
        f"{labels.get(key, key)} | {_detail_value(value)}"
        for key, value in mapping.items()
    ]


def _source_binding_for_observation(
    registry: dict[str, Any],
    source: str | None,
) -> str:
    if not source:
        return "NOT_MEASURED"
    basename = source.split("::", 1)[0]
    for binding in registry["source_bindings"]:
        if Path(binding["path"]).name == basename:
            return (
                f"{source} | role={binding['role']} | "
                f"sha256={binding['sha256']}"
            )
    return f"{source} | SOURCE_BINDING_NOT_FOUND"


def _provider_statuses(
    registry: dict[str, Any],
    source_ids: Iterable[str],
) -> str:
    catalog = {
        source["source_id"]: source
        for source in registry["external_source_catalog"]["sources"]
    }
    rows = []
    for source_id in source_ids:
        source = catalog.get(source_id)
        if source is None:
            rows.append(f"{source_id}=CATALOG_MISSING")
        else:
            rows.append(
                f"{source_id}: status={source['status']}; "
                f"live_use={source['live_use']}; "
                f"historical={source['historical_backfill']}"
            )
    return " | ".join(rows) if rows else "NONE"


def _card_detail_sections(
    registry: dict[str, Any],
    card: dict[str, Any],
) -> list[tuple[str, list[str]]]:
    identity = card["identity"]
    classification = card["classification"]
    observed = card["current_result"].get("observed_signal") or {}

    status_entries = _detail_entries(
        identity,
        {
            "experiment_id": "实验卡 ID",
            "family_code": "家族",
            "family_name": "家族名称",
            "version": "卡片版本",
            "lineage_status": "血缘状态",
            "source_atomic_id": "原子来源 ID",
            "registry_id": "Registry ID",
        },
    )
    status_entries.extend(
        _detail_entries(
            classification,
            {
                "target_class": "目标类别",
                "computed_class": "当前类别",
                "blocked_from": "阻断自",
                "lifecycle": "生命周期",
                "strategy_eligible": "当前策略合格",
                "promotion_rule": "升级规则",
            },
        )
    )
    status_entries.extend(
        [
            f"definition_sha256 | {card['definition_sha256']}",
            f"Registry action authority | {registry['scope']['action_authority']}",
            f"Registry order authority | {registry['scope']['order_authority']}",
            f"Registry deployment authority | {registry['scope']['deployment_authority']}",
        ]
    )

    hypothesis_entries = [
        f"经济机制 mechanism | {_detail_value(card['hypothesis']['mechanism'])}",
        f"可证伪陈述 falsifiable_statement | {_detail_value(card['hypothesis']['falsifiable_statement'])}",
        f"主估计量 primary_estimand | {_detail_value(card['hypothesis']['primary_estimand'])}",
    ]
    for key, value in card["hypothesis"]["failure_conditions"].items():
        hypothesis_entries.append(f"失败条件 {key} | {_detail_value(value)}")

    signal_entries = _detail_entries(
        card["signal"],
        {
            "trigger": "触发信号",
            "direction_semantics": "方向语义",
            "decision_clock": "决策时钟",
            "forecast_horizon": "预测/持有区间",
            "falsifier": "信号证伪",
        },
    )
    for key, value in card["current_result"].items():
        if key == "observed_signal":
            continue
        signal_entries.append(f"当前证据 {key} | {_detail_value(value)}")
    signal_entries.extend(
        [
            f"observed_signal.status | {_detail_value(observed.get('status'), null_text='NOT_MEASURED')}",
            f"observed_signal.values | {_detail_value(observed.get('values'), null_text='NOT_MEASURED')}",
            (
                "observed_signal.source | "
                + _source_binding_for_observation(
                    registry,
                    observed.get("source"),
                )
            ),
        ]
    )

    action_entries = _detail_entries(
        card["action"],
        {
            "profile_id": "动作 profile",
            "status": "动作状态",
            "registered_signal_to_action": "登记的 signal -> action",
            "signal_to_action_mapping": "具体信号到动作映射",
            "action_type": "动作类型",
            "instrument_side": "交易哪一边",
            "order_style": "订单类型",
            "price_rule": "价格规则",
            "quantity_rule": "数量规则",
            "timing_and_cancel": "时序与撤单",
            "exit_or_settlement": "退出/结算",
            "kill_conditions": "停止条件",
            "current_execution_authority": "当前执行权限",
        },
    )

    venue_baseline_entries = _detail_entries(
        card["venue"],
        {
            "execution_venue": "执行场所",
            "availability_receipt": "场所可用收据",
            "required_permission": "所需权限",
        },
    )
    venue_baseline_entries.extend(
        _detail_entries(
            card["baseline"],
            {
                "baseline_id": "基准 ID",
                "baseline_type": "基准类型",
                "policy": "基准策略",
                "comparability": "可比性",
                "secondary_baselines": "次级基准",
            },
        )
    )

    pnl_entries = _detail_entries(
        card["pnl"],
        {
            "status": "PnL 状态",
            "basis": "PnL 口径",
            "formula": "现金 PnL 公式",
            "incremental_formula": "相对基准增量 PnL",
            "aggregation_unit": "聚合单元",
            "denominators": "报告分母",
            "zero_fill_treatment": "零触发/零成交处理",
            "capacity_rule": "容量规则",
            "markout_rule": "markout 边界",
            "cost_allocation_rule": "成本分摊规则",
            "roc_definition": "商业 ROC 定义",
        },
    )

    if card["costs"]:
        cost_entries = []
        for index, cost in enumerate(card["costs"], 1):
            cost_entries.append(
                f"成本 {index:02d} | type={_detail_value(cost.get('type'))}; "
                f"status={_detail_value(cost.get('status'))}; "
                f"role={_detail_value(cost.get('role'))}; "
                f"rule={_detail_value(cost.get('rule'))}"
            )
    else:
        cost_entries = [
            "成本 | NOT_APPLICABLE / 不是策略；未知 PnL 不显示为 0"
        ]

    data = card["data"]
    data_entries = [
        f"data profile | {_detail_value(data['profile_id'])}",
        f"输入 inputs | {_detail_value(data['inputs'])}",
        f"当前能算 can_compute_now | {_detail_value(data['can_compute_now'])}",
        f"目前可计算 | {_detail_value(data['currently_calculable'])}",
        f"data blockers | {_detail_value(data['blockers'])}",
        f"缺失数据 missing | {_detail_value(data['missing'])}",
        f"采集/建设路径 acquisition_paths | {_detail_value(data['acquisition_paths'])}",
    ]
    data_entries.extend(
        f"study_dates.{key} | {_detail_value(value)}"
        for key, value in data["study_dates"].items()
    )

    gap_entries = [
        f"完整卡级 blockers | {_detail_value(card['blockers'])}"
    ]
    if data["critical_gaps"]:
        for index, gap in enumerate(data["critical_gaps"], 1):
            gap_entries.append(
                f"缺口 {index:02d} | blocker={_detail_value(gap.get('blocker'))}; "
                f"stage={_detail_value(gap.get('blocking_stage'))}; "
                f"missing={_detail_value(gap.get('missing_evidence'))}; "
                f"补数/建设={_detail_value(gap.get('acquisition_or_build_path'))}; "
                f"API candidates={_detail_value(gap.get('external_api_candidate_ids'))}; "
                f"unknown policy={_detail_value(gap.get('unknown_value_policy'))}"
            )
    else:
        gap_entries.append("critical_gaps | NONE")

    external = card["external_sports_data"]
    external_entries = _detail_entries(
        external,
        {
            "dependency": "外部体育依赖",
            "availability": "可用状态",
            "required_variables": "所需直接变量",
            "approved_source": "已批准来源",
            "provider_candidates": "候选 provider",
            "asof_clock_status": "时钟状态",
            "license_status": "许可状态",
            "historical_backfill_status": "历史点时状态",
            "missing_policy": "缺失策略",
            "proxy_policy": "代理禁区",
            "blockers": "外部数据 blockers",
        },
    )
    provider_ids = external.get("provider_candidates") or []
    external_entries.append(
        "候选来源目录状态 | "
        + _provider_statuses(registry, provider_ids)
    )

    design_entries = _detail_entries(
        card["experiment_design"],
        {
            "unit": "检验单元",
            "eligibility": "入样资格",
            "clock_rule": "时间规则",
            "forecast_horizon": "冻结区间",
            "comparison": "比较方法",
            "overlap_and_precedence": "重叠与优先级",
            "split_status": "切分状态",
            "minimum_evidence": "最低证据",
            "multiple_testing_family": "多重检验族",
            "stop_rule": "停止规则",
        },
    )
    next_entries = _detail_entries(
        card["next_evidence"],
        {
            "to_measure_signal": "补齐信号",
            "to_measure_pnl": "补齐 PnL",
            "external_source_ids": "外部来源 ID",
        },
    )
    next_entries.append(
        "外部来源目录解析 | "
        + _provider_statuses(
            registry,
            card["next_evidence"].get("external_source_ids") or [],
        )
    )

    return [
        ("身份、状态与权限", status_entries),
        ("假设与失败条件", hypothesis_entries),
        ("信号与当前证据", signal_entries),
        ("触发后的具体动作", action_entries),
        ("执行场所与基准策略", venue_baseline_entries),
        ("现金 PnL 合约", pnl_entries),
        ("成本逐项", cost_entries),
        ("当前可算、数据与日期", data_entries),
        ("完整缺口与补数路径", gap_entries),
        ("外部体育数据契约", external_entries),
        ("检验设计", design_entries),
        ("下一步证据", next_entries),
    ]


def _new_detail_page_layout() -> list[list[dict[str, Any]]]:
    return [[] for _ in range(DETAIL_COLS)]


def _layout_detail_sections(
    sections: list[tuple[str, list[str]]],
) -> list[list[list[dict[str, Any]]]]:
    pages: list[list[list[dict[str, Any]]]] = [_new_detail_page_layout()]
    page_index = 0
    column_index = 0
    used = 0.0

    def advance_column() -> None:
        nonlocal page_index, column_index, used
        column_index += 1
        used = 0.0
        if column_index >= DETAIL_COLS:
            pages.append(_new_detail_page_layout())
            page_index += 1
            column_index = 0

    def add_block(block: dict[str, Any]) -> None:
        nonlocal used
        pages[page_index][column_index].append(block)
        used += float(block["height"])

    for section_title, entries in sections:
        remaining = DETAIL_CONTENT_H - used
        if remaining < DETAIL_SECTION_H + DETAIL_LEADING * 2:
            advance_column()
        add_block(
            {
                "kind": "section",
                "title": section_title,
                "height": DETAIL_SECTION_H,
            }
        )
        for entry in entries:
            lines = _split_lines(
                entry,
                DETAIL_COL_W - 22,
                FONT_BODY,
                DETAIL_FONT_SIZE,
            )
            if not lines:
                lines = ["—"]
            offset = 0
            while offset < len(lines):
                remaining = DETAIL_CONTENT_H - used
                available = int((remaining - 5) // DETAIL_LEADING)
                if available < 1:
                    advance_column()
                    add_block(
                        {
                            "kind": "section",
                            "title": f"{section_title}（续）",
                            "height": DETAIL_SECTION_H,
                        }
                    )
                    remaining = DETAIL_CONTENT_H - used
                    available = int((remaining - 5) // DETAIL_LEADING)
                take = min(available, len(lines) - offset)
                chunk = lines[offset : offset + take]
                height = len(chunk) * DETAIL_LEADING + 5
                add_block(
                    {
                        "kind": "entry",
                        "lines": chunk,
                        "height": height,
                    }
                )
                offset += take
                if offset < len(lines):
                    advance_column()
                    add_block(
                        {
                            "kind": "section",
                            "title": f"{section_title}（续）",
                            "height": DETAIL_SECTION_H,
                        }
                    )
    return pages


def _detail_page_header(
    c: canvas.Canvas,
    registry: dict[str, Any],
    card: dict[str, Any],
    card_index: int,
    card_page_index: int,
    card_page_count: int,
    page_no: int,
) -> None:
    identity = card["identity"]
    classification = card["classification"]
    family = identity["family_code"]
    accent = FAMILY_COLORS[family]

    c.setPageSize((DETAIL_PAGE_W, DETAIL_PAGE_H))
    c.setFillColor(PAPER)
    c.rect(0, 0, DETAIL_PAGE_W, DETAIL_PAGE_H, fill=1, stroke=0)
    c.setFillColor(accent)
    c.roundRect(DETAIL_MARGIN, DETAIL_PAGE_H - 40, 150, 19, 8, fill=1, stroke=0)
    _draw_centered(
        c,
        f"{family} · {FAMILY_LABELS[family]}",
        DETAIL_MARGIN + 75,
        DETAIL_PAGE_H - 34,
        font=FONT_HEAD,
        size=8,
        color=white,
    )
    c.setFillColor(INK)
    c.setFont(FONT_HEAD, 18)
    marker = "实验卡 ID:" if card_page_index == 1 else "实验卡续页:"
    c.drawString(
        DETAIL_MARGIN,
        DETAIL_PAGE_H - 76,
        f"{marker} {identity['experiment_id']}",
    )
    c.setFont(FONT_HEAD, 8.5)
    c.setFillColor(MUTED)
    c.drawRightString(
        DETAIL_PAGE_W - DETAIL_MARGIN,
        DETAIL_PAGE_H - 33,
        f"第 {card_index:03d} / 106 张 · 本卡 {card_page_index} / {card_page_count} 页",
    )
    chips = [
        f"target {classification['target_class']}",
        f"current {classification['computed_class']}",
        f"action {card['action']['profile_id']}",
        f"data {card['data']['profile_id']}",
        f"venue {card['venue']['execution_venue']}",
        "AUTHORITY NONE",
    ]
    x = DETAIL_MARGIN
    for index, chip in enumerate(chips):
        chip_color = RED if index in {1, 5} else accent
        width = min(
            225,
            max(
                92,
                pdfmetrics.stringWidth(chip, FONT_MONO_BOLD, 7.2) + 18,
            ),
        )
        c.setFillColor(Color(chip_color.red, chip_color.green, chip_color.blue, alpha=0.12))
        c.setStrokeColor(chip_color)
        c.roundRect(x, DETAIL_PAGE_H - 106, width, 20, 8, fill=1, stroke=1)
        _draw_centered(
            c,
            chip,
            x + width / 2,
            DETAIL_PAGE_H - 99,
            font=FONT_MONO_BOLD,
            size=7.2,
            color=chip_color,
        )
        x += width + 8
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.8)
    c.line(
        DETAIL_MARGIN,
        DETAIL_PAGE_H - 119,
        DETAIL_PAGE_W - DETAIL_MARGIN,
        DETAIL_PAGE_H - 119,
    )
    if card_page_index == 1:
        bookmark = f"card-{identity['experiment_id']}"
        c.bookmarkPage(bookmark)
        c.addOutlineEntry(
            identity["experiment_id"],
            bookmark,
            level=0,
            closed=False,
        )


def _detail_page_footer(
    c: canvas.Canvas,
    registry: dict[str, Any],
    card: dict[str, Any],
    page_no: int,
) -> None:
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(DETAIL_MARGIN, 31, DETAIL_PAGE_W - DETAIL_MARGIN, 31)
    c.setFont(FONT_MONO, 6.8)
    c.setFillColor(MUTED)
    c.drawString(
        DETAIL_MARGIN,
        17,
        f"registry_sha256 {registry['registry_sha256']}",
    )
    c.drawCentredString(
        DETAIL_PAGE_W / 2,
        17,
        f"definition_sha256 {card['definition_sha256']}",
    )
    c.drawRightString(
        DETAIL_PAGE_W - DETAIL_MARGIN,
        17,
        f"NO AUTHORITY · {page_no} / {TOTAL_PAGES}",
    )


def _render_detail_card(
    c: canvas.Canvas,
    registry: dict[str, Any],
    card: dict[str, Any],
    card_index: int,
    page_layouts: list[list[list[dict[str, Any]]]],
    first_page_no: int,
) -> int:
    page_no = first_page_no
    accent = FAMILY_COLORS[card["identity"]["family_code"]]
    for card_page_index, page_layout in enumerate(page_layouts, 1):
        _detail_page_header(
            c,
            registry,
            card,
            card_index,
            card_page_index,
            len(page_layouts),
            page_no,
        )
        for column_index, blocks in enumerate(page_layout):
            x = DETAIL_MARGIN + column_index * (DETAIL_COL_W + DETAIL_COL_GAP)
            _round_rect(
                c,
                x,
                DETAIL_BOTTOM,
                DETAIL_COL_W,
                DETAIL_CONTENT_H,
                fill=white,
                stroke=BORDER,
                radius=8,
                line_width=0.6,
            )
            y = DETAIL_TOP
            for block in blocks:
                if block["kind"] == "section":
                    c.setFillColor(
                        Color(accent.red, accent.green, accent.blue, alpha=0.12)
                    )
                    c.roundRect(
                        x + 6,
                        y - DETAIL_SECTION_H + 3,
                        DETAIL_COL_W - 12,
                        DETAIL_SECTION_H - 5,
                        5,
                        fill=1,
                        stroke=0,
                    )
                    c.setFillColor(accent)
                    c.setFont(FONT_HEAD, 9)
                    c.drawString(x + 13, y - 11, block["title"])
                else:
                    c.setFillColor(accent)
                    c.circle(x + 11, y - 7, 1.7, fill=1, stroke=0)
                    c.setFillColor(INK)
                    c.setFont(FONT_BODY, DETAIL_FONT_SIZE)
                    line_y = y - 10
                    for line in block["lines"]:
                        c.drawString(x + 17, line_y, line)
                        line_y -= DETAIL_LEADING
                y -= block["height"]
            if y < DETAIL_BOTTOM - 0.1:
                raise AssertionError(
                    f"detail layout overflow: "
                    f"{card['identity']['experiment_id']} page "
                    f"{card_page_index} column {column_index + 1}"
                )
        _detail_page_footer(c, registry, card, page_no)
        c.showPage()
        page_no += 1
    return page_no


def build_pdf(registry: dict[str, Any], output: Path = OUTPUT_PATH) -> Path:
    global TOTAL_PAGES
    _register_fonts()
    if len(registry["experiments"]) != 106:
        raise AssertionError(
            f"expected 106 experiment cards, found {len(registry['experiments'])}"
        )
    detail_payloads = []
    for card in registry["experiments"]:
        sections = _card_detail_sections(registry, card)
        layouts = _layout_detail_sections(sections)
        detail_payloads.append((card, layouts))
    TOTAL_PAGES = SUMMARY_PAGES + sum(
        len(layouts)
        for _, layouts in detail_payloads
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(
        str(output),
        pagesize=(PAGE_W, PAGE_H),
        pageCompression=1,
        invariant=1,
    )
    c.setTitle("DeepResearch V3 统一实验变现 Registry 完整可视化版")
    c.setSubject("管理摘要与 106 张完整实验卡：从假设、动作和数据到真实 PnL")
    c.setAuthor("Deep03 Experiment Registry")
    c.setCreator("tools/render_experiment_registry_pdf.py")

    page_no = 1
    _page_cover(c, registry, page_no)
    page_no += 1
    for renderer in (
        _page_dashboard,
        _page_operating_change,
        _page_card_anatomy,
        _page_families,
        _page_target_vs_current,
        _page_data_truth,
        _page_blockers,
        _page_pnl,
        _page_rfq,
        _page_mve,
        _page_external,
        _page_priority,
        _page_roadmap,
    ):
        renderer(c, registry, page_no)
        page_no += 1
    _page_provenance(c, registry, page_no)
    page_no += 1
    for card_index, (card, layouts) in enumerate(detail_payloads, 1):
        page_no = _render_detail_card(
            c,
            registry,
            card,
            card_index,
            layouts,
            page_no,
        )
    if page_no - 1 != TOTAL_PAGES:
        raise AssertionError(f"expected {TOTAL_PAGES} pages, rendered {page_no - 1}")
    c.save()
    return output


def main() -> int:
    registry = _load_registry()
    output = build_pdf(registry)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
