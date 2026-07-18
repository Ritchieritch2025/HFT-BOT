#!/usr/bin/env python3
"""Read-only Kalshi order activity -> Telegram notifications.

This program is deliberately independent from ``tg_alert.py``: order events
are one-shot facts, not alert conditions, so they must never enter the
alert/recovery state machine.  A timer may run this file once per minute.

Only authenticated GETs are made:

* ``/portfolio/orders`` (recent orders plus the complete resting set)
* ``/portfolio/orders/{id}`` (only when a previously-resting order vanishes)
* ``/portfolio/fills`` (overlapping recent window; durable fill backstop)
* ``/portfolio/settlements`` (overlapping recent window)
* ``/markets/{ticker}`` (best-effort human-readable labels only)

The first successful run creates a baseline and sends one compact startup
summary, but never replays historical trade details.  Later runs report new
orders, meaningful order snapshot changes, otherwise-unreported fills, and
settlements in compact Chinese batches.  State is written with mode 0600 and
atomically replaced.  New facts are first committed to a durable pending queue
and removed only after Telegram accepts the batch, so a delivery failure cannot
lose them.  (As with any non-transactional external message API, a process
crash after Telegram accepts but before the final state write can cause one
at-least-once duplicate; preferring that is safer than silently losing a
trade.)

No live-order or mutation module is imported.
"""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse
import zoneinfo

import balance_probe
import tg_common as tg


STATE_VERSION = 1
DEFAULT_STATE_NAME = "order_activity_state.json"
MAX_PAGES = 50
DEFAULT_LOOKBACK_S = 5 * 60
DEFAULT_CATCHUP_SPAN_S = 5 * 60
DEFAULT_RETENTION_DAYS = 30
DEFAULT_BATCH_CHARS = 3500
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_MARKET_LOOKUPS_PER_POLL = 3
MARKET_LOOKUP_TIMEOUT_S = 2

ORDER_STATUS = {"resting", "canceled", "executed"}
BOOK_SIDE = {"bid", "ask"}
OUTCOME_SIDE = {"yes", "no"}
LEGACY_SIDE = {"yes", "no"}
ACTION = {"buy", "sell"}
ORDER_TYPE = {"limit", "market"}

# last_update_time itself is intentionally excluded: timestamp-only API
# rewrites must not create notification noise.
ORDER_CHANGE_FIELDS = (
    "status",
    "fill_count_e2",
    "remaining_count_e2",
    "initial_count_e2",
    "taker_fees_e6",
    "maker_fees_e6",
    "taker_fill_cost_e6",
    "maker_fill_cost_e6",
    "yes_price_e6",
    "no_price_e6",
    "outcome_side",
    "book_side",
    "order_type",
)

_DEC_RE = re.compile(r"^(-)?([0-9]+)(?:\.([0-9]+))?$")


class OrderActivityError(RuntimeError):
    """Transport, schema, pagination, or state failure; poll fails closed."""


def _conf_int(conf, key, default, minimum=1):
    try:
        value = int(conf.get(key, default))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, value)


def _scaled(value, places, field, nonnegative=False):
    """Parse an ASCII fixed-point string to an integer without floats."""
    if not isinstance(value, str):
        raise OrderActivityError("%s must be a fixed-point string" % field)
    match = _DEC_RE.fullmatch(value)
    if not match:
        raise OrderActivityError("%s is malformed" % field)
    sign, whole, fraction = match.group(1), match.group(2), match.group(3) or ""
    if any(ch != "0" for ch in fraction[places:]):
        raise OrderActivityError("%s has more than %d decimal places" %
                                 (field, places))
    number = int(whole) * (10 ** places)
    kept = fraction[:places].ljust(places, "0")
    if kept:
        number += int(kept)
    if sign:
        number = -number
    if nonnegative and number < 0:
        raise OrderActivityError("%s must be nonnegative" % field)
    return number


def _required_string(obj, key, enum=None):
    value = obj.get(key)
    if not isinstance(value, str) or not value or \
            (enum is not None and value not in enum):
        raise OrderActivityError("invalid %s" % key)
    return value


def _optional_string(obj, key, enum=None):
    value = obj.get(key)
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or \
            (enum is not None and value not in enum):
        raise OrderActivityError("invalid %s" % key)
    return value


def _integer(obj, key):
    value = obj.get(key)
    if type(value) is not int:  # bool is not an acceptable integer here
        raise OrderActivityError("invalid %s" % key)
    return value


def normalize_order(raw):
    """Validate and retain only fields needed for diffing/display."""
    if not isinstance(raw, dict):
        raise OrderActivityError("order record is not an object")
    subaccount = raw.get("subaccount_number")
    if subaccount is not None and type(subaccount) is not int:
        raise OrderActivityError("invalid subaccount_number")
    return {
        "order_id": _required_string(raw, "order_id"),
        "client_order_id": _optional_string(raw, "client_order_id"),
        "subaccount_number": subaccount,
        "ticker": _required_string(raw, "ticker"),
        "status": _required_string(raw, "status", ORDER_STATUS),
        "outcome_side": _required_string(raw, "outcome_side", OUTCOME_SIDE),
        "book_side": _required_string(raw, "book_side", BOOK_SIDE),
        # Deprecated fields remain useful for the clearest human direction;
        # tolerate their eventual removal and fall back to canonical fields.
        "legacy_side": _optional_string(raw, "side", LEGACY_SIDE),
        "action": _optional_string(raw, "action", ACTION),
        "order_type": _required_string(raw, "type", ORDER_TYPE),
        "yes_price_e6": _scaled(raw.get("yes_price_dollars"), 6,
                                  "yes_price_dollars", True),
        "no_price_e6": _scaled(raw.get("no_price_dollars"), 6,
                                 "no_price_dollars", True),
        "fill_count_e2": _scaled(raw.get("fill_count_fp"), 2,
                                   "fill_count_fp", True),
        "remaining_count_e2": _scaled(raw.get("remaining_count_fp"), 2,
                                        "remaining_count_fp", True),
        "initial_count_e2": _scaled(raw.get("initial_count_fp"), 2,
                                      "initial_count_fp", True),
        "taker_fees_e6": _scaled(raw.get("taker_fees_dollars"), 6,
                                   "taker_fees_dollars", True),
        "maker_fees_e6": _scaled(raw.get("maker_fees_dollars"), 6,
                                   "maker_fees_dollars", True),
        "taker_fill_cost_e6": _scaled(raw.get("taker_fill_cost_dollars"), 6,
                                        "taker_fill_cost_dollars", True),
        "maker_fill_cost_e6": _scaled(raw.get("maker_fill_cost_dollars"), 6,
                                        "maker_fill_cost_dollars", True),
        "created_time": _optional_string(raw, "created_time"),
        "last_update_time": _optional_string(raw, "last_update_time"),
    }


def normalize_fill(raw):
    """OpenAPI Fill -> minimal durable identity and display fields."""
    if not isinstance(raw, dict):
        raise OrderActivityError("fill record is not an object")
    taker = raw.get("is_taker")
    if type(taker) is not bool:
        raise OrderActivityError("invalid is_taker")
    # ticker is canonical; market_ticker is only a legacy alias.
    ticker = raw.get("ticker") or raw.get("market_ticker")
    if not isinstance(ticker, str) or not ticker:
        raise OrderActivityError("invalid ticker")
    return {
        "fill_id": _required_string(raw, "fill_id"),
        "order_id": _required_string(raw, "order_id"),
        "ticker": ticker,
        "outcome_side": _required_string(raw, "outcome_side", OUTCOME_SIDE),
        "book_side": _required_string(raw, "book_side", BOOK_SIDE),
        "legacy_side": _optional_string(raw, "side", LEGACY_SIDE),
        "action": _optional_string(raw, "action", ACTION),
        "count_e2": _scaled(raw.get("count_fp"), 2, "count_fp", True),
        "yes_price_e6": _scaled(raw.get("yes_price_dollars"), 6,
                                  "yes_price_dollars", True),
        "no_price_e6": _scaled(raw.get("no_price_dollars"), 6,
                                 "no_price_dollars", True),
        "fee_e6": _scaled(raw.get("fee_cost"), 6, "fee_cost", True),
        "is_taker": taker,
        "created_time": _optional_string(raw, "created_time"),
        "ts": raw.get("ts") if type(raw.get("ts")) is int else None,
    }


def normalize_settlement(raw):
    """OpenAPI Settlement -> minimal immutable settlement snapshot."""
    if not isinstance(raw, dict):
        raise OrderActivityError("settlement record is not an object")
    result = _required_string(raw, "market_result", {"yes", "no", "scalar"})
    value = raw.get("value")
    if value is not None and type(value) is not int:
        raise OrderActivityError("invalid value")
    return {
        "ticker": _required_string(raw, "ticker"),
        "event_ticker": _required_string(raw, "event_ticker"),
        "market_result": result,
        "yes_count_e2": _scaled(raw.get("yes_count_fp"), 2,
                                  "yes_count_fp", True),
        "yes_total_cost_e6": _scaled(raw.get("yes_total_cost_dollars"), 6,
                                       "yes_total_cost_dollars", True),
        "no_count_e2": _scaled(raw.get("no_count_fp"), 2,
                                 "no_count_fp", True),
        "no_total_cost_e6": _scaled(raw.get("no_total_cost_dollars"), 6,
                                      "no_total_cost_dollars", True),
        "revenue_cents": _integer(raw, "revenue"),
        "fee_e6": _scaled(raw.get("fee_cost"), 6, "fee_cost", True),
        "settled_time": _required_string(raw, "settled_time"),
        "value": value,
        "subaccount_number": raw.get("subaccount_number")
            if type(raw.get("subaccount_number")) is int else None,
    }


def normalize_market(raw, expected_ticker):
    """Keep only human-facing metadata from GetMarketResponse.market."""
    if not isinstance(raw, dict):
        raise OrderActivityError("market record is not an object")
    ticker = _required_string(raw, "ticker")
    if ticker != expected_ticker:
        raise OrderActivityError("market lookup returned mismatched ticker")
    title = raw.get("title") or raw.get("subtitle") or ""
    yes_title = raw.get("yes_sub_title") or ""
    no_title = raw.get("no_sub_title") or ""
    if not isinstance(title, str) or not isinstance(yes_title, str) or \
            not isinstance(no_title, str):
        raise OrderActivityError("invalid market title fields")
    event_ticker = raw.get("event_ticker") or ""
    if not isinstance(event_ticker, str):
        raise OrderActivityError("invalid market event_ticker")
    # API-owned labels are untrusted display data.  Bound them so one unusual
    # market cannot exceed Telegram's message limit or crowd out trade facts.
    return {"ticker": ticker, "event_ticker": event_ticker,
            "title": title.strip()[:200],
            "yes_sub_title": yes_title.strip()[:120],
            "no_sub_title": no_title.strip()[:120]}


def _with_query(path, pairs):
    return path + "?" + urllib.parse.urlencode(pairs)


class KalshiReader:
    """Authenticated GET-only reader using the proven balance probe signer."""

    def __init__(self, key_id, key_path, get_fn=None, max_pages=MAX_PAGES):
        if not key_id or not key_path:
            raise OrderActivityError("KALSHI credentials are unset")
        self.key_id = key_id
        self.key_path = key_path
        self._uses_default_get = get_fn is None
        self.get_fn = get_fn or balance_probe._get
        self.max_pages = max_pages

    def _get(self, path, timeout=None):
        try:
            if timeout is not None and self._uses_default_get:
                body = self.get_fn(path, self.key_id, self.key_path,
                                   timeout=timeout)
            else:
                body = self.get_fn(path, self.key_id, self.key_path)
        except Exception as exc:
            raise OrderActivityError("GET %s failed: %s" %
                                     (path.split("?", 1)[0], str(exc)[:160])) \
                from None
        if not isinstance(body, dict):
            raise OrderActivityError("GET %s returned a non-object" %
                                     path.split("?", 1)[0])
        return body

    def _paged(self, path, list_key, cursor_required=True):
        records = []
        cursor = ""
        seen_cursors = set()
        for _page in range(self.max_pages):
            pairs = []
            if "?" in path:
                base, raw_query = path.split("?", 1)
                pairs.extend(urllib.parse.parse_qsl(raw_query,
                                                    keep_blank_values=True))
            else:
                base = path
            if cursor:
                pairs.append(("cursor", cursor))
            body = self._get(_with_query(base, pairs))
            page_records = body.get(list_key)
            if not isinstance(page_records, list):
                raise OrderActivityError("%s missing %s array" %
                                         (base, list_key))
            records.extend(page_records)
            if "cursor" not in body and not cursor_required:
                return records
            if "cursor" not in body or not isinstance(body["cursor"], str):
                raise OrderActivityError("%s has invalid cursor" % base)
            next_cursor = body["cursor"]
            if not next_cursor:
                return records
            if next_cursor in seen_cursors:
                raise OrderActivityError("%s repeated pagination cursor" % base)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise OrderActivityError("%s exceeded %d pages; refusing partial data" %
                                 (path.split("?", 1)[0], self.max_pages))

    def orders(self, min_ts, max_ts):
        """Recent orders of every status, including instant terminal orders."""
        return self._paged(_with_query("/portfolio/orders",
                                       [("limit", 1000),
                                        ("min_ts", int(min_ts)),
                                        ("max_ts", int(max_ts))]), "orders")

    def resting_orders(self):
        """All resting orders; the API promises these are always available."""
        return self._paged(_with_query("/portfolio/orders",
                                       [("status", "resting"),
                                        ("limit", 1000)]), "orders")

    def order(self, order_id):
        """Resolve a disappeared resting order without scanning all history."""
        safe_id = urllib.parse.quote(order_id, safe="")
        body = self._get("/portfolio/orders/%s" % safe_id)
        raw = body.get("order")
        if not isinstance(raw, dict):
            raise OrderActivityError("order lookup missing order object")
        return raw

    def market(self, ticker):
        safe_ticker = urllib.parse.quote(ticker, safe="")
        body = self._get("/markets/%s" % safe_ticker,
                         timeout=MARKET_LOOKUP_TIMEOUT_S)
        raw = body.get("market")
        if not isinstance(raw, dict):
            raise OrderActivityError("market lookup missing market object")
        return raw

    def fills(self, min_ts, max_ts):
        return self._paged(_with_query("/portfolio/fills",
                                       [("limit", 1000),
                                        ("min_ts", int(min_ts)),
                                        ("max_ts", int(max_ts))]), "fills")

    def settlements(self, min_ts, max_ts):
        return self._paged(_with_query("/portfolio/settlements",
                                       [("limit", 1000),
                                        ("min_ts", int(min_ts)),
                                        ("max_ts", int(max_ts))]),
                           "settlements", cursor_required=False)


def _empty_state():
    return {
        "version": STATE_VERSION,
        "bootstrapped": False,
        "last_poll_epoch": 0,
        "next_sequence": 1,
        "orders": {},
        "fills": {},
        "settlements": {},
        "markets": {},
        "pending": [],
    }


def load_state(path):
    if not os.path.exists(path):
        return _empty_state()
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception as exc:
        # Never reinterpret corrupt state as a first-run baseline: that could
        # silently swallow live events.
        raise OrderActivityError("cannot read state: %s" % str(exc)[:160]) \
            from None
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise OrderActivityError("unsupported/corrupt order activity state")
    # A state file is created only by a completed atomic baseline write, so an
    # on-disk non-bootstrapped object is corruption, not permission to swallow
    # current activity as a fresh baseline.
    if state.get("bootstrapped") is not True:
        raise OrderActivityError("state.bootstrapped is invalid")
    if type(state.get("last_poll_epoch")) is not int or \
            state["last_poll_epoch"] < 0:
        raise OrderActivityError("state.last_poll_epoch is invalid")
    for key in ("orders", "fills", "settlements", "markets"):
        if not isinstance(state.get(key), dict):
            raise OrderActivityError("state.%s is invalid" % key)
    if not isinstance(state.get("pending"), list):
        raise OrderActivityError("state.pending is invalid")
    if type(state.get("next_sequence")) is not int or \
            state["next_sequence"] < 1:
        raise OrderActivityError("state.next_sequence is invalid")

    def seen(record, label):
        if not isinstance(record, dict) or \
                type(record.get("_last_seen_epoch")) is not int or \
                record["_last_seen_epoch"] < 0:
            raise OrderActivityError("invalid stored %s" % label)

    order_ints = ("yes_price_e6", "no_price_e6", "fill_count_e2",
                  "remaining_count_e2", "initial_count_e2", "taker_fees_e6",
                  "maker_fees_e6", "taker_fill_cost_e6",
                  "maker_fill_cost_e6")
    for order_id, record in state["orders"].items():
        seen(record, "order")
        if not isinstance(order_id, str) or not order_id or \
                record.get("order_id") != order_id or \
                not isinstance(record.get("ticker"), str) or \
                not isinstance(record.get("client_order_id"), str) or \
                (record.get("subaccount_number") is not None and
                 type(record.get("subaccount_number")) is not int) or \
                record.get("status") not in ORDER_STATUS or \
                record.get("outcome_side") not in OUTCOME_SIDE or \
                record.get("book_side") not in BOOK_SIDE or \
                record.get("legacy_side") not in LEGACY_SIDE | {""} or \
                record.get("action") not in ACTION | {""} or \
                record.get("order_type") not in ORDER_TYPE or \
                any(type(record.get(key)) is not int for key in order_ints):
            raise OrderActivityError("invalid stored order")
        if not isinstance(record.get("created_time"), str) or \
                not isinstance(record.get("last_update_time"), str):
            raise OrderActivityError("invalid stored order timestamps")

    for fill_id, record in state["fills"].items():
        seen(record, "fill")
        if not isinstance(fill_id, str) or not fill_id:
            raise OrderActivityError("invalid stored fill id")

    settlement_ints = ("yes_count_e2", "yes_total_cost_e6", "no_count_e2",
                       "no_total_cost_e6", "revenue_cents", "fee_e6")
    for key, record in state["settlements"].items():
        seen(record, "settlement")
        if not isinstance(key, str) or not key or \
                not isinstance(record.get("ticker"), str) or \
                not isinstance(record.get("event_ticker"), str) or \
                record.get("market_result") not in {"yes", "no", "scalar"} or \
                not isinstance(record.get("settled_time"), str) or \
                any(type(record.get(name)) is not int
                    for name in settlement_ints):
            raise OrderActivityError("invalid stored settlement")

    for ticker, record in state["markets"].items():
        seen(record, "market")
        if not isinstance(ticker, str) or not ticker or \
                record.get("ticker") != ticker or \
                any(not isinstance(record.get(name), str)
                    for name in ("event_ticker", "title", "yes_sub_title",
                                 "no_sub_title")):
            raise OrderActivityError("invalid stored market")

    pending_ids, pending_sequences = set(), set()
    for event in state["pending"]:
        if not isinstance(event, dict) or \
                not isinstance(event.get("id"), str) or not event["id"] or \
                type(event.get("sequence")) is not int or \
                event["sequence"] < 1 or \
                type(event.get("created_epoch")) is not int or \
                event["created_epoch"] < 0 or \
                not isinstance(event.get("text"), str) or not event["text"]:
            raise OrderActivityError("invalid pending order event")
        if event["id"] in pending_ids or \
                event["sequence"] in pending_sequences:
            raise OrderActivityError("duplicate pending order event")
        pending_ids.add(event["id"])
        pending_sequences.add(event["sequence"])
    if pending_sequences and state["next_sequence"] <= max(pending_sequences):
        raise OrderActivityError("state.next_sequence does not follow pending")
    return state


def write_state_atomic(path, state):
    """Durable, atomic JSON replacement; never exposes trade state broadly."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                                     suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support directory fsync; os.replace is
            # still atomic there.
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _event_id(kind, stable_id, payload):
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return "%s:%s:%s" % (kind, stable_id, digest[:20])


def _settlement_key(item):
    stable = [item["ticker"], item["settled_time"],
              item.get("subaccount_number")]
    return hashlib.sha256(_canonical(stable).encode("utf-8")).hexdigest()[:32]


def _fmt_scaled(value, places):
    sign = "-" if value < 0 else ""
    value = abs(value)
    scale = 10 ** places
    whole, fraction = divmod(value, scale)
    if not fraction:
        return "%s%d" % (sign, whole)
    frac = ("%0*d" % (places, fraction)).rstrip("0")
    return "%s%d.%s" % (sign, whole, frac)


def _money(value_e6):
    return "$" + _fmt_scaled(value_e6, 6)


def _signed_money(value_e6):
    sign = "+" if value_e6 > 0 else "-" if value_e6 < 0 else ""
    return sign + "$" + _fmt_scaled(abs(value_e6), 6)


def _count(value_e2):
    return _fmt_scaled(value_e2, 2)


def _status(value):
    return {"resting": "挂单", "canceled": "撤销",
            "executed": "完成"}.get(value, value)


def _direction(item):
    action, side = item.get("action"), item.get("legacy_side")
    if action and side:
        return "%s %s" % ({"buy": "买", "sell": "卖"}[action],
                            side.upper())
    # When either deprecated field disappears, do not mix half a legacy pair
    # with canonical vocabulary; canonical outcome is direction, not action.
    return "%s 方向" % str(item.get("outcome_side", "?")).upper()


def _price(item):
    if item.get("action") and item.get("legacy_side"):
        return item["no_price_e6"] if item["legacy_side"] == "no" \
            else item["yes_price_e6"]
    # Canonical book prices are represented on the YES book.
    return item["yes_price_e6"]


def _short_order_id(order_id):
    return order_id[:10]


def _timezone(name):
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tg.log("invalid ORDER_ACTIVITY_TIMEZONE %r; using UTC" % name)
        return datetime.timezone.utc


def _parse_time(value):
    try:
        if type(value) is int:
            return datetime.datetime.fromtimestamp(value,
                                                   datetime.timezone.utc)
        elif isinstance(value, str) and value:
            iso = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed
        else:
            return None
    except (OverflowError, OSError, ValueError):
        return None


def _fmt_time(value, time_zone):
    """ISO-8601/Unix seconds -> operator-local timestamp, never raises."""
    parsed = _parse_time(value)
    if parsed is None:
        return str(value) if value else "未知"
    return parsed.astimezone(time_zone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _elapsed_text(start, end):
    """Compact elapsed time; invalid or materially negative spans are omitted."""
    start_time = _parse_time(start)
    end_time = (_parse_time(end) if not isinstance(end, (int, float)) else
                datetime.datetime.fromtimestamp(end, datetime.timezone.utc))
    if start_time is None or end_time is None:
        return ""
    seconds = (end_time - start_time).total_seconds()
    if seconds < -2:
        return ""
    seconds = max(0.0, seconds)
    if seconds < 1:
        return "<1s"
    if seconds < 10:
        return ("%.1fs" % seconds).replace(".0s", "s")
    rounded = int(round(seconds))
    if rounded < 60:
        return "%ds" % rounded
    if rounded < 3600:
        minutes, secs = divmod(rounded, 60)
        return "%dm%02ds" % (minutes, secs)
    hours, remainder = divmod(rounded, 3600)
    minutes = remainder // 60
    return "%dh%02dm" % (hours, minutes)


def _observation_suffix(event_time, now_epoch):
    elapsed = _elapsed_text(event_time, now_epoch)
    return "｜观测 %s" % elapsed if elapsed else ""


def _market_heading(ticker, market):
    if market and market.get("title"):
        return market["title"]
    return ticker


def _market_context(ticker, market):
    parts = []
    if market and market.get("yes_sub_title"):
        parts.append("YES选项：%s" % market["yes_sub_title"])
    if market and market.get("no_sub_title"):
        parts.append("NO选项：%s" % market["no_sub_title"])
    parts.append(ticker)
    return " · ".join(parts)


def _market_label(ticker, market, limit=48):
    """One-line, bounded market identity for the compact Telegram stream."""
    title = (market or {}).get("title", "")
    yes = (market or {}).get("yes_sub_title", "")
    no = (market or {}).get("no_sub_title", "")
    if yes and no and yes.casefold() != no.casefold():
        value = "%s / %s" % (yes, no)
    else:
        value = title or yes or no or ticker
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[:limit - 1].rstrip() + "…"
    return value


def _format_new_order(order, market, time_zone, now_epoch=None):
    text = "🆕 下单｜%s｜%s %s @ %s｜%s" % (
        _market_label(order["ticker"], market), _direction(order),
        _count(order["initial_count_e2"]), _money(_price(order)),
        _status(order["status"]))
    return text + _observation_suffix(order.get("created_time"), now_epoch)


def _format_order_change(before, after, market, time_zone, now_epoch=None):
    market_label = _market_label(after["ticker"], market)
    direction = _direction(after)
    filled = _count(after["fill_count_e2"])
    total = _count(after["initial_count_e2"])
    delta = after["fill_count_e2"] - before["fill_count_e2"]
    if after["status"] == "canceled":
        text = "❌ 撤单｜%s｜%s｜已成 %s/%s" % (
            market_label, direction, filled, total)
    elif after["status"] == "executed":
        text = "✅ 完成｜%s｜%s｜%s/%s" % (
            market_label, direction, filled, total)
    elif delta > 0:
        text = "🟡 部成｜%s｜%s｜+%s（共 %s/%s）" % (
            market_label, direction, _count(delta), filled, total)
    else:
        changes = []
        if _price(before) != _price(after):
            changes.append("%s→%s" % (_money(_price(before)),
                                       _money(_price(after))))
        if before["initial_count_e2"] != after["initial_count_e2"]:
            changes.append("%s→%s 张" %
                           (_count(before["initial_count_e2"]), total))
        if before["status"] != after["status"]:
            changes.append(_status(after["status"]))
        if not changes:
            changes.append("剩 %s" % _count(after["remaining_count_e2"]))
        text = "🔄 改单｜%s｜%s｜%s" % (
            market_label, direction, " / ".join(changes))
    event_time = after.get("last_update_time") or after.get("created_time")
    return text + _observation_suffix(event_time, now_epoch)


def _format_fills(fills, market, time_zone, order=None, now_epoch=None):
    first = fills[0]
    total_count = sum(item["count_e2"] for item in fills)
    weighted = sum(_price(item) * item["count_e2"] for item in fills)
    average = ((weighted + total_count // 2) // total_count
               if total_count else 0)
    text = "💱 成交｜%s｜%s %s @ %s" % (
        _market_label(first["ticker"], market), _direction(first),
        _count(total_count), _money(average))
    fill_times = [(_parse_time(item.get("created_time") or item.get("ts")),
                   item.get("created_time") or item.get("ts"))
                  for item in fills]
    fill_times = [item for item in fill_times if item[0] is not None]
    if order and fill_times:
        first_fill_raw = min(fill_times, key=lambda item: item[0])[1]
        speed = _elapsed_text(order.get("created_time"), first_fill_raw)
        if speed:
            text += "｜成交耗时 %s" % speed
    if fill_times:
        latest_fill_raw = max(fill_times, key=lambda item: item[0])[1]
        text += _observation_suffix(latest_fill_raw, now_epoch)
    return text


def _format_settlement(item, market, time_zone, updated=False):
    result = item["market_result"].upper()
    total_cost = item["yes_total_cost_e6"] + item["no_total_cost_e6"]
    label = "结算更新" if updated else "结算"
    if item["market_result"] == "scalar" and item.get("value") is not None:
        result += " %s" % _money(item["value"] * 10000)
    net = item["revenue_cents"] * 10000 - total_cost - item["fee_e6"]
    return "🏁 %s｜%s｜%s｜净 %s" % (
        label, _market_label(item["ticker"], market), result,
        _signed_money(net))


def _changed(before, after):
    direct = ("status", "fill_count_e2", "remaining_count_e2",
              "initial_count_e2", "yes_price_e6", "no_price_e6",
              "outcome_side", "book_side", "order_type")
    return any(before.get(field) != after.get(field) for field in direct)


def _validate_order_transition(before, after):
    """Reject stale/regressive snapshots instead of notifying false reversals."""
    for field in ("order_id", "ticker", "client_order_id",
                  "subaccount_number"):
        if before.get(field) != after.get(field):
            raise OrderActivityError("order immutable %s changed" % field)
    if after["fill_count_e2"] < before["fill_count_e2"]:
        raise OrderActivityError("order fill_count regressed")
    if before["status"] in {"executed", "canceled"} and \
            after["status"] != before["status"]:
        raise OrderActivityError("terminal order status regressed")
    old_time = _parse_time(before.get("last_update_time") or
                           before.get("created_time"))
    new_time = _parse_time(after.get("last_update_time") or
                           after.get("created_time"))
    if old_time is not None and new_time is not None and new_time < old_time:
        raise OrderActivityError("order update timestamp regressed")


def _append_pending(state, event_id, text, now_epoch):
    if any(item.get("id") == event_id for item in state["pending"]):
        return False
    sequence = state["next_sequence"]
    state["next_sequence"] = sequence + 1
    state["pending"].append({
        "id": event_id,
        "sequence": sequence,
        "created_epoch": int(now_epoch),
        "text": text,
    })
    return True


def _batches(pending, max_chars):
    """Return [(event list, Telegram body)] preserving event order."""
    ordered = sorted(pending, key=lambda item: item.get("sequence", 0))
    batches, current = [], []
    for item in ordered:
        candidate = current + [item]
        body = "\n".join(x["text"] for x in candidate)
        if not current and len(body) > max_chars:
            # Never truncate an event and then acknowledge it as delivered.
            # Keeping it pending makes the formatting defect visible/retriable.
            raise OrderActivityError(
                "single order event exceeds Telegram batch limit")
        if current and len(body) > max_chars:
            ready = "\n".join(x["text"] for x in current)
            batches.append((current, ready))
            current = [item]
        else:
            current = candidate
    if current:
        body = "\n".join(x["text"] for x in current)
        batches.append((current, body))
    return batches


def _deliver_pending(state, state_path, sender, max_chars):
    delivered = 0
    for items, body in _batches(state["pending"], max_chars):
        try:
            accepted = bool(sender(body))
        except Exception as exc:
            tg.log("order activity send error: %s" % str(exc)[:120])
            accepted = False
        if not accepted:
            break
        delivered_ids = {item["id"] for item in items}
        state["pending"] = [item for item in state["pending"]
                            if item.get("id") not in delivered_ids]
        write_state_atomic(state_path, state)
        delivered += len(delivered_ids)
    return delivered


def _dedupe_records(raw_records, normalizer, key_name):
    normalized = {}
    for raw in raw_records:
        item = normalizer(raw)
        key = item[key_name]
        previous = normalized.get(key)
        if previous is not None and previous != item:
            raise OrderActivityError("conflicting duplicate %s %s" %
                                     (key_name, key))
        normalized[key] = item
    return normalized


def _orders_from_sources(reader, recent_raw, resting_raw, old_orders):
    """Merge race-prone list snapshots and resolve ambiguity by order GET.

    The two list calls are sequential, so an order can legitimately change
    between them.  A conflicting duplicate is therefore not corruption: ask
    the single-order read endpoint for the newest authoritative snapshot.
    Previously-resting orders absent from both lists are resolved the same
    way, catching an old order canceled/executed today even if ``min_ts`` on
    the list endpoint is interpreted as creation time.
    """
    candidates = {}
    conflicting = set()
    for raw in list(recent_raw) + list(resting_raw):
        item = normalize_order(raw)
        order_id = item["order_id"]
        previous = candidates.get(order_id)
        if previous is not None and previous != item:
            conflicting.add(order_id)
        candidates[order_id] = item

    missing_resting = {
        order_id for order_id, item in old_orders.items()
        if item.get("status") == "resting" and order_id not in candidates
    }
    for order_id in sorted(conflicting | missing_resting):
        item = normalize_order(reader.order(order_id))
        if item["order_id"] != order_id:
            raise OrderActivityError("order lookup returned mismatched id")
        candidates[order_id] = item
    return candidates


def _prune_unseen(items, current_keys, cutoff_epoch):
    for key in list(items):
        record = items[key]
        if key not in current_keys and \
                int(record.get("_last_seen_epoch", 0)) < cutoff_epoch:
            del items[key]


def _market_for_event(reader, state, ticker, now_epoch, attempted, max_lookups):
    """Best-effort metadata enrichment; trade facts never depend on it."""
    cached = state["markets"].get(ticker)
    if isinstance(cached, dict):
        cached["_last_seen_epoch"] = now_epoch
        return cached
    if ticker in attempted or len(attempted) >= max_lookups:
        return None
    attempted.add(ticker)
    try:
        market = normalize_market(reader.market(ticker), ticker)
    except Exception as exc:
        # Human titles are useful but ancillary.  A market endpoint outage
        # must not delay an order/fill/settlement notification.
        tg.log("market metadata %s unavailable: %s" %
               (ticker, str(exc)[:120]))
        return None
    state["markets"][ticker] = dict(market, _last_seen_epoch=now_epoch)
    return state["markets"][ticker]


def _bootstrap_text(orders, fills, settlements):
    resting = sum(item["status"] == "resting" for item in orders.values())
    return "ℹ️ 订单推送已启用｜订单 %d（挂单 %d）/ 成交 %d / 结算 %d" % (
        len(orders), resting, len(fills), len(settlements))


def poll_once(reader=None, sender=None, state_path=None, now_epoch=None,
              conf=None):
    """Run one fetch/diff/persist/send cycle; dependency-injectable for tests.

    Returns a small operational summary.  API/schema failures raise
    ``OrderActivityError`` without advancing the baseline.
    """
    conf = conf if conf is not None else tg.load_conf()
    state_path = state_path or tg.state_path(DEFAULT_STATE_NAME)
    sender = sender or tg.send
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    lookback_s = _conf_int(conf, "ORDER_ACTIVITY_LOOKBACK_S",
                           DEFAULT_LOOKBACK_S)
    catchup_span_s = _conf_int(conf, "ORDER_ACTIVITY_CATCHUP_SPAN_S",
                               DEFAULT_CATCHUP_SPAN_S)
    retention_s = _conf_int(conf, "ORDER_ACTIVITY_RETENTION_DAYS",
                            DEFAULT_RETENTION_DAYS) * 86400
    max_chars = _conf_int(conf, "ORDER_ACTIVITY_BATCH_CHARS",
                          DEFAULT_BATCH_CHARS, minimum=512)
    time_zone = _timezone(str(conf.get("ORDER_ACTIVITY_TIMEZONE",
                                       DEFAULT_TIMEZONE)))
    max_market_lookups = _conf_int(
        conf, "ORDER_ACTIVITY_MARKET_LOOKUPS_PER_POLL",
        DEFAULT_MARKET_LOOKUPS_PER_POLL)

    state = load_state(state_path)
    delivered = _deliver_pending(state, state_path, sender, max_chars)

    if reader is None:
        # Prefer a dedicated read-only monitoring key when configured; retain
        # the existing account key as a backwards-compatible fallback.
        reader = KalshiReader(
            os.environ.get("KALSHI_MONITOR_API_KEY_ID") or
            os.environ.get("KALSHI_API_KEY_ID"),
            os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PATH") or
            os.environ.get("KALSHI_PRIVATE_KEY_PATH"))

    previous_poll = int(state.get("last_poll_epoch", 0) or 0)
    min_ts = max(0, (previous_poll or now_epoch) - lookback_s)
    # A bounded, fixed catch-up slice prevents a failed retry window from
    # growing forever while new account activity continues to arrive.  Normal
    # minute polls reach ``now_epoch``; after downtime, successive polls advance
    # the durable watermark one slice at a time with overlap.
    target_epoch = (now_epoch if not previous_poll else
                    min(now_epoch, previous_poll + catchup_span_s))

    # Start timestamp becomes the next watermark.  Because reads use a broad
    # overlap and durable IDs, records created while this poll is running are
    # either in this snapshot or safely rediscovered on the next one.
    recent_orders = reader.orders(min_ts, target_epoch)
    resting_orders = reader.resting_orders()
    orders = _orders_from_sources(reader, recent_orders, resting_orders,
                                  state["orders"])
    fills = _dedupe_records(reader.fills(min_ts, target_epoch),
                            normalize_fill, "fill_id")
    settlements_list = [normalize_settlement(raw)
                        for raw in reader.settlements(min_ts, target_epoch)]
    settlements = {}
    for item in settlements_list:
        key = _settlement_key(item)
        if key in settlements and settlements[key] != item:
            raise OrderActivityError("conflicting duplicate settlement %s" %
                                     item["ticker"])
        settlements[key] = item

    if not state.get("bootstrapped"):
        state["orders"] = {key: dict(value, _last_seen_epoch=now_epoch)
                           for key, value in orders.items()}
        # Fill payloads are only needed while formatting a new event.  The
        # durable dedupe state stores the OpenAPI fill_id and last-seen time,
        # not a second copy of account trade details.
        state["fills"] = {key: {"_last_seen_epoch": now_epoch}
                          for key in fills}
        state["settlements"] = {
            key: dict(value, _last_seen_epoch=now_epoch)
            for key, value in settlements.items()
        }
        state["bootstrapped"] = True
        state["last_poll_epoch"] = target_epoch
        queued = int(_append_pending(
            state, _event_id("monitor-bootstrap", str(target_epoch),
                             {"orders": len(orders), "fills": len(fills),
                              "settlements": len(settlements)}),
            _bootstrap_text(orders, fills, settlements), now_epoch))
        write_state_atomic(state_path, state)
        delivered += _deliver_pending(state, state_path, sender, max_chars)
        return {"bootstrapped": True, "queued": queued,
                "delivered": delivered, "pending": len(state["pending"])}

    old_orders = state["orders"]
    old_fills = state["fills"]
    old_settlements = state["settlements"]

    queued = 0
    market_attempts = set()
    for order_id, order in sorted(orders.items()):
        before = old_orders.get(order_id)
        if before is not None:
            _validate_order_transition(before, order)
        if before is None:
            payload = {"order": order}
            event_id = _event_id("order-new", order_id, payload)
            market = _market_for_event(reader, state, order["ticker"],
                                       now_epoch, market_attempts,
                                       max_market_lookups)
            queued += int(_append_pending(state, event_id,
                                          _format_new_order(order, market,
                                                            time_zone,
                                                            now_epoch),
                                          now_epoch))
        elif _changed(before, order):
            payload = {"before": {k: before.get(k)
                                   for k in ORDER_CHANGE_FIELDS},
                       "after": {k: order.get(k)
                                  for k in ORDER_CHANGE_FIELDS}}
            event_id = _event_id("order-change", order_id, payload)
            market = _market_for_event(reader, state, order["ticker"],
                                       now_epoch, market_attempts,
                                       max_market_lookups)
            queued += int(_append_pending(state, event_id,
                                          _format_order_change(before, order,
                                                               market,
                                                               time_zone,
                                                               now_epoch),
                                          now_epoch))
        old_orders[order_id] = dict(order, _last_seen_epoch=now_epoch)

    # Fills are an independent durable event stream.  Never discard a fill just
    # because an order snapshot changed in the same poll: the two endpoints are
    # read sequentially and can lag one another in either direction.  Always
    # reporting each unseen fill_id avoids both permanent loss and cross-poll
    # double-counting guesses; order status and actual executions are distinct
    # facts and are compacted into the same Telegram batch when concurrent.
    new_fills_by_order = {}
    for fill_id, fill in sorted(fills.items()):
        if fill_id not in old_fills:
            new_fills_by_order.setdefault(fill["order_id"], []).append(fill)
        old_fills[fill_id] = {"_last_seen_epoch": now_epoch}
    for order_id, grouped in sorted(new_fills_by_order.items()):
        payload = {"fill_ids": sorted(item["fill_id"] for item in grouped)}
        event_id = _event_id("fills", order_id, payload)
        market = _market_for_event(reader, state, grouped[0]["ticker"],
                                   now_epoch, market_attempts,
                                   max_market_lookups)
        related_order = old_orders.get(order_id)
        queued += int(_append_pending(state, event_id,
                                      _format_fills(grouped, market, time_zone,
                                                    related_order, now_epoch),
                                      now_epoch))

    for key, settlement in sorted(settlements.items()):
        before = old_settlements.get(key)
        comparable_before = ({k: v for k, v in before.items()
                              if not k.startswith("_")} if before else None)
        if before is None or comparable_before != settlement:
            updated = before is not None
            event_id = _event_id("settlement-update" if updated else
                                 "settlement", key, settlement)
            market = _market_for_event(reader, state, settlement["ticker"],
                                       now_epoch, market_attempts,
                                       max_market_lookups)
            queued += int(_append_pending(
                state, event_id, _format_settlement(settlement, market,
                                                    time_zone, updated),
                now_epoch))
        old_settlements[key] = dict(settlement, _last_seen_epoch=now_epoch)

    cutoff = now_epoch - retention_s
    _prune_unseen(old_orders, set(orders), cutoff)
    _prune_unseen(old_fills, set(fills), cutoff)
    _prune_unseen(old_settlements, set(settlements), cutoff)
    _prune_unseen(state["markets"], set(), cutoff)
    state["last_poll_epoch"] = target_epoch

    # Commit snapshots and pending facts before attempting delivery.  A crash
    # or Telegram outage therefore retries pending facts rather than losing
    # them or regenerating duplicates on every poll.
    write_state_atomic(state_path, state)
    delivered += _deliver_pending(state, state_path, sender, max_chars)
    return {"bootstrapped": False, "queued": queued,
            "delivered": delivered, "pending": len(state["pending"])}


@contextlib.contextmanager
def _exclusive_lock(state_path):
    directory = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    lock_path = state_path + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OrderActivityError("another order activity poll is running") \
                from None
        yield


def main():
    state_path = tg.state_path(DEFAULT_STATE_NAME)
    try:
        with _exclusive_lock(state_path):
            summary = poll_once(state_path=state_path)
        tg.log("order activity poll: %s" % summary)
        return 0
    except OrderActivityError as exc:
        tg.log("order activity poll failed: %s" % str(exc)[:200])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
