#!/usr/bin/env python3
"""W-LAT-BENCH-01 Tier 2a — authenticated READ-ONLY full-round-trip latency probe.

Measures the complete production GET round trip (network + TLS + auth +
server processing) over ONE persistent HTTPS connection — the same shape a
warm order lane has. Strictly read-only: the HTTP method is the constant
"GET" and there is no code path that builds any other verb. Safe against
production capture (P4): default 2 req/s alternating /exchange/status
(10 read tokens) and /markets?limit=1 (1 token) ≈ 11 tokens/s, 3.7% of the
300/s read bucket (config/kalshi_facts.yaml rate_limits).

Signing mirrors deploy/balance_probe.py (openssl RSA-PSS subprocess, no
third-party deps, portable to any box with python3 + openssl). The
subprocess sign time is recorded separately (sign_ms) and EXCLUDED from
rtt_ms — the C++ engine signs in-process at ~100-200us (see Tier 1
engine_bench), so subprocess overhead here would pollute the RTT statistic.

Per-sample ndjson lines are appended to --out; a summary line prints at the
end. Run the identical script from every candidate box (Tier 3) with a
distinct --tag.

Usage:
  latency_probe_auth.py [--n 400] [--hz 2.0] [--tag mac] [--throwaway]
                        [--out work/latency_baseline/tier2a_samples.ndjson]
Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
     KALSHI_BASE_URL (default https://external-api.kalshi.com)

--throwaway (Tier 3 remote points, e.g. W09 whose isolation contract
forbids real credentials on the box): generates an ephemeral local RSA-2048
key and a zero key id. Kalshi ignores the signature on public market-data
GETs (same precedent as apps/bench_rtt.cpp), so the round trip is still the
full network+TLS+server path; every record carries key_source so
authenticated and throwaway samples are never silently mixed.
"""
import argparse
import base64
import datetime
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

API_PREFIX = "/trade-api/v2"
METHOD = "GET"  # constant by design — this tool cannot mutate anything
# (path, read-token cost per config/kalshi_facts.yaml + endpoint_costs)
ENDPOINTS = [("/exchange/status", 10), ("/markets?limit=1", 1)]


def _sign(key_path, message):
    t0 = time.perf_counter()
    p = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sigopt", "rsa_padding_mode:pss",
         "-sigopt", "rsa_pss_saltlen:digest", "-sign", key_path, "-binary"],
        input=message.encode(), capture_output=True)
    sign_ms = (time.perf_counter() - t0) * 1000.0
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("openssl signing failed")
    return base64.b64encode(p.stdout).decode(), sign_ms


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(p * len(sorted_vals)))
    return sorted_vals[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--tag", default=socket.gethostname())
    ap.add_argument("--out",
                    default="work/latency_baseline/tier2a_samples.ndjson")
    ap.add_argument("--throwaway", action="store_true",
                    help="ephemeral local key, zero key id (public GETs only)")
    args = ap.parse_args()

    if args.throwaway:
        key_id = "00000000-0000-0000-0000-000000000000"
        key_path = "/tmp/latency_probe_throwaway_key.pem"
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA",
                        "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", key_path], check=True, capture_output=True)
        key_source = "throwaway"
    else:
        key_id = os.environ.get("KALSHI_API_KEY_ID")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        key_source = "env"
    if not key_id or not key_path:
        print("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH unset", file=sys.stderr)
        return 2
    base = os.environ.get("KALSHI_BASE_URL", "https://external-api.kalshi.com")
    host = urllib.parse.urlsplit(base).netloc

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    period = 1.0 / args.hz if args.hz > 0 else 0.0
    conn = None
    rtts, cold_ms, errors = {}, None, 0

    with open(args.out, "a") as f:
        for i in range(args.n):
            t_loop = time.perf_counter()
            path, cost = ENDPOINTS[i % len(ENDPOINTS)]
            ts = str(int(time.time() * 1000))
            message = ts + METHOD + API_PREFIX + path.split("?", 1)[0]
            try:
                sig, sign_ms = _sign(key_path, message)
            except Exception as e:
                print("sign error: %s" % e, file=sys.stderr)
                return 1
            headers = {"KALSHI-ACCESS-KEY": key_id,
                       "KALSHI-ACCESS-SIGNATURE": sig,
                       "KALSHI-ACCESS-TIMESTAMP": ts,
                       "Connection": "keep-alive"}
            cold = conn is None
            try:
                if conn is None:
                    conn = http.client.HTTPSConnection(host, timeout=10)
                t0 = time.perf_counter()
                conn.request(METHOD, API_PREFIX + path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                rtt_ms = (time.perf_counter() - t0) * 1000.0
                status = resp.status
            except Exception as e:
                errors += 1
                conn = None  # force reconnect; next sample is cold again
                f.write(json.dumps({"ts_utc": utc_now(), "tag": args.tag,
                                    "endpoint": path, "error": str(e)[:120]})
                        + "\n")
                continue
            rec = {"ts_utc": utc_now(), "tag": args.tag, "host": host,
                   "key_source": key_source,
                   "endpoint": path.split("?", 1)[0], "cost_tokens": cost,
                   "cold": cold, "rtt_ms": round(rtt_ms, 2),
                   "sign_ms": round(sign_ms, 2), "http_status": status,
                   "body_bytes": len(body)}
            f.write(json.dumps(rec) + "\n")
            if cold:
                cold_ms = rtt_ms
            elif status == 200:
                rtts.setdefault(rec["endpoint"], []).append(rtt_ms)
            else:
                errors += 1
            if period:
                time.sleep(max(0.0, period - (time.perf_counter() - t_loop)))

    print("tag=%s host=%s cold=%.1fms errors=%d" %
          (args.tag, host, cold_ms or -1, errors))
    for ep, v in sorted(rtts.items()):
        v.sort()
        print("%-18s n=%-4d min=%.1f p50=%.1f p90=%.1f p99=%.1f max=%.1f ms" %
              (ep, len(v), v[0], percentile(v, .50), percentile(v, .90),
               percentile(v, .99), v[-1]))
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
