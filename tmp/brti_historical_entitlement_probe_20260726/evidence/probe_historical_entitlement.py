#!/usr/bin/env python3
"""One-shot, empty-period CF Benchmarks historical entitlement probe."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


HOST = "https://external-api.kalshi.com"
SIGNED_PATH = "/trade-api/v2/cfbenchmarks/history/values"
QUERY_ITEMS = (
    ("id", "BRTI"),
    ("timespan", "HOUR"),
    ("timestamp", "2009-01-01T00:00:00.000Z"),
)
FORBIDDEN_DATE_TOKENS = (
    "2026-07-20",
    "2026-07-21",
    "2026-07-22",
    "2026-07-23",
)
TIMEOUT_S = 15
MAX_RESPONSE_BYTES = 1_048_576


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_seal(script: Path, prereg: Path, seal: Path) -> dict[str, Any]:
    receipt = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "script_sha256": sha256(script),
        "preregistration_sha256": sha256(prereg),
    }
    for key, value in observed.items():
        if receipt.get(key) != value:
            raise RuntimeError(
                f"seal mismatch for {key}: observed {value}"
            )
    return {
        **observed,
        "seal_sha256": sha256(seal),
        "verified": True,
    }


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise RuntimeError(f"missing credential environment class: {names}")


def auth_headers() -> dict[str, str]:
    key_id = env_value("KALSHI_API_KEY_ID", "KALSHI_KEY_ID")
    key_path = env_value(
        "KALSHI_PRIVATE_KEY_PATH", "KALSHI_PRIV_KEY_PATH"
    )
    private_key = serialization.load_pem_private_key(
        Path(key_path).read_bytes(), password=None
    )
    timestamp_ms = str(int(time.time() * 1000))
    message = f"{timestamp_ms}GET{SIGNED_PATH}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Accept": "application/json",
        "User-Agent": "brti-historical-entitlement-probe/1",
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bounded_read(response) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError(
            f"response exceeds {MAX_RESPONSE_BYTES} byte bound"
        )
    return body


def authorization_detail(text: str) -> bool:
    return bool(re.search(
        r"authoriz|entitl|permission|stream_historical_values|"
        r"access[ _-]?denied|forbidden",
        text,
        flags=re.IGNORECASE,
    ))


def omit_payloads(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if key == "payload":
                clean[key] = {
                    "omitted": True,
                    "row_count": len(item) if isinstance(item, list) else None,
                }
            else:
                clean[key] = omit_payloads(item)
        return clean
    if isinstance(value, list):
        return [omit_payloads(item) for item in value]
    return value


def sanitized_detail(parsed: Any, raw_text: str) -> str:
    if parsed is not None:
        detail = json.dumps(
            omit_payloads(parsed),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        detail = raw_text
    detail = re.sub(
        r"(?i)(kalshi-access-(?:key|signature|timestamp))[\"':= ]+[^,}\\s]+",
        r"\1:<redacted>",
        detail,
    )
    return detail[:4000]


def classify(
    status: int | None,
    parsed: Any,
    detail: str,
    network_error: str | None,
) -> str:
    if network_error is not None:
        return "NETWORK_ERROR"
    if status == 200:
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, dict):
            return "HTTP_200_INVALID_KALSHI_ENVELOPE"
        if data.get("error"):
            return "HTTP_200_UPSTREAM_ERROR"
        payload = data.get("payload")
        if payload is None or isinstance(payload, list):
            return "ENTITLED_HISTORICAL_AVAILABLE"
        return "HTTP_200_INVALID_CF_ENVELOPE"
    if status == 401:
        return "LOCAL_KALSHI_AUTH_FAILED"
    if status == 403:
        return "KALSHI_ENTITLEMENT_DENIED"
    if status == 503:
        if authorization_detail(detail):
            return "UPSTREAM_HISTORICAL_ENTITLEMENT_DENIED"
        return "UPSTREAM_503_AMBIGUOUS_AUTH_SERVER_OR_TIMEOUT"
    if status == 400:
        return "PROBE_SYNTAX_REJECTED"
    if status == 404:
        return "PASSTHROUGH_ROUTE_NOT_FOUND"
    return "UNEXPECTED_HTTP_RESULT"


def response_summary(
    status: int | None,
    headers: Any,
    body: bytes,
    network_error: str | None,
) -> dict[str, Any]:
    raw_text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw_text) if raw_text else None
    except json.JSONDecodeError:
        parsed = None
    detail = sanitized_detail(parsed, raw_text)
    result = {
        "http_status": status,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "content_type": (
            headers.get("Content-Type") if headers is not None else None
        ),
        "request_id": (
            headers.get("X-Request-Id") if headers is not None else None
        ),
        "network_error": network_error,
        "sanitized_detail": detail,
    }
    data = parsed.get("data") if isinstance(parsed, dict) else None
    payload = data.get("payload") if isinstance(data, dict) else None
    if payload is None:
        result["payload_count"] = 0
        result["payload_sha256"] = hashlib.sha256(b"null").hexdigest()
        result["unexpected_nonempty_payload"] = False
    elif isinstance(payload, list):
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
        result["payload_count"] = len(payload)
        result["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
        result["unexpected_nonempty_payload"] = bool(payload)
    else:
        result["payload_count"] = None
        result["payload_sha256"] = None
        result["unexpected_nonempty_payload"] = None
    result["classification"] = classify(
        status, parsed, detail, network_error
    )
    return result


def run_probe() -> dict[str, Any]:
    query = urllib.parse.urlencode(QUERY_ITEMS)
    url = f"{HOST}{SIGNED_PATH}?{query}"
    for token in FORBIDDEN_DATE_TOKENS:
        if token in url:
            raise RuntimeError(f"forbidden experiment date in probe URL: {token}")
    if "/cfbenchmarks/values" in url:
        raise RuntimeError("recent-values endpoint is forbidden")
    request = urllib.request.Request(
        url=url,
        method="GET",
        headers=auth_headers(),
    )
    opener = urllib.request.build_opener(NoRedirect)
    start = time.monotonic()
    status = None
    headers = None
    body = b""
    network_error = None
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            status = response.status
            headers = response.headers
            body = bounded_read(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = exc.headers
        body = bounded_read(exc)
    except Exception as exc:
        network_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
    elapsed_ms = (time.monotonic() - start) * 1000
    summary = response_summary(
        status, headers, body, network_error
    )
    summary["elapsed_ms"] = round(elapsed_ms, 3)
    return summary


def render_markdown(report: dict[str, Any]) -> str:
    response = report["response"]
    classification = response["classification"]
    if classification == "ENTITLED_HISTORICAL_AVAILABLE":
        next_step = (
            "Historical entitlement is confirmed. No experiment-period value "
            "was requested. A separate calibration-download preregistration "
            "must now be written and sealed before accessing 2026-07-20..22."
        )
    else:
        next_step = (
            "Historical entitlement is not confirmed. Do not request "
            "experiment-period history; retain the HTTP/error receipt and "
            "resolve entitlement or upstream availability first."
        )
    return f"""# Authoritative historical BRTI availability probe

Result: **{classification}**

- HTTP status: `{response['http_status']}`
- Elapsed: `{response['elapsed_ms']:.3f} ms`
- Payload rows: `{response['payload_count']}`
- Error/detail: `{response['sanitized_detail']}`
- Response body SHA-256: `{response['body_sha256']}`

The single request was:

```text
GET /trade-api/v2/cfbenchmarks/history/values
id=BRTI
timespan=HOUR
timestamp=2009-01-01T00:00:00.000Z
```

The hour predates BRTI and all project experiments. No 2026-07-20..23 value
was requested, persisted, printed, hashed, counted, or summarized. The recent
`/cfbenchmarks/values?id=BRTI` endpoint was not called.

{next_step}

Official sources:

- https://docs.kalshi.com/cfbenchmarks/rest-passthrough.md
- https://docs.cfbenchmarks.com/api/rest/historical-values/
- https://docs.cfbenchmarks.com/api/rest/values/
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()
    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    seal_info = verify_seal(script, prereg, seal)
    response = run_probe()
    report = {
        "schema": "brti-historical-entitlement-probe-receipt-v1",
        "read_only": True,
        "request_count": 1,
        "retry_count": 0,
        "redirect_count": 0,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "experiment_period_values_requested_or_processed": False,
        "recent_values_endpoint_called": False,
        "request": {
            "method": "GET",
            "host": HOST,
            "signed_path": SIGNED_PATH,
            "query": dict(QUERY_ITEMS),
            "signature_excludes_query": True,
            "credential_fields_recorded": False,
        },
        "seal": seal_info,
        "response": response,
        "next_step": (
            "SEAL_NEW_CALIBRATION_PREREG_BEFORE_EXPERIMENT_DOWNLOAD"
            if response["classification"]
            == "ENTITLED_HISTORICAL_AVAILABLE"
            else "DO_NOT_DOWNLOAD_EXPERIMENT_HISTORY"
        ),
    }
    out = Path(args.out)
    md_out = Path(args.md_out)
    out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_out.write_text(render_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
