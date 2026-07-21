#!/usr/bin/env python3
"""Read-only strategy playground over recent raw firehose files.

This is deliberately isolated from the production pipeline:
- reads only work/raw/date=... firehose files and dim/latest CSV metadata
- never imports production warehouse loaders
- never touches credentials, network, or orders
- writes nothing unless --out is given, and --out must stay under
  sandbox/research/reports/

All outputs are gross research hints, before fees, queue position, slippage,
inventory risk, and settlement risk.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "work" / "raw"
DIM_ROOT = ROOT / "work" / "warehouse" / "dim" / "latest"
REPORT_ROOT = ROOT / "sandbox" / "research" / "reports"

SPORTS_RE = re.compile(
    r"^KX(MLB|WC|UECL|WNBA|NBA|NFL|NHL|ATP|WTA|ITF|MENWORLDCUP|WOMENWORLDCUP)"
)


def dollars(value) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ts_ms_from_msg(msg: dict, outer: dict) -> int | None:
    val = msg.get("ts_ms")
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    wall_ns = outer.get("recv_wall_ns")
    if wall_ns is not None:
        try:
            return int(wall_ns) // 1_000_000
        except (TypeError, ValueError):
            pass
    ts = msg.get("ts")
    if ts is not None:
        try:
            return int(float(ts) * 1000)
        except (TypeError, ValueError):
            pass
    return None


def fmt_time(ms: int | None) -> str:
    if ms is None:
        return "-"
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


def short(text: str | None, n: int = 82) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "..."


@dataclass
class MarketMeta:
    event_ticker: str
    title: str = ""
    yes_sub_title: str = ""
    no_sub_title: str = ""
    event_structure: str = ""
    status: str = ""


@dataclass
class EventMeta:
    title: str = ""
    category: str = ""
    mutually_exclusive: str = ""


@dataclass
class MarketStats:
    ticker: str
    first_ts: int | None = None
    last_ts: int | None = None
    first_price: float | None = None
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    quote_ts: int | None = None
    quote_updates: int = 0
    trades: int = 0
    contracts: float = 0.0
    yes_takers: int = 0
    no_takers: int = 0
    first_trade_price: float | None = None
    last_trade_price: float | None = None
    vwap_num: float = 0.0
    vwap_den: float = 0.0

    def touch(self, ts_ms: int | None, price: float | None) -> None:
        if ts_ms is not None:
            self.first_ts = ts_ms if self.first_ts is None else min(self.first_ts, ts_ms)
            self.last_ts = ts_ms if self.last_ts is None else max(self.last_ts, ts_ms)
        if price is not None:
            if self.first_price is None:
                self.first_price = price
            self.last_price = price

    def on_ticker(self, msg: dict, ts_ms: int | None) -> None:
        price = dollars(msg.get("price_dollars"))
        self.touch(ts_ms, price)
        bid = dollars(msg.get("yes_bid_dollars"))
        ask = dollars(msg.get("yes_ask_dollars"))
        if bid is not None:
            self.bid = bid
        if ask is not None:
            self.ask = ask
        self.bid_size = dollars(msg.get("yes_bid_size_fp")) or self.bid_size
        self.ask_size = dollars(msg.get("yes_ask_size_fp")) or self.ask_size
        self.quote_ts = ts_ms or self.quote_ts
        self.quote_updates += 1

    def on_trade(self, msg: dict, ts_ms: int | None) -> None:
        px = dollars(msg.get("yes_price_dollars") or msg.get("price_dollars"))
        qty = dollars(msg.get("count_fp") or msg.get("count"))
        qty = qty if qty is not None else 0.0
        self.touch(ts_ms, px)
        self.trades += 1
        self.contracts += qty
        if px is not None:
            if self.first_trade_price is None:
                self.first_trade_price = px
            self.last_trade_price = px
            self.vwap_num += px * qty
            self.vwap_den += qty
        side = (msg.get("taker_side") or msg.get("taker_outcome_side") or "").lower()
        if side == "yes":
            self.yes_takers += 1
        elif side == "no":
            self.no_takers += 1

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None or self.ask <= self.bid:
            return None
        return self.ask - self.bid

    @property
    def vwap(self) -> float | None:
        return self.vwap_num / self.vwap_den if self.vwap_den > 0 else None

    @property
    def flow(self) -> int:
        return self.yes_takers - self.no_takers

    @property
    def move(self) -> float | None:
        first = self.first_trade_price if self.first_trade_price is not None else self.first_price
        last = self.last_trade_price if self.last_trade_price is not None else self.last_price
        if first is None or last is None:
            return None
        return last - first


def raw_files_for_date(date: str, count: int) -> list[Path]:
    day_dir = RAW_ROOT / f"date={date}"
    files = [p for p in day_dir.glob("firehose*.ndjson*") if p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return files[-count:]


def iter_records(paths: Iterable[Path]):
    parse_errors = 0
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    outer = json.loads(line)
                    inner = json.loads(outer.get("raw", "{}"))
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                yield path, line_no, outer, inner
    if parse_errors:
        yield None, parse_errors, None, None


def collect_stats(paths: list[Path], focus_re: re.Pattern | None, include_all: bool):
    stats: dict[str, MarketStats] = {}
    parse_errors = 0
    total_records = 0
    kept_records = 0
    type_counts: dict[str, int] = defaultdict(int)
    min_ts = None
    max_ts = None

    for path, line_no, outer, inner in iter_records(paths):
        if path is None:
            parse_errors += line_no
            continue
        total_records += 1
        kind = inner.get("type") or outer.get("channel") or "?"
        type_counts[kind] += 1
        msg = inner.get("msg") or {}
        ticker = msg.get("market_ticker") or outer.get("source_ticker")
        if not ticker:
            continue
        if not include_all and not SPORTS_RE.search(ticker):
            continue
        haystack = ticker
        if focus_re and not focus_re.search(haystack):
            continue
        ts_ms = ts_ms_from_msg(msg, outer)
        if ts_ms is not None:
            min_ts = ts_ms if min_ts is None else min(min_ts, ts_ms)
            max_ts = ts_ms if max_ts is None else max(max_ts, ts_ms)
        s = stats.setdefault(ticker, MarketStats(ticker=ticker))
        if kind == "ticker":
            s.on_ticker(msg, ts_ms)
        elif kind == "trade":
            s.on_trade(msg, ts_ms)
        else:
            s.touch(ts_ms, None)
        kept_records += 1

    return {
        "stats": stats,
        "parse_errors": parse_errors,
        "total_records": total_records,
        "kept_records": kept_records,
        "type_counts": dict(type_counts),
        "min_ts": min_ts,
        "max_ts": max_ts,
    }


def load_metadata(tickers: set[str]):
    market_meta: dict[str, MarketMeta] = {}
    event_counts: dict[str, int] = defaultdict(int)
    event_tickers: set[str] = set()
    markets_path = DIM_ROOT / "markets.csv"
    if markets_path.exists():
        with markets_path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                event_ticker = row.get("event_ticker") or ""
                ticker = row.get("ticker") or ""
                if event_ticker:
                    event_counts[event_ticker] += 1
                if ticker in tickers:
                    meta = MarketMeta(
                        event_ticker=event_ticker or derive_event_ticker(ticker),
                        title=row.get("title") or "",
                        yes_sub_title=row.get("yes_sub_title") or "",
                        no_sub_title=row.get("no_sub_title") or "",
                        event_structure=row.get("event_structure") or "",
                        status=row.get("status") or "",
                    )
                    market_meta[ticker] = meta
                    event_tickers.add(meta.event_ticker)
    for ticker in tickers:
        if ticker not in market_meta:
            event_ticker = derive_event_ticker(ticker)
            market_meta[ticker] = MarketMeta(event_ticker=event_ticker)
            event_tickers.add(event_ticker)

    event_meta: dict[str, EventMeta] = {}
    events_path = DIM_ROOT / "events.csv"
    if events_path.exists():
        with events_path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                event_ticker = row.get("event_ticker") or ""
                if event_ticker in event_tickers:
                    event_meta[event_ticker] = EventMeta(
                        title=row.get("title") or "",
                        category=row.get("category") or "",
                        mutually_exclusive=row.get("mutually_exclusive") or "",
                    )
    return market_meta, event_meta, event_counts


def derive_event_ticker(ticker: str) -> str:
    if "-" not in ticker:
        return ticker
    return ticker.rsplit("-", 1)[0]


def event_for(ticker: str, market_meta: dict[str, MarketMeta]) -> str:
    return market_meta.get(ticker, MarketMeta(derive_event_ticker(ticker))).event_ticker


def meta_title(ticker: str, market_meta, event_meta) -> str:
    mm = market_meta.get(ticker)
    ev = event_meta.get(mm.event_ticker if mm else derive_event_ticker(ticker))
    parts = []
    if ev and ev.title:
        parts.append(ev.title)
    if mm and mm.title and mm.title not in parts:
        parts.append(mm.title)
    if mm and mm.yes_sub_title and mm.yes_sub_title not in parts:
        parts.append(mm.yes_sub_title)
    return " | ".join(parts)


def market_making_candidates(stats, market_meta, event_meta, top):
    rows = []
    for ticker, s in stats.items():
        spread = s.spread
        if spread is None or spread <= 0 or spread > 0.25:
            continue
        if s.bid is None or s.ask is None or s.bid <= 0.01 or s.ask >= 0.99:
            continue
        opp = s.trades if s.trades else max(1, s.quote_updates // 20)
        volume = s.contracts if s.contracts > 0 else max(1.0, (s.bid_size or 0) + (s.ask_size or 0))
        depth = min(s.bid_size or 0, s.ask_size or 0)
        score = spread * opp * math.sqrt(max(volume, 1.0)) * math.sqrt(max(depth, 1.0))
        rows.append((score, ticker, s, meta_title(ticker, market_meta, event_meta)))
    rows.sort(reverse=True, key=lambda x: x[0])
    return rows[:top]


def pair_parity(stats, market_meta, event_meta, event_counts, top, show_incomplete):
    groups: dict[str, list[tuple[str, MarketStats]]] = defaultdict(list)
    for ticker, s in stats.items():
        if s.bid is None or s.ask is None:
            continue
        groups[event_for(ticker, market_meta)].append((ticker, s))
    rows = []
    for ev, items in groups.items():
        if len(items) < 2 or len(items) > 5:
            continue
        ask_sum = sum(s.ask for _, s in items if s.ask is not None)
        bid_sum = sum(s.bid for _, s in items if s.bid is not None)
        expected = event_counts.get(ev, 0)
        event_info = event_meta.get(ev, EventMeta())
        mutually_exclusive = event_info.mutually_exclusive.lower() == "true"
        complete = expected == len(items) and expected in (2, 3) and mutually_exclusive
        if not complete and not show_incomplete:
            continue
        buy_gap = 1.0 - ask_sum
        sell_gap = bid_sum - 1.0
        edge = max(buy_gap, sell_gap)
        if edge <= 0.0005:
            continue
        title = event_info.title
        rows.append((edge, buy_gap, sell_gap, ev, items, complete, expected, title))
    rows.sort(reverse=True, key=lambda x: x[0])
    return rows[:top]


LADDER_WORDS = re.compile(
    r"\b(over|under|total|corners?|goals?|runs?|points?|hits?|"
    r"strikeouts?|rebounds?|assists?|saves?|cards?|sets?|maps?)\b",
    re.IGNORECASE,
)


def ladder_context(ev: str, items, market_meta, event_meta) -> bool:
    title = event_meta.get(ev, EventMeta()).title
    joined = " ".join([title] + [meta_title(t, market_meta, event_meta) for t, _ in items])
    if re.search(r"\bwhen will\b|\bwhich date\b|\bnew team\b", joined, re.IGNORECASE):
        return False
    return bool(LADDER_WORDS.search(joined))


def numeric_suffix(ticker: str, prefix: str) -> int | None:
    if not ticker.startswith(prefix + "-"):
        return None
    suffix = ticker[len(prefix) + 1 :]
    return int(suffix) if re.fullmatch(r"\d+", suffix) else None


def ladder_checks(stats, market_meta, event_meta, top):
    groups: dict[str, list[tuple[str, MarketStats]]] = defaultdict(list)
    for ticker, s in stats.items():
        if s.bid is None or s.ask is None:
            continue
        groups[event_for(ticker, market_meta)].append((ticker, s))
    rows = []
    for ev, items in groups.items():
        if not ladder_context(ev, items, market_meta, event_meta):
            continue
        numeric = []
        for ticker, s in items:
            val = numeric_suffix(ticker, ev)
            if val is not None:
                numeric.append((val, ticker, s))
        numeric.sort()
        for (lo_v, lo_t, lo_s), (hi_v, hi_t, hi_s) in zip(numeric, numeric[1:]):
            gap = (hi_s.bid or 0) - (lo_s.ask or 0)
            if gap > 0.002:
                title = event_meta.get(ev, EventMeta()).title
                rows.append((gap, "numeric", ev, lo_v, hi_v, lo_t, hi_t, title))

        by_base: dict[str, list[tuple[int, str, MarketStats]]] = defaultdict(list)
        for ticker, s in items:
            m = re.match(r"^(.*)-(\d+)$", ticker)
            if not m:
                continue
            base, tier_s = m.group(1), m.group(2)
            if base == ev:
                continue
            by_base[base].append((int(tier_s), ticker, s))
        for base, tiers in by_base.items():
            tiers.sort()
            for (lo_v, lo_t, lo_s), (hi_v, hi_t, hi_s) in zip(tiers, tiers[1:]):
                gap = (hi_s.bid or 0) - (lo_s.ask or 0)
                if gap > 0.002:
                    title = event_meta.get(ev, EventMeta()).title
                    rows.append((gap, "tier", ev, lo_v, hi_v, lo_t, hi_t, title))
    rows.sort(reverse=True, key=lambda x: x[0])
    return rows[:top]


def largest_moves(stats, market_meta, event_meta, top):
    rows = []
    for ticker, s in stats.items():
        move = s.move
        if move is None:
            continue
        if s.trades < 2 and s.quote_updates < 5:
            continue
        rows.append((abs(move), move, ticker, s, meta_title(ticker, market_meta, event_meta)))
    rows.sort(reverse=True, key=lambda x: x[0])
    return rows[:top]


def line_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["(none)"]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  ".join("-" * w for w in widths))
    for row in rows:
        out.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return out


def render_report(args, paths, collected, market_meta, event_meta, event_counts) -> str:
    stats = collected["stats"]
    lines = []
    lines.append("# Strategy Playground")
    lines.append("")
    lines.append("Research only: no network, no credentials, no orders, no production writes.")
    lines.append("All edge numbers are gross hints before fees, queue position, slippage, and risk.")
    lines.append("")
    lines.append("## Input")
    lines.append(f"- date: {args.date}")
    lines.append(f"- files: {len(paths)} latest raw firehose file(s)")
    for path in paths:
        lines.append(f"  - {path.relative_to(ROOT)}")
    lines.append(f"- raw records scanned: {collected['total_records']:,}")
    lines.append(f"- sports/focus records kept: {collected['kept_records']:,}")
    lines.append(f"- markets kept: {len(stats):,}")
    lines.append(f"- skipped malformed/partial lines: {collected['parse_errors']:,}")
    lines.append(f"- time window: {fmt_time(collected['min_ts'])} -> {fmt_time(collected['max_ts'])}")
    lines.append(f"- message types: {collected['type_counts']}")

    mm = market_making_candidates(stats, market_meta, event_meta, args.top)
    lines.append("")
    lines.append("## Passive MM Candidates")
    lines.extend(
        line_table(
            ["score", "spread", "trades", "contracts", "bid/ask", "flow", "ticker", "title"],
            [
                [
                    f"{score:.1f}",
                    f"{s.spread:.3f}" if s.spread is not None else "-",
                    str(s.trades),
                    f"{s.contracts:.0f}",
                    f"{s.bid:.3f}/{s.ask:.3f}",
                    str(s.flow),
                    ticker[:48],
                    short(title, 64),
                ]
                for score, ticker, s, title in mm
            ],
        )
    )

    parity = pair_parity(
        stats,
        market_meta,
        event_meta,
        event_counts,
        args.top,
        args.show_incomplete_parity,
    )
    lines.append("")
    lines.append("## Pair/Small-Set Parity Checks")
    lines.append("Only complete two/three-outcome events are shown by default.")
    lines.extend(
        line_table(
            ["edge", "ask_sum", "bid_sum", "complete", "seen/expected", "event", "markets", "title"],
            [
                [
                    f"{edge:.3f}",
                    f"{1 - buy_gap:.3f}",
                    f"{1 + sell_gap:.3f}",
                    "yes" if complete else "no",
                    f"{len(items)}/{expected or '?'}",
                    ev[:44],
                    ",".join(t[:16] for t, _ in items),
                    short(title, 54),
                ]
                for edge, buy_gap, sell_gap, ev, items, complete, expected, title in parity
            ],
        )
    )

    ladders = ladder_checks(stats, market_meta, event_meta, args.top)
    lines.append("")
    lines.append("## Ladder Sanity Checks")
    lines.append("These flag monotonic violations only; empty is good.")
    lines.extend(
        line_table(
            ["gap", "kind", "event", "lower", "higher", "lower_ticker", "higher_ticker", "title"],
            [
                [
                    f"{gap:.3f}",
                    kind,
                    ev[:38],
                    str(lo_v),
                    str(hi_v),
                    lo_t[:34],
                    hi_t[:34],
                    short(title, 48),
                ]
                for gap, kind, ev, lo_v, hi_v, lo_t, hi_t, title in ladders
            ],
        )
    )

    moves = largest_moves(stats, market_meta, event_meta, args.top)
    lines.append("")
    lines.append("## Largest Moves In Window")
    lines.extend(
        line_table(
            ["move", "trades", "contracts", "bid/ask", "flow", "ticker", "title"],
            [
                [
                    f"{move:+.3f}",
                    str(s.trades),
                    f"{s.contracts:.0f}",
                    f"{s.bid:.3f}/{s.ask:.3f}" if s.bid is not None and s.ask is not None else "-",
                    str(s.flow),
                    ticker[:48],
                    short(title, 64),
                ]
                for _, move, ticker, s, title in moves
            ],
        )
    )
    lines.append("")
    lines.append("Next sensible test: run a fill simulator only on the top 3-5 candidates, with pessimistic queue assumptions.")
    return "\n".join(lines) + "\n"


def safe_out(path_s: str) -> Path:
    path = Path(path_s).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    report_root = REPORT_ROOT.resolve()
    if report_root not in path.parents and path != report_root:
        raise SystemExit("--out must stay under sandbox/research/reports/")
    return path


def main(argv: list[str]) -> int:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=today, help="UTC date, default today")
    ap.add_argument("--files", type=int, default=2, help="latest raw files to scan")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--focus", default=None, help="regex filter for tickers, e.g. FRAMAR|NICALA")
    ap.add_argument("--all", action="store_true", help="include non-sports tickers too")
    ap.add_argument(
        "--show-incomplete-parity",
        action="store_true",
        help="also show partial event groups; useful for debugging, noisy for strategy",
    )
    ap.add_argument("--out", default=None, help="optional report path under sandbox/research/reports/")
    args = ap.parse_args(argv[1:])

    focus_re = re.compile(args.focus) if args.focus else None
    paths = raw_files_for_date(args.date, args.files)
    if not paths:
        print(f"no raw files found for {args.date}", file=sys.stderr)
        return 1

    collected = collect_stats(paths, focus_re, args.all)
    stats = collected["stats"]
    if not stats:
        print("no matching records found", file=sys.stderr)
        return 1

    market_meta, event_meta, event_counts = load_metadata(set(stats))
    report = render_report(args, paths, collected, market_meta, event_meta, event_counts)
    if args.out:
        out = safe_out(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
