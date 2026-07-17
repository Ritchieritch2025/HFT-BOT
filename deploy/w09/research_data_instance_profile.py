#!/usr/bin/env python3
"""W09 entrypoint for the canonical PIPE-W05 ``research_data`` CLI.

The canonical CLI deliberately implements S3 reads with stdlib SigV4.  Its
Mac credential path predates W09 and only accepts a long-lived access key.
W09 must never receive that key, so this entrypoint replaces only the S3
credential/store factory with an IMDSv2 instance-profile implementation.

Security properties:

* IMDSv2 only; IMDSv1 is never attempted.
* the attached role name must be exactly ``w09-research-runner``;
* static AWS credentials and Kalshi trading credentials are refused;
* the session token is included in the SigV4 canonical signed headers;
* credentials are refreshed before expiry and are never printed;
* only the canonical CLI's ListObjectsV2/GetObject/GetObjectVersion calls are
  available.  No S3 write implementation exists in either layer.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import research_data as rd


IMDS_ROOT = "http://169.254.169.254/latest"
EXPECTED_PROFILE = "w09-research-runner"
EXPECTED_ROLE = "w09-research-runner"
REFRESH_SKEW_SECONDS = 300
CANONICAL_MAKE_STORE = rd.make_store


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _request(url: str, *, method: str = "GET", headers=None,
             timeout: float = 2.0) -> bytes:
    request = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def refuse_static_credentials() -> None:
    """W09 is instance-profile-only; a static key is a configuration fault."""
    static_names = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_PROFILE",
    )
    present = [name for name in static_names if os.environ.get(name)]
    legacy_file = os.path.expanduser("~/.kalshi/research_s3.env.sh")
    if present or os.path.exists(legacy_file):
        detail = ",".join(present) if present else legacy_file
        raise SystemExit(
            "REFUSED (W09 credential mode): static AWS credential material "
            "is present (%s). W09 must use only its IMDSv2 instance profile; "
            "no values were read or printed." % detail
        )


class IMDSv2Credentials:
    """Short-lived instance-role credentials with expiry-aware refresh."""

    def __init__(self, expected_profile: str = EXPECTED_PROFILE,
                 expected_role: str = EXPECTED_ROLE):
        self.expected_profile = expected_profile
        self.expected_role = expected_role
        self.key_id = None
        self.secret = None
        self.session_token = None
        self.expiration = None
        self.role_name = None

    def _metadata_token(self) -> str:
        raw = _request(
            IMDS_ROOT + "/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        token = raw.decode("utf-8").strip()
        if not token:
            raise SystemExit("IMDS_REQUIRED: IMDSv2 returned an empty token")
        return token

    def refresh(self) -> None:
        token = self._metadata_token()
        headers = {"X-aws-ec2-metadata-token": token}
        try:
            info = json.loads(_request(
                IMDS_ROOT + "/meta-data/iam/info", headers=headers
            ))
            profile_arn = info["InstanceProfileArn"]
            profile_name = profile_arn.rsplit("/", 1)[-1]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(
                "IMDS_REQUIRED: invalid instance-profile identity response "
                "(%s)" % exc
            )
        if profile_name != self.expected_profile:
            raise SystemExit(
                "REFUSED (W09 identity): attached instance profile is %r, "
                "expected %r" % (profile_name, self.expected_profile)
            )
        role = _request(
            IMDS_ROOT + "/meta-data/iam/security-credentials/",
            headers=headers,
        ).decode("utf-8").strip()
        if not role or "\n" in role:
            raise SystemExit("REFUSED (W09 identity): invalid role response")
        if role != self.expected_role:
            raise SystemExit(
                "REFUSED (W09 identity): attached role is %r, expected %r" %
                (role, self.expected_role)
            )
        raw = _request(
            IMDS_ROOT + "/meta-data/iam/security-credentials/" +
            urllib.parse.quote(role, safe="-_.~"),
            headers=headers,
        )
        try:
            payload = json.loads(raw)
            if payload.get("Code") != "Success":
                raise ValueError("credential status is not Success")
            expiration = datetime.datetime.fromisoformat(
                payload["Expiration"].replace("Z", "+00:00")
            )
            key_id = payload["AccessKeyId"]
            secret = payload["SecretAccessKey"]
            session_token = payload["Token"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(
                "IMDS_REQUIRED: invalid instance-profile credential response "
                "(%s); credential values were not printed" % exc
            )
        if not key_id or not secret or not session_token:
            raise SystemExit(
                "IMDS_REQUIRED: incomplete temporary credentials; values were "
                "not printed"
            )
        self.role_name = role
        self.key_id = key_id
        self.secret = secret
        self.session_token = session_token
        self.expiration = expiration

    def current(self):
        if (self.expiration is None or
                self.expiration - _utcnow() <= datetime.timedelta(
                    seconds=REFRESH_SKEW_SECONDS)):
            self.refresh()
        return self.key_id, self.secret, self.session_token


class InstanceProfileS3Store(rd.S3Store):
    """Canonical read-only S3 store with temporary-token SigV4 support."""

    def __init__(self, bucket, prefix, region, credentials):
        self.credentials = credentials
        key_id, secret, _token = credentials.current()
        super().__init__(bucket, prefix, region, key_id, secret)

    def _signed_request(self, key="", query=None):
        self.key_id, self.secret, session_token = self.credentials.current()
        query = dict(query or {})
        amzdate = _utcnow().strftime("%Y%m%dT%H%M%SZ")
        datestamp = amzdate[:8]
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_uri = "/" + urllib.parse.quote(key, safe="/-_.~")
        canonical_qs = "&".join(
            "%s=%s" % (urllib.parse.quote(str(k), safe="-_.~"),
                        urllib.parse.quote(str(v), safe="-_.~"))
            for k, v in sorted(query.items()))
        headers = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amzdate,
            "x-amz-security-token": session_token,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            "%s:%s\n" % (name, headers[name]) for name in sorted(headers)
        )
        canonical_request = "\n".join([
            "GET", canonical_uri, canonical_qs, canonical_headers,
            signed_headers, payload_hash,
        ])
        scope = "%s/%s/s3/aws4_request" % (datestamp, self.region)
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amzdate, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signing_key = rd._hmac(("AWS4" + self.secret).encode(), datestamp)
        for part in (self.region, "s3", "aws4_request"):
            signing_key = rd._hmac(signing_key, part)
        signature = hmac.new(
            signing_key, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        headers["Authorization"] = (
            "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, "
            "Signature=%s" %
            (self.key_id, scope, signed_headers, signature)
        )
        url = "https://%s%s" % (self.host, canonical_uri)
        if canonical_qs:
            url += "?" + canonical_qs
        request = urllib.request.Request(url)
        for name, value in headers.items():
            if name != "host":
                request.add_header(name, value)
        try:
            return urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as exc:
            body = exc.read(2048).decode("utf-8", "replace")
            code = re.search(r"<Code>([^<]+)</Code>", body)
            raise SystemExit(
                "S3 %s on GET %s: %s (credential values never printed)" %
                (exc.code, key or "(list)",
                 code.group(1) if code else body)
            )


def make_instance_profile_store(root):
    if not root.startswith("s3://"):
        return CANONICAL_MAKE_STORE(root)
    match = re.match(r"^s3://([^/]+)/?(.*)$", root)
    if not match:
        raise SystemExit("invalid S3 root: %s" % root)
    refuse_static_credentials()
    region = os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
    credentials = IMDSv2Credentials()
    return InstanceProfileS3Store(
        match.group(1), match.group(2), region, credentials
    )


def main(argv=None):
    refuse_static_credentials()
    rd.make_store = make_instance_profile_store
    effective_argv = sys.argv if argv is None else argv
    return rd.main(effective_argv)


if __name__ == "__main__":
    raise SystemExit(main())
