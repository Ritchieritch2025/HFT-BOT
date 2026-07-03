#!/usr/bin/env python3
"""Offline verification of signatures captured by mock_server.py.

Usage: verify_captured.py <capture.jsonl> <public_key.pem> <openssl_bin> [sample_n]

Reconstructs each signed message as timestamp + METHOD + path-without-query
and verifies the base64 RSA-PSS signature with the openssl CLI — an
implementation of the check Kalshi's servers perform.
"""
import base64
import json
import random
import subprocess
import sys
import tempfile


def main():
    capture, pub_pem, openssl = sys.argv[1], sys.argv[2], sys.argv[3]
    sample_n = int(sys.argv[4]) if len(sys.argv) > 4 else 50

    with open(capture) as f:
        records = [json.loads(line) for line in f if line.strip()]
    records = [r for r in records if r.get("sig")]
    if not records:
        print("no captured signed requests")
        return 1

    random.seed(7)
    sample = records if len(records) <= sample_n else random.sample(records, sample_n)

    failures = 0
    for r in sample:
        message = (r["ts"] + r["method"] + r["path"].split("?")[0]).encode()
        sig = base64.b64decode(r["sig"])
        with tempfile.NamedTemporaryFile(suffix=".msg") as mf, \
             tempfile.NamedTemporaryFile(suffix=".sig") as sf:
            mf.write(message)
            mf.flush()
            sf.write(sig)
            sf.flush()
            proc = subprocess.run(
                [openssl, "dgst", "-sha256",
                 "-sigopt", "rsa_padding_mode:pss",
                 "-sigopt", "rsa_pss_saltlen:digest",
                 "-verify", pub_pem, "-signature", sf.name, mf.name],
                capture_output=True, text=True)
        if "Verified OK" not in proc.stdout:
            failures += 1
            print(f"FAIL: {message!r}: {proc.stdout.strip()} {proc.stderr.strip()}")

    print(f"verified {len(sample) - failures}/{len(sample)} sampled signatures "
          f"(of {len(records)} captured requests)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
