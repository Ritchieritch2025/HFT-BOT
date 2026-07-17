#!/usr/bin/env python3
"""Offline tests for the W09 exact-VersionId S3 adapter."""

from __future__ import annotations

from email.message import Message
import importlib.util
import hashlib
import inspect
from io import BytesIO
from pathlib import Path
import sys
import urllib.request
from urllib.response import addinfourl
from urllib.parse import parse_qs, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
W09 = ROOT / "deploy" / "w09"
TOOLS = ROOT / "tools"


def _load_adapter():
    sys.path.insert(0, str(TOOLS))
    sys.path.insert(0, str(W09))
    try:
        spec = importlib.util.spec_from_file_location(
            "w09_fresh_rfq_exact_s3_adapter_test",
            W09 / "fresh_rfq_exact_s3_adapter.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(W09))
        sys.path.remove(str(TOOLS))


adapter = _load_adapter()

sys.path.insert(0, str(TOOLS))
try:
    import fresh_rfq_exact_reader as exact_reader  # noqa: E402
finally:
    sys.path.remove(str(TOOLS))


class TemporaryCredentials:
    def __init__(self):
        self.calls = 0

    def current(self):
        self.calls += 1
        return "ASIAEXACTTEST", "not-a-real-secret", "session-token-value"


class FakeResponse:
    def __init__(self, body: bytes, version_id: str, request_id: str):
        self._body = body
        self._offset = 0
        self.status = 200
        self.headers = {
            "x-amz-version-id": version_id,
            "Content-Length": str(len(body)),
            "x-amz-request-id": request_id,
        }
        self.closed = False
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append({"request": request, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


class RedirectChainHTTPSHandler(urllib.request.HTTPSHandler):
    """Offline HTTPS handler that would follow a chain if asked again."""

    handler_order = 100

    def __init__(self, status, location):
        super().__init__()
        self.status = status
        self.location = location
        self.calls = []
        self.responses = []

    def https_open(self, request):
        self.calls.append(request)
        headers = Message()
        if len(self.calls) == 1:
            headers["Location"] = self.location
            response = addinfourl(
                BytesIO(b"redirect-body"), headers, request.full_url,
                code=self.status,
            )
            response.msg = "Redirect"
        else:
            # Reaching this branch means urllib followed Location, which is
            # precisely the credential-bearing second request being refused.
            headers["Content-Length"] = "0"
            response = addinfourl(
                BytesIO(b""), headers, request.full_url, code=200,
            )
            response.msg = "OK"
        self.responses.append(response)
        return response


def _headers(request):
    return {name.lower(): value for name, value in request.header_items()}


def _client(transport, credentials=None):
    return adapter.W09ExactVersionS3Client(
        credentials=TemporaryCredentials() if credentials is None else credentials,
        urlopen=transport,
    )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_default_opener_refuses_redirect_without_sending_location_request(status):
    location = "https://redirect.invalid/intermediate?then=final"
    credentials = TemporaryCredentials()
    client = adapter.W09ExactVersionS3Client(credentials=credentials)
    redirect_transport = RedirectChainHTTPSHandler(status, location)
    assert client._opener is not None
    client._opener.add_handler(redirect_transport)
    key = "ec2/raw/rfq/date=2026-07-16/rfq_redirect.ndjson"
    version = "exact-version-with/slash+plus=="

    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.head(adapter.SOURCE_BUCKET, key, version)

    assert caught.value.code == "S3_REDIRECT_REFUSED"
    assert len(redirect_transport.calls) == 1
    original = redirect_transport.calls[0]
    assert original.get_method() == "HEAD"
    parsed = urlsplit(original.full_url)
    assert parsed.hostname == (
        f"{adapter.SOURCE_BUCKET}.s3.us-east-2.amazonaws.com"
    )
    assert parse_qs(parsed.query, keep_blank_values=True) == {
        "versionId": [version],
    }
    assert all(call.full_url != location for call in redirect_transport.calls)
    headers = _headers(original)
    assert headers["x-amz-security-token"] == "session-token-value"
    assert "x-amz-security-token" in headers["authorization"]
    assert redirect_transport.responses[0].closed is True
    assert client.operation_ledger == []
    rendered = str(caught.value)
    for secret in (
        "ASIAEXACTTEST", "not-a-real-secret", "session-token-value",
        location,
    ):
        assert secret not in rendered


def test_injected_offline_transport_bypasses_default_opener():
    response = FakeResponse(b"", "v1", "head-id")
    transport = FakeTransport([response])
    client = _client(transport)
    assert client._opener is None
    assert client.head(
        adapter.SOURCE_BUCKET, "ec2/raw/rfq/offline", "v1",
    ) == {"VersionId": "v1", "ContentLength": 0}
    assert len(transport.calls) == 1


def test_head_and_get_are_method_and_exact_query_bound_and_token_signed(tmp_path):
    version = "rfq-version/with+reserved=="
    body = b"rfq-body\n"
    head_response = FakeResponse(b"", version, "request-head")
    head_response.headers["Content-Length"] = str(len(body))
    get_response = FakeResponse(body, version, "request-get")
    transport = FakeTransport([head_response, get_response])
    credentials = TemporaryCredentials()
    client = _client(transport, credentials)

    key = "ec2/raw/rfq/date=2026-07-16/rfq_03.ndjson"
    assert client.head(adapter.SOURCE_BUCKET, key, version) == {
        "VersionId": version,
        "ContentLength": len(body),
    }
    destination = tmp_path / "already-created.bin"
    destination.touch(mode=0o600)
    assert client.get_exact(
        adapter.SOURCE_BUCKET, key, version, destination,
    ) == {
        "VersionId": version,
        "ContentLength": len(body),
    }
    assert destination.read_bytes() == body

    assert credentials.calls == 2
    assert [call["request"].get_method() for call in transport.calls] == [
        "HEAD", "GET",
    ]
    for call in transport.calls:
        request = call["request"]
        parsed = urlsplit(request.full_url)
        assert parsed.hostname == (
            f"{adapter.SOURCE_BUCKET}.s3.us-east-2.amazonaws.com"
        )
        assert parse_qs(parsed.query, keep_blank_values=True) == {
            "versionId": [version],
        }
        headers = _headers(request)
        assert headers["x-amz-security-token"] == "session-token-value"
        assert "x-amz-security-token" in headers["authorization"]
        assert "ASIAEXACTTEST" in headers["authorization"]
        assert "not-a-real-secret" not in headers["authorization"]
    assert _headers(transport.calls[0]["request"])["authorization"] != \
        _headers(transport.calls[1]["request"])["authorization"]
    assert head_response.closed is True
    assert get_response.closed is True


def test_get_streams_into_precreated_same_inode_without_chmod_or_replace(tmp_path):
    body = b"x" * (adapter.READ_CHUNK_BYTES * 2 + 17)
    head_response = FakeResponse(b"", "version-1", "request-head")
    head_response.headers["Content-Length"] = str(len(body))
    response = FakeResponse(body, "version-1", "request-get")
    transport = FakeTransport([head_response, response])
    client = _client(transport)
    destination = tmp_path / "exact-version.bin"
    destination.touch(mode=0o640)
    tmp_path.chmod(0o750)
    file_before = destination.stat()
    parent_mode_before = tmp_path.stat().st_mode

    client.head(
        adapter.SOURCE_BUCKET,
        "ec2/raw/rfq/date=2026-07-16/rfq_04.ndjson",
        "version-1",
    )
    result = client.get_exact(
        adapter.SOURCE_BUCKET,
        "ec2/raw/rfq/date=2026-07-16/rfq_04.ndjson",
        "version-1",
        destination,
    )

    file_after = destination.stat()
    assert result == {"VersionId": "version-1", "ContentLength": len(body)}
    assert destination.read_bytes() == body
    assert (file_after.st_dev, file_after.st_ino) == (
        file_before.st_dev, file_before.st_ino,
    )
    assert file_after.st_mode == file_before.st_mode
    assert tmp_path.stat().st_mode == parent_mode_before
    assert len(response.read_sizes) >= 4
    assert set(response.read_sizes) == {adapter.READ_CHUNK_BYTES}


def test_get_without_prior_same_key_version_head_is_refused_before_http(tmp_path):
    transport = FakeTransport([])
    client = _client(transport)
    destination = tmp_path / "empty"
    destination.touch(mode=0o600)

    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.get_exact(
            adapter.SOURCE_BUCKET, "ec2/raw/rfq/object", "v1", destination,
        )

    assert caught.value.code == "HEAD_BINDING_REQUIRED"
    assert destination.read_bytes() == b""
    assert transport.calls == []


def test_head_binding_is_exactly_key_and_version_scoped(tmp_path):
    body = b"exact-binding"
    head_response = FakeResponse(b"", "v1", "head-id")
    head_response.headers["Content-Length"] = str(len(body))
    get_response = FakeResponse(body, "v1", "get-id")
    transport = FakeTransport([head_response, get_response])
    client = _client(transport)
    key = "ec2/raw/rfq/date=2026-07-16/rfq_11.ndjson"
    client.head(adapter.SOURCE_BUCKET, key, "v1")

    for index, (candidate_key, candidate_version) in enumerate((
        (key, "v2"),
        ("ec2/raw/rfq/date=2026-07-16/rfq_12.ndjson", "v1"),
    )):
        destination = tmp_path / f"wrong-binding-{index}"
        destination.touch(mode=0o600)
        with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
            client.get_exact(
                adapter.SOURCE_BUCKET, candidate_key, candidate_version,
                destination,
            )
        assert caught.value.code == "HEAD_BINDING_REQUIRED"
        assert destination.read_bytes() == b""

    valid = tmp_path / "valid-binding"
    valid.touch(mode=0o600)
    client.get_exact(adapter.SOURCE_BUCKET, key, "v1", valid)
    assert valid.read_bytes() == body
    assert len(transport.calls) == 2


def test_mismatched_head_response_is_not_a_successful_get_binding(tmp_path):
    head_response = FakeResponse(b"", "different-version", "head-id")
    head_response.headers["Content-Length"] = "4"
    transport = FakeTransport([head_response])
    client = _client(transport)
    key = "ec2/raw/rfq/date=2026-07-16/rfq_13.ndjson"

    with pytest.raises(adapter.W09ExactS3AdapterError) as head_error:
        client.head(adapter.SOURCE_BUCKET, key, "v1")
    assert head_error.value.code == "HEAD_VERSION_MISMATCH"
    assert [row["method"] for row in client.operation_ledger] == ["HEAD"]

    destination = tmp_path / "after-failed-head"
    destination.touch(mode=0o600)
    with pytest.raises(adapter.W09ExactS3AdapterError) as get_error:
        client.get_exact(adapter.SOURCE_BUCKET, key, "v1", destination)
    assert get_error.value.code == "HEAD_BINDING_REQUIRED"
    assert destination.read_bytes() == b""
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("version", "GET_RESPONSE_VERSION_MISMATCH"),
        ("length", "GET_RESPONSE_LENGTH_MISMATCH"),
    ],
)
def test_get_response_must_match_head_before_any_body_read_or_write(
    tmp_path, fault, code,
):
    body = b"bound-body"
    head_response = FakeResponse(b"", "v1", "head-id")
    head_response.headers["Content-Length"] = str(len(body))
    get_response = FakeResponse(body, "v1", "get-id")
    if fault == "version":
        get_response.headers["x-amz-version-id"] = "different-version"
    else:
        get_response.headers["Content-Length"] = str(len(body) + 1)
    transport = FakeTransport([head_response, get_response])
    client = _client(transport)
    key = "ec2/raw/rfq/date=2026-07-16/rfq_07.ndjson"
    client.head(adapter.SOURCE_BUCKET, key, "v1")
    destination = tmp_path / f"mismatch-{fault}"
    destination.touch(mode=0o600)

    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.get_exact(adapter.SOURCE_BUCKET, key, "v1", destination)

    assert caught.value.code == code
    assert get_response.read_sizes == []
    assert destination.read_bytes() == b""
    assert get_response.closed is True
    assert [row["method"] for row in client.operation_ledger] == ["HEAD", "GET"]

    retry = tmp_path / f"mismatch-{fault}-retry"
    retry.touch(mode=0o600)
    with pytest.raises(adapter.W09ExactS3AdapterError) as retry_error:
        client.get_exact(adapter.SOURCE_BUCKET, key, "v1", retry)
    assert retry_error.value.code == "HEAD_BINDING_CONSUMED"
    assert retry.read_bytes() == b""
    assert len(transport.calls) == 2


def test_oversized_get_stops_before_writing_the_excess_chunk(tmp_path):
    expected = b"x" * adapter.READ_CHUNK_BYTES
    excess = b"MUST-NOT-BE-WRITTEN"
    head_response = FakeResponse(b"", "v1", "head-id")
    head_response.headers["Content-Length"] = str(len(expected))
    get_response = FakeResponse(expected + excess, "v1", "get-id")
    # Simulate a response whose declared length matches HEAD but whose stream
    # contains trailing bytes.  The first full chunk is valid; the next chunk
    # must be rejected before any of it reaches the destination inode.
    get_response.headers["Content-Length"] = str(len(expected))
    transport = FakeTransport([head_response, get_response])
    client = _client(transport)
    key = "ec2/raw/rfq/date=2026-07-16/rfq_08.ndjson"
    client.head(adapter.SOURCE_BUCKET, key, "v1")
    destination = tmp_path / "oversized"
    destination.touch(mode=0o600)

    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.get_exact(adapter.SOURCE_BUCKET, key, "v1", destination)

    assert caught.value.code == "GET_BODY_OVERSIZED"
    assert destination.read_bytes() == expected
    assert excess not in destination.read_bytes()
    assert get_response.read_sizes == [
        adapter.READ_CHUNK_BYTES, adapter.READ_CHUNK_BYTES,
    ]
    assert [row["method"] for row in client.operation_ledger] == ["HEAD", "GET"]


def test_truncated_get_is_rejected_after_stream_ends(tmp_path):
    expected_length = 20
    partial = b"only-partial"
    head_response = FakeResponse(b"", "v1", "head-id")
    head_response.headers["Content-Length"] = str(expected_length)
    get_response = FakeResponse(partial, "v1", "get-id")
    get_response.headers["Content-Length"] = str(expected_length)
    transport = FakeTransport([head_response, get_response])
    client = _client(transport)
    key = "ec2/raw/rfq/date=2026-07-16/rfq_09.ndjson"
    client.head(adapter.SOURCE_BUCKET, key, "v1")
    destination = tmp_path / "truncated"
    destination.touch(mode=0o600)

    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.get_exact(adapter.SOURCE_BUCKET, key, "v1", destination)

    assert caught.value.code == "GET_BODY_TRUNCATED"
    assert destination.read_bytes() == partial
    assert len(get_response.read_sizes) == 2
    assert [row["method"] for row in client.operation_ledger] == ["HEAD", "GET"]


def test_one_successful_head_can_be_consumed_by_only_one_get(tmp_path):
    body = b"once"
    head_response = FakeResponse(b"", "v1", "head-id")
    head_response.headers["Content-Length"] = str(len(body))
    get_response = FakeResponse(body, "v1", "get-id")
    transport = FakeTransport([head_response, get_response])
    client = _client(transport)
    key = "ec2/raw/rfq/date=2026-07-16/rfq_10.ndjson"
    client.head(adapter.SOURCE_BUCKET, key, "v1")
    first = tmp_path / "first"
    first.touch(mode=0o600)
    client.get_exact(adapter.SOURCE_BUCKET, key, "v1", first)
    second = tmp_path / "second"
    second.touch(mode=0o600)

    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.get_exact(adapter.SOURCE_BUCKET, key, "v1", second)

    assert caught.value.code == "HEAD_BINDING_CONSUMED"
    assert first.read_bytes() == body
    assert second.read_bytes() == b""
    assert len(transport.calls) == 2


@pytest.mark.parametrize("version", [None, "", " ", "null", "NULL", "v\n2"])
def test_null_or_invalid_version_is_rejected_before_transport(version):
    transport = FakeTransport([])
    client = _client(transport)
    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.head(adapter.SOURCE_BUCKET, "ec2/raw/rfq/object", version)
    assert caught.value.code == "VERSION_REQUIRED"
    assert transport.calls == []


def test_wrong_bucket_is_rejected_before_transport(tmp_path):
    transport = FakeTransport([])
    client = _client(transport)
    with pytest.raises(adapter.W09ExactS3AdapterError) as caught:
        client.get_exact(
            "some-copy-bucket", "ec2/raw/rfq/object", "version-1",
            tmp_path / "not-created",
        )
    assert caught.value.code == "SOURCE_BUCKET_MISMATCH"
    assert transport.calls == []


def test_get_requires_existing_empty_regular_inode(tmp_path):
    transport = FakeTransport([])
    client = _client(transport)
    missing = tmp_path / "missing"
    with pytest.raises(adapter.W09ExactS3AdapterError) as missing_error:
        client.get_exact(
            adapter.SOURCE_BUCKET, "ec2/raw/rfq/object", "v1", missing,
        )
    assert missing_error.value.code == "DESTINATION_INVALID"

    nonempty = tmp_path / "nonempty"
    nonempty.write_bytes(b"do-not-overwrite")
    inode = nonempty.stat().st_ino
    with pytest.raises(adapter.W09ExactS3AdapterError) as nonempty_error:
        client.get_exact(
            adapter.SOURCE_BUCKET, "ec2/raw/rfq/object", "v1", nonempty,
        )
    assert nonempty_error.value.code == "DESTINATION_INVALID"
    assert nonempty.read_bytes() == b"do-not-overwrite"
    assert nonempty.stat().st_ino == inode
    assert transport.calls == []


def test_operation_ledger_is_credential_free_and_has_only_allowed_fields(tmp_path):
    body = b"one"
    head_response = FakeResponse(b"", "v1", "head-id")
    head_response.headers["Content-Length"] = str(len(body))
    get_response = FakeResponse(body, "v1", "get-id")
    client = _client(FakeTransport([head_response, get_response]))
    key = "ec2/raw/rfq/date=2026-07-16/rfq_05.ndjson"
    client.head(adapter.SOURCE_BUCKET, key, "v1")
    destination = tmp_path / "body"
    destination.touch(mode=0o600)
    client.get_exact(adapter.SOURCE_BUCKET, key, "v1", destination)

    ledger = client.operation_ledger
    assert ledger == [
        {
            "method": "HEAD", "key": key, "version_id": "v1",
            "http_status": 200, "request_id": "head-id",
        },
        {
            "method": "GET", "key": key, "version_id": "v1",
            "http_status": 200, "request_id": "get-id",
        },
    ]
    assert all(set(row) == {
        "method", "key", "version_id", "http_status", "request_id",
    } for row in ledger)
    rendered = repr(ledger)
    assert "ASIAEXACTTEST" not in rendered
    assert "not-a-real-secret" not in rendered
    assert "session-token-value" not in rendered
    ledger[0]["method"] = "tampered"
    assert client.operation_ledger[0]["method"] == "HEAD"


def test_exact_read_session_accepts_adapter_but_keeps_external_audit_gate(tmp_path):
    body = b'{"request_id":"fresh-one"}\n'
    version = "fresh-version-1"
    key = "ec2/raw/rfq/date=2026-07-16/rfq_06.ndjson"
    identity = {
        "bucket": adapter.SOURCE_BUCKET,
        "key": key,
        "version_id": version,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    head_response = FakeResponse(b"", version, "head-session-id")
    head_response.headers["Content-Length"] = str(len(body))
    get_response = FakeResponse(body, version, "get-session-id")
    client = _client(FakeTransport([head_response, get_response]))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    session = exact_reader.ExactReadSession(
        [identity], client,
        transport_kind="W09_S3_EXACT_VERSION_ADAPTER",
        temp_parent=scratch,
    )

    with session as active:
        with active.open_exact(identity) as opened:
            assert opened.path.read_bytes() == body

    assert [row["method"] for row in client.operation_ledger] == ["HEAD", "GET"]
    assert session.attestation["transport_attestation_state"] == \
        "CALLER_ADAPTER_UNVERIFIED"
    assert session.attestation["source_objects_exact_get_verified"] is False
    assert session.attestation["requires_external_iam_and_operation_audit"] is True


def test_client_public_callable_surface_has_no_list_latest_or_write_api():
    public_callables = {
        name for name, value in inspect.getmembers(
            adapter.W09ExactVersionS3Client,
        )
        if not name.startswith("_") and callable(value)
    }
    assert public_callables == {"head", "get_exact"}
    source = (W09 / "fresh_rfq_exact_s3_adapter.py").read_text()
    for forbidden in (
        "def list(", "def get_latest(", "def put(", "def copy(",
        "def delete(", "def upload(", "os.replace(", "os.chmod(",
    ):
        assert forbidden not in source


def test_module_does_not_claim_live_iam_was_audited():
    source = (W09 / "fresh_rfq_exact_s3_adapter.py").read_text()
    assert "does not\nclaim that W09's live IAM policy" in source
    assert "iam_audited" not in source.lower()
    assert "iam_verified" not in source.lower()
