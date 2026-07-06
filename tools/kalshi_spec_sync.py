#!/usr/bin/env python3
"""Fetch official Kalshi docs snapshots and report implementation drift.

This tool is ops-only. It fetches public docs when --allow-network is explicit,
stores snapshots under docs/vendor/kalshi/latest/, writes a manifest with source
hashes/fetch times, and reports drift. It never patches trading code.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
LATEST_DIR = os.path.join(ROOT, "docs", "vendor", "kalshi", "latest")
BASELINE_DIR = os.path.join(ROOT, "docs", "vendor", "kalshi", "baseline")
STATUS_FILE = os.path.join(WORK, "kalshi_spec_alignment.json")

SOURCES = [
    ("llms", "https://docs.kalshi.com/llms.txt", "llms.txt"),
    ("openapi", "https://docs.kalshi.com/openapi.yaml", "openapi.yaml"),
    ("asyncapi", "https://docs.kalshi.com/asyncapi.yaml", "asyncapi.yaml"),
    ("changelog", "https://docs.kalshi.com/changelog", "changelog.html"),
    ("changelog_rss", "https://docs.kalshi.com/changelog/rss.xml", "changelog_rss.xml"),
]

TARGET_FILES = [
    "src/request_spec.cpp",
    "src/rest_api.cpp",
    "include/kalshi/rest_api.hpp",
    "src/ws_client.cpp",
    "tests/mock_rest.py",
    "tests/test_rest_api.cpp",
    "tests/test_request_spec.cpp",
    "tests/test_ws_client.cpp",
    "docs/kalshi_ws_protocol.md",
]

REST_EXPECTATIONS = [
    {
        "id": "exchange_status",
        "method": "GET",
        "wire": "/trade-api/v2/exchange/status",
        "repo_tokens": ["/exchange/status"],
    },
    {
        "id": "markets",
        "method": "GET",
        "wire": "/trade-api/v2/markets",
        "repo_tokens": ["/markets?limit=", "MarketsQuery"],
    },
    {
        "id": "market",
        "method": "GET",
        "wire": "/trade-api/v2/markets/{ticker}",
        "repo_tokens": ["/markets/"],
    },
    {
        "id": "orderbook",
        "method": "GET",
        "wire": "/trade-api/v2/markets/{ticker}/orderbook",
        "repo_tokens": ["/orderbook", "orderbook_fp"],
    },
    {
        "id": "batch_orderbooks",
        "method": "GET",
        "wire": "/trade-api/v2/markets/orderbooks",
        "repo_tokens": ["/markets/orderbooks", "batch_orderbook"],
    },
    {
        "id": "account_limits",
        "method": "GET",
        "wire": "/trade-api/v2/account/limits",
        "repo_tokens": ["/account/limits", "AccountLimits"],
    },
    {
        "id": "endpoint_costs",
        "method": "GET",
        "wire": "/trade-api/v2/account/endpoint_costs",
        "repo_tokens": ["/account/endpoint_costs", "EndpointCostTable"],
    },
    {
        "id": "fills",
        "method": "GET",
        "wire": "/trade-api/v2/portfolio/fills",
        "repo_tokens": ["/portfolio/fills"],
    },
    {
        "id": "positions",
        "method": "GET",
        "wire": "/trade-api/v2/portfolio/positions",
        "repo_tokens": ["/portfolio/positions"],
    },
    {
        "id": "create_order_v2",
        "method": "POST",
        "wire": "/trade-api/v2/portfolio/events/orders",
        "repo_tokens": ["/portfolio/events/orders", "client_order_id", "order_json"],
    },
]

WS_EXPECTATIONS = [
    {
        "id": "subscribe_orderbook_delta",
        "repo_tokens": ["cmd\":\"subscribe", "orderbook_delta", "market_tickers"],
        "official_tokens": ["subscribe", "orderbook_delta", "market_tickers"],
    },
    {
        "id": "unsubscribe",
        "repo_tokens": ["cmd\":\"unsubscribe", "sids"],
        "official_tokens": ["unsubscribe", "sids"],
    },
    {
        "id": "orderbook_snapshot",
        "repo_tokens": ["orderbook_snapshot", "yes_dollars_fp", "no_dollars_fp"],
        "official_tokens": ["orderbook_snapshot", "yes_dollars_fp", "no_dollars_fp"],
    },
    {
        "id": "orderbook_delta",
        "repo_tokens": ["orderbook_delta", "price_dollars", "delta_fp"],
        "official_tokens": ["orderbook_delta", "price_dollars", "delta_fp"],
    },
    {
        "id": "use_yes_price_false",
        "repo_tokens": ["use_yes_price", "false"],
        "official_tokens": ["use_yes_price"],
    },
]


def now_ms():
    return int(time.time() * 1000)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


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


def fetch(url, timeout):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "kalshi-hft-spec-sync/1.0 (+public docs only)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_snapshots(args):
    os.makedirs(args.latest_dir, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "generated_at_ms": now_ms(),
        "sources": [],
    }
    errors = []
    for name, url, filename in SOURCES:
        try:
            data = fetch(url, args.timeout)
        except Exception as e:
            errors.append({"source": name, "url": url, "error": str(e)})
            continue
        path = os.path.join(args.latest_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        manifest["sources"].append({
            "name": name,
            "url": url,
            "file": filename,
            "sha256": sha256(data),
            "bytes": len(data),
            "fetched_at_ms": now_ms(),
        })
    write_json(os.path.join(args.latest_dir, "manifest.json"), manifest)
    return manifest, errors


def load_manifest(path):
    return load_json(os.path.join(path, "manifest.json"), {"sources": []})


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def latest_text(args, filename):
    return read_text(os.path.join(args.latest_dir, filename))


def repo_text():
    parts = []
    missing = []
    for rel in TARGET_FILES:
        path = os.path.join(ROOT, rel)
        text = read_text(path)
        if not text:
            missing.append(rel)
        parts.append("\n/* %s */\n%s" % (rel, text))
    return "\n".join(parts), missing


def normalize_for_spec(path):
    path = path.replace("{ticker}", "")
    path = path.replace("{order_id}", "")
    path = path.replace("/trade-api/v2", "")
    return path.strip("/")


def official_has(spec_text, wire_path):
    if not spec_text:
        return False
    candidates = {
        wire_path,
        wire_path.replace("/trade-api/v2", ""),
        normalize_for_spec(wire_path),
    }
    hay = spec_text.lower()
    return any(c.lower() in hay for c in candidates if c)


def compare_hashes(latest, baseline):
    drift = []
    syntax_sources = {"openapi", "asyncapi"}
    latest_by_name = {s.get("name"): s for s in latest.get("sources", [])}
    base_by_name = {s.get("name"): s for s in baseline.get("sources", [])}
    for name, src in sorted(latest_by_name.items()):
        old = base_by_name.get(name)
        if not old:
            drift.append({
                "kind": "baseline_missing_source",
                "source": name,
                "new_sha256": src.get("sha256"),
                "severity": "info",
            })
        elif old.get("sha256") != src.get("sha256"):
            severity = "error" if name in syntax_sources else "info"
            drift.append({
                "kind": "source_hash_changed",
                "source": name,
                "old_sha256": old.get("sha256"),
                "new_sha256": src.get("sha256"),
                "severity": severity,
            })
    return drift


def compare_implementation(args, latest):
    drift = []
    repo, missing_files = repo_text()
    repo_l = repo.lower()
    openapi = latest_text(args, "openapi.yaml")
    asyncapi = latest_text(args, "asyncapi.yaml")

    for rel in missing_files:
        drift.append({
            "kind": "target_file_missing",
            "file": rel,
            "severity": "error",
        })

    for exp in REST_EXPECTATIONS:
        official = official_has(openapi, exp["wire"])
        implemented = all(tok.lower() in repo_l for tok in exp["repo_tokens"])
        if official and not implemented:
            drift.append({
                "kind": "official_rest_endpoint_missing_in_repo",
                "id": exp["id"],
                "method": exp["method"],
                "wire_path": exp["wire"],
                "repo_tokens": exp["repo_tokens"],
                "severity": "error",
            })
        elif implemented and openapi and not official:
            drift.append({
                "kind": "repo_rest_endpoint_not_found_in_official_spec",
                "id": exp["id"],
                "method": exp["method"],
                "wire_path": exp["wire"],
                "severity": "warning",
            })

    official_ws = asyncapi.lower() if asyncapi else ""
    for exp in WS_EXPECTATIONS:
        implemented = all(tok.lower() in repo_l for tok in exp["repo_tokens"])
        official = all(tok.lower() in official_ws for tok in exp["official_tokens"]) if official_ws else False
        if official and not implemented:
            drift.append({
                "kind": "official_ws_shape_missing_in_repo",
                "id": exp["id"],
                "repo_tokens": exp["repo_tokens"],
                "official_tokens": exp["official_tokens"],
                "severity": "error",
            })
        elif implemented and official_ws and not official:
            drift.append({
                "kind": "repo_ws_shape_not_found_in_official_spec",
                "id": exp["id"],
                "repo_tokens": exp["repo_tokens"],
                "official_tokens": exp["official_tokens"],
                "severity": "warning",
            })

    # Surface likely REST path literals from implementation so reviewers can
    # spot newly added endpoints that should be checked against OpenAPI.
    repo_paths = sorted(set(re.findall(r'"/(?:trade-api/v2/)?[A-Za-z0-9_/{}/.-]+', repo)))
    return drift, [p.strip('"') for p in repo_paths[:200]]


def accept_baseline(args, latest):
    if not latest.get("sources"):
        return False
    os.makedirs(args.baseline_dir, exist_ok=True)
    for src in latest.get("sources", []):
        filename = src.get("file")
        if not filename:
            continue
        src_path = os.path.join(args.latest_dir, filename)
        dst_path = os.path.join(args.baseline_dir, filename)
        if os.path.exists(src_path):
            shutil.copy2(src_path, dst_path)
    manifest = os.path.join(args.latest_dir, "manifest.json")
    if os.path.exists(manifest):
        shutil.copy2(manifest, os.path.join(args.baseline_dir, "manifest.json"))
    return True


def build_report(args):
    errors = []
    if args.allow_network:
        latest, errors = fetch_snapshots(args)
    else:
        latest = load_manifest(args.latest_dir)

    if args.accept_baseline and latest.get("sources"):
        accept_baseline(args, latest)
    baseline = load_manifest(args.baseline_dir)
    drift = []
    if not latest.get("sources"):
        status = "skipped" if not args.allow_network else "fail"
        summary = "No latest Kalshi snapshots available."
    else:
        drift.extend(compare_hashes(latest, baseline))
        impl_drift, repo_paths = compare_implementation(args, latest)
        drift.extend(impl_drift)
        if errors:
            status = "fail"
            summary = "%d fetch error(s); spec alignment incomplete." % len(errors)
        elif not baseline.get("sources"):
            status = "not_started"
            summary = "Latest snapshots exist, but no reviewed baseline manifest exists."
        elif any(d.get("severity") == "error" for d in drift):
            status = "fail"
            summary = "Spec alignment found implementation drift."
        elif any(d.get("kind") == "source_hash_changed" and d.get("severity") == "error"
                 for d in drift):
            status = "fail"
            summary = "Official Kalshi REST/WS spec hashes changed versus baseline."
        else:
            status = "pass"
            summary = "No spec drift detected versus baseline and repository expectations."

    report = {
        "type": "kalshi_spec_alignment",
        "schema_version": 1,
        "generated_at_ms": now_ms(),
        "status": status,
        "summary": summary,
        "snapshot_dir": os.path.relpath(args.latest_dir, ROOT),
        "baseline_dir": os.path.relpath(args.baseline_dir, ROOT),
        "manifest": os.path.relpath(os.path.join(args.latest_dir, "manifest.json"), ROOT),
        "sources": latest.get("sources", []),
        "fetch_errors": errors,
        "drift": drift,
        "target_files": TARGET_FILES,
        "accepted_baseline": bool(args.accept_baseline and latest.get("sources")),
        "repo_paths_sample": repo_paths if latest.get("sources") else [],
    }
    write_json(STATUS_FILE, report)
    return report


def main():
    ap = argparse.ArgumentParser(description="Fetch Kalshi public specs and report drift")
    ap.add_argument("--allow-network", action="store_true",
                    help="fetch public Kalshi docs/spec URLs")
    ap.add_argument("--latest-dir", default=LATEST_DIR)
    ap.add_argument("--baseline-dir", default=BASELINE_DIR)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--accept-baseline", action="store_true",
                    help="promote latest snapshots to the reviewed baseline")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_report(args)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print("%s: %s" % (report["status"], report["summary"]))
        for d in report.get("drift", [])[:50]:
            print("DRIFT: %s" % json.dumps(d, sort_keys=True))
        if report["status"] == "pass":
            print("KALSHI SPEC ALIGNMENT PASS")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
