#!/usr/bin/env python3
"""Public Kalshi docs/update watcher.

Monitors only public documentation URLs. It never reads API keys, never calls a
trading/account endpoint, and never mutates account state.

Output events are appended to work/kalshi_updates.ndjson for the dashboard to
stream over SSE.
"""
import argparse
import email.utils
import hashlib
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
STATE_FILE = os.path.join(WORK, "kalshi_update_state.json")
EVENTS_FILE = os.path.join(WORK, "kalshi_updates.ndjson")

SOURCES = [
    ("llms", "https://docs.kalshi.com/llms.txt"),
    ("openapi", "https://docs.kalshi.com/openapi.yaml"),
    ("asyncapi", "https://docs.kalshi.com/asyncapi.yaml"),
    ("changelog", "https://docs.kalshi.com/changelog"),
    ("changelog_rss", "https://docs.kalshi.com/changelog/rss.xml"),
]


def now_ms():
    return int(time.time() * 1000)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def append_event(event):
    os.makedirs(WORK, exist_ok=True)
    event = dict(event)
    event.setdefault("type", "kalshi_update")
    event.setdefault("ts_ms", now_ms())
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url, timeout):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "kalshi-hft-docs-watcher/1.0 (+public docs only)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_rss(data):
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for item in root.findall(".//item"):
        def text(name):
            el = item.find(name)
            return (el.text or "").strip() if el is not None else ""

        guid = text("guid") or text("link") or text("title")
        pub = text("pubDate")
        pub_ms = None
        if pub:
            try:
                pub_ms = int(email.utils.parsedate_to_datetime(pub).timestamp() * 1000)
            except Exception:
                pub_ms = None
        out.append({
            "guid": guid,
            "title": text("title"),
            "link": text("link"),
            "pubDate": pub,
            "pub_ts_ms": pub_ms,
            "category": text("category"),
        })
    return out


def run_once(args):
    if not args.allow_network:
        result = {
            "status": "skipped",
            "changes": 0,
            "summary": "Skipped: --allow-network is required for public docs fetches.",
            "events_file": os.path.relpath(EVENTS_FILE, ROOT),
        }
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(result["summary"])
        return 0

    state = load_json(args.state, {"hashes": {}, "rss_guids": []})
    old_hashes = state.get("hashes", {})
    old_guids = set(state.get("rss_guids", []))
    new_hashes = {}
    rss_items = []
    events = []
    errors = []

    for name, url in SOURCES:
        try:
            data = fetch(url, args.timeout)
        except Exception as e:
            errors.append({"source": name, "url": url, "error": str(e)})
            continue
        h = sha256(data)
        new_hashes[name] = h
        prev = old_hashes.get(name)
        if prev and prev != h:
            events.append({
                "kind": "source_hash_changed",
                "source": name,
                "url": url,
                "old_sha256": prev,
                "new_sha256": h,
            })
        elif not prev:
            events.append({
                "kind": "source_seen",
                "source": name,
                "url": url,
                "new_sha256": h,
            })
        if name == "changelog_rss":
            rss_items = parse_rss(data)

    for item in rss_items:
        guid = item.get("guid")
        if guid and guid not in old_guids:
            events.append({
                "kind": "new_changelog_entry",
                "source": "changelog_rss",
                "guid": guid,
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "pubDate": item.get("pubDate", ""),
                "pub_ts_ms": item.get("pub_ts_ms"),
                "category": item.get("category", ""),
            })

    for event in events:
        append_event(event)

    state = {
        "schema_version": 1,
        "updated_at_ms": now_ms(),
        "hashes": new_hashes or old_hashes,
        "rss_guids": sorted({i.get("guid") for i in rss_items if i.get("guid")} | old_guids),
        "last_errors": errors,
        "sources": [{"name": n, "url": u} for n, u in SOURCES],
    }
    write_json(args.state, state)

    status = "pass" if not errors else "fail"
    result = {
        "status": status,
        "changes": len(events),
        "errors": errors,
        "summary": "%d update event(s), %d fetch error(s)." % (len(events), len(errors)),
        "events_file": os.path.relpath(EVENTS_FILE, ROOT),
        "state_file": os.path.relpath(args.state, ROOT),
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result["summary"])
        if status == "pass":
            print("KALSHI UPDATE WATCH PASS")
    return 0 if status == "pass" else 1


def main():
    ap = argparse.ArgumentParser(description="Monitor public Kalshi docs/spec updates")
    ap.add_argument("--allow-network", action="store_true",
                    help="allow fetching public docs URLs")
    ap.add_argument("--once", action="store_true", help="run one check and exit")
    ap.add_argument("--watch", action="store_true", help="poll until interrupted")
    ap.add_argument("--interval-seconds", type=int, default=900,
                    help="watch interval; keep >= 900 in normal use")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--state", default=STATE_FILE)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.watch:
        code = 0
        while True:
            code = run_once(args)
            time.sleep(max(60, args.interval_seconds))
        return code
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
