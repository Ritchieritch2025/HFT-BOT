#!/usr/bin/env python3
"""W09-only, exact-VersionId S3 transport for the fresh-RFQ reader.

The public client surface is deliberately limited to ``head`` and
``get_exact`` so it can be injected into ``fresh_rfq_exact_reader`` without
also granting that code a list, latest-version, copy, delete, or write API.
Every S3 request includes a caller-supplied, non-null ``versionId`` and is
signed with temporary IMDSv2 credentials (including the session token).
Each successful HEAD creates one in-process key/version/length binding; one
GET attempt consumes it, including when response or body validation fails.
The default urllib opener rejects every HTTP redirect before it can construct
or send a second request with stale authorization or a changed query.

This module proves only what this adapter requested and observed.  It does not
claim that W09's live IAM policy, instance profile, or bucket policy has been
audited.  Deployment and runner integration are intentionally separate gates.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from research_data_instance_profile import (
    IMDSv2Credentials,
    refuse_static_credentials,
)


SOURCE_BUCKET = "kalshi-vault-ritcardo"
DEFAULT_REGION = "us-east-2"
READ_CHUNK_BYTES = 1 << 20
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class W09ExactS3AdapterError(RuntimeError):
    """Fail-closed transport error that never includes credential values."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise W09ExactS3AdapterError(code, detail)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _safe_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        _fail("KEY_INVALID", "key is not a safe canonical S3 key")
    if any(part in ("", ".", "..") for part in value.split("/")):
        _fail("KEY_INVALID", "key has an unsafe path component")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("KEY_INVALID", "key contains a control character")
    return value


def _exact_version(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.lower() == "null"
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        _fail("VERSION_REQUIRED", "a pinned non-null VersionId is required")
    return value


def _region(value: Any) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z0-9-]{3,32}", value) is None
    ):
        _fail("REGION_INVALID", "AWS region is invalid")
    return value


def _header(headers: Any, name: str) -> str | None:
    value = headers.get(name) if headers is not None else None
    if value is None:
        return None
    return str(value).strip()


def _response_metadata(response: Any, *, method: str, key: str) -> dict[str, Any]:
    headers = getattr(response, "headers", None)
    version_id = _header(headers, "x-amz-version-id")
    if not version_id or version_id.lower() == "null":
        _fail("RESPONSE_VERSION_MISSING", f"{method} {key} omitted VersionId")
    raw_length = _header(headers, "Content-Length")
    try:
        content_length = int(raw_length) if raw_length is not None else -1
    except ValueError:
        content_length = -1
    if content_length < 0:
        _fail("RESPONSE_LENGTH_INVALID", f"{method} {key} has invalid length")
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if not isinstance(status, int) or isinstance(status, bool):
        _fail("RESPONSE_STATUS_INVALID", f"{method} {key} has no HTTP status")
    if status != 200:
        _fail("RESPONSE_STATUS_INVALID", f"{method} {key} returned HTTP {status}")
    request_id = _header(headers, "x-amz-request-id")
    if not request_id:
        _fail("RESPONSE_REQUEST_ID_MISSING", f"{method} {key} has no request id")
    return {
        "VersionId": version_id,
        "ContentLength": content_length,
        "http_status": status,
        "request_id": request_id,
    }


class _RejectAllRedirects(urllib.request.HTTPRedirectHandler):
    """Fail closed before urllib can construct or send a redirect request."""

    def _refuse(self, _request, response, code, _message, _headers):
        try:
            response.close()
        finally:
            _fail(
                "S3_REDIRECT_REFUSED",
                f"HTTP {code} redirect refused before following Location",
            )

    http_error_301 = _refuse
    http_error_302 = _refuse
    http_error_303 = _refuse
    http_error_307 = _refuse
    http_error_308 = _refuse

    def redirect_request(self, *_args, **_kwargs):
        # Defense in depth if a future urllib path calls redirect_request
        # directly instead of dispatching through one of the handlers above.
        _fail("S3_REDIRECT_REFUSED", "HTTP redirect refused")


def _build_no_redirect_opener() -> urllib.request.OpenerDirector:
    """Return an opener whose redirect handler replaces urllib's default."""
    return urllib.request.build_opener(_RejectAllRedirects())


class W09ExactVersionS3Client:
    """Two-operation S3 adapter for ``ExactReadSession``.

    If ``credentials`` is omitted, static credentials are refused and the
    existing W09 IMDSv2 provider is used.  Tests inject a temporary-credential
    provider and an offline URL opener; that injection is not an IAM claim.
    """

    def __init__(
        self,
        *,
        region: str = DEFAULT_REGION,
        credentials: Any | None = None,
        urlopen: Any | None = None,
        timeout: float = 120.0,
    ):
        self._region = _region(region)
        if credentials is None:
            refuse_static_credentials()
            credentials = IMDSv2Credentials()
        if not callable(getattr(credentials, "current", None)):
            _fail("CREDENTIAL_PROVIDER_INVALID", "provider lacks current()")
        if urlopen is not None and not callable(urlopen):
            _fail("TRANSPORT_INVALID", "urlopen transport is not callable")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            _fail("TIMEOUT_INVALID", "timeout must be positive")
        self._credentials = credentials
        if urlopen is None:
            self._opener = _build_no_redirect_opener()
            self._urlopen = self._opener.open
        else:
            self._opener = None
            self._urlopen = urlopen
        self._timeout = float(timeout)
        self._host = f"{SOURCE_BUCKET}.s3.{self._region}.amazonaws.com"
        self._ledger: list[dict[str, Any]] = []
        self._head_bindings: dict[tuple[str, str], dict[str, Any]] = {}
        self._state_lock = threading.Lock()

    @property
    def operation_ledger(self) -> list[dict[str, Any]]:
        """Return a credential-free copy of completed HTTP operations."""
        with self._state_lock:
            return copy.deepcopy(self._ledger)

    def _validate_binding(
        self, bucket: Any, key: Any, version_id: Any,
    ) -> tuple[str, str]:
        if bucket != SOURCE_BUCKET:
            _fail("SOURCE_BUCKET_MISMATCH", "bucket is not the fixed source")
        return _safe_key(key), _exact_version(version_id)

    def _signed_request(self, method: str, key: str, version_id: str) -> Any:
        if method not in ("HEAD", "GET"):
            _fail("METHOD_REFUSED", "only HEAD and GET are implemented")
        key = _safe_key(key)
        version_id = _exact_version(version_id)
        credentials = self._credentials.current()
        if not isinstance(credentials, tuple) or len(credentials) != 3:
            _fail("CREDENTIAL_PROVIDER_INVALID", "current() shape is invalid")
        key_id, secret, session_token = credentials
        if not all(isinstance(value, str) and value for value in credentials):
            _fail("CREDENTIAL_PROVIDER_INVALID", "temporary credentials incomplete")

        amzdate = _utcnow().strftime("%Y%m%dT%H%M%SZ")
        datestamp = amzdate[:8]
        canonical_uri = "/" + urllib.parse.quote(key, safe="/-_.~")
        canonical_query = "versionId=" + urllib.parse.quote(
            version_id, safe="-_.~",
        )
        headers = {
            "host": self._host,
            "x-amz-content-sha256": EMPTY_SHA256,
            "x-amz-date": amzdate,
            "x-amz-security-token": session_token,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{headers[name]}\n" for name in sorted(headers)
        )
        canonical_request = "\n".join([
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            EMPTY_SHA256,
        ])
        scope = f"{datestamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amzdate,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signing_key = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
        for part in (self._region, "s3", "aws4_request"):
            signing_key = _hmac(signing_key, part)
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = urllib.request.Request(
            f"https://{self._host}{canonical_uri}?{canonical_query}",
            method=method,
        )
        for name, value in headers.items():
            if name != "host":
                request.add_header(name, value)
        try:
            return self._urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", "unknown")
            exc.close()
            _fail("S3_HTTP_ERROR", f"{method} {key} returned HTTP {status}")
        except (OSError, TimeoutError) as exc:
            _fail("S3_TRANSPORT_ERROR", f"{method} {key} failed: {type(exc).__name__}")

    def _record(
        self, method: str, key: str, version_id: str,
        metadata: dict[str, Any],
    ) -> None:
        row = {
            "method": method,
            "key": key,
            "version_id": version_id,
            "http_status": metadata["http_status"],
            "request_id": metadata["request_id"],
        }
        with self._state_lock:
            self._ledger.append(row)

    def _reserve_head(self, key: str, version_id: str) -> None:
        exact = (key, version_id)
        with self._state_lock:
            prior = self._head_bindings.get(exact)
            if prior is not None and prior["state"] in (
                "HEAD_IN_FLIGHT", "READY", "GET_IN_FLIGHT",
            ):
                _fail(
                    "HEAD_SEQUENCE_INVALID",
                    "same key/version already has an active HEAD-to-GET binding",
                )
            self._head_bindings[exact] = {"state": "HEAD_IN_FLIGHT"}

    def _abort_head(self, key: str, version_id: str) -> None:
        exact = (key, version_id)
        with self._state_lock:
            current = self._head_bindings.get(exact)
            if current is not None and current["state"] == "HEAD_IN_FLIGHT":
                del self._head_bindings[exact]

    def _complete_head(
        self, key: str, version_id: str, metadata: dict[str, Any],
    ) -> None:
        exact = (key, version_id)
        with self._state_lock:
            current = self._head_bindings.get(exact)
            if current is None or current["state"] != "HEAD_IN_FLIGHT":
                _fail("HEAD_SEQUENCE_INVALID", "HEAD reservation was lost")
            self._head_bindings[exact] = {
                "state": "READY",
                "response_version_id": metadata["VersionId"],
                "content_length": metadata["ContentLength"],
            }

    def _claim_head_for_get(self, key: str, version_id: str) -> dict[str, Any]:
        exact = (key, version_id)
        with self._state_lock:
            current = self._head_bindings.get(exact)
            if current is None:
                _fail(
                    "HEAD_BINDING_REQUIRED",
                    "GET requires a prior successful HEAD for the same key/version",
                )
            if current["state"] == "CONSUMED":
                _fail(
                    "HEAD_BINDING_CONSUMED",
                    "the prior HEAD for this key/version was already consumed",
                )
            if current["state"] != "READY":
                _fail(
                    "HEAD_BINDING_NOT_READY",
                    "the HEAD-to-GET binding is not ready",
                )
            binding = copy.deepcopy(current)
            current["state"] = "GET_IN_FLIGHT"
            return binding

    def _finish_get_binding(self, key: str, version_id: str) -> None:
        exact = (key, version_id)
        with self._state_lock:
            current = self._head_bindings.get(exact)
            if current is not None and current["state"] == "GET_IN_FLIGHT":
                current["state"] = "CONSUMED"

    def head(self, bucket: str, key: str, version_id: str) -> dict[str, Any]:
        """HEAD one exact S3 version and return the reader's identity fields."""
        key, version_id = self._validate_binding(bucket, key, version_id)
        self._reserve_head(key, version_id)
        try:
            response = self._signed_request("HEAD", key, version_id)
            try:
                metadata = _response_metadata(response, method="HEAD", key=key)
            finally:
                response.close()
            self._record("HEAD", key, version_id, metadata)
            if metadata["VersionId"] != version_id:
                _fail(
                    "HEAD_VERSION_MISMATCH",
                    "HEAD response VersionId differs from the requested version",
                )
            self._complete_head(key, version_id, metadata)
        except BaseException:
            self._abort_head(key, version_id)
            raise
        return {
            "VersionId": metadata["VersionId"],
            "ContentLength": metadata["ContentLength"],
        }

    def get_exact(
        self,
        bucket: str,
        key: str,
        version_id: str,
        destination: str | os.PathLike[str],
    ) -> dict[str, Any]:
        """Stream one exact S3 version into an existing regular file.

        A successful same-key/version HEAD must precede this call.  Its
        VersionId and Content-Length are checked before the first body read,
        and the one-use binding is consumed even on failure.  The destination
        must already exist and be empty.  The adapter opens that inode without
        ``O_CREAT`` or replacement and never changes its or its parent's mode.
        """
        key, version_id = self._validate_binding(bucket, key, version_id)
        try:
            path = Path(os.fspath(destination))
            before = path.lstat()
        except (OSError, TypeError, ValueError) as exc:
            _fail("DESTINATION_INVALID", type(exc).__name__)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != 0
        ):
            _fail("DESTINATION_INVALID", "destination must be one empty regular inode")

        flags = os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            _fail("DESTINATION_OPEN_FAILED", type(exc).__name__)
        binding_claimed = False
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_nlink != before.st_nlink
                or opened.st_size != 0
            ):
                _fail("DESTINATION_RACE", "opened inode differs from preflight")

            binding = self._claim_head_for_get(key, version_id)
            binding_claimed = True
            response = self._signed_request("GET", key, version_id)
            try:
                metadata = _response_metadata(response, method="GET", key=key)
                self._record("GET", key, version_id, metadata)
                if metadata["VersionId"] != binding["response_version_id"]:
                    _fail(
                        "GET_RESPONSE_VERSION_MISMATCH",
                        "GET response VersionId differs from the successful HEAD",
                    )
                if metadata["ContentLength"] != binding["content_length"]:
                    _fail(
                        "GET_RESPONSE_LENGTH_MISMATCH",
                        "GET Content-Length differs from the successful HEAD",
                    )
                observed = 0
                while True:
                    chunk = response.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        _fail("RESPONSE_BODY_INVALID", "GET body yielded non-bytes")
                    if observed + len(chunk) > binding["content_length"]:
                        _fail(
                            "GET_BODY_OVERSIZED",
                            "GET body exceeds the successful HEAD Content-Length",
                        )
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            _fail("DESTINATION_WRITE_FAILED", "short local write")
                        view = view[written:]
                    observed += len(chunk)
            finally:
                response.close()
            if observed != binding["content_length"]:
                _fail(
                    "GET_BODY_TRUNCATED",
                    "GET body ended before the successful HEAD Content-Length",
                )
            after = os.fstat(fd)
            try:
                named_after = path.lstat()
            except OSError as exc:
                _fail("DESTINATION_RACE", type(exc).__name__)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_nlink != before.st_nlink
                or after.st_size != observed
                or after.st_mode != before.st_mode
                or named_after.st_dev != before.st_dev
                or named_after.st_ino != before.st_ino
                or named_after.st_mode != before.st_mode
            ):
                _fail("DESTINATION_RACE", "destination inode changed during GET")
        finally:
            if binding_claimed:
                self._finish_get_binding(key, version_id)
            os.close(fd)
        return {
            "VersionId": metadata["VersionId"],
            "ContentLength": metadata["ContentLength"],
        }


__all__ = [
    "SOURCE_BUCKET",
    "W09ExactS3AdapterError",
    "W09ExactVersionS3Client",
]
