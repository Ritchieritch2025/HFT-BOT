#!/usr/bin/env python3
"""FAKE-AWS: a stub aws-CLI double for PIPE-W05 P0-1 tests (NOT LocalDest).

Simulates a VERSIONED S3 store on local disk with the exact subcommand
surface tools/research_release.py uses (s3 sync / s3 ls / s3api head-object /
get-object --version-id / put-object --if-none-match), so the publisher's
real-S3 code path — version-pinned post-upload verification and the
write-once conditional-create manifest — runs against controllable races:

  FAKE_S3_ROOT          store directory (required)
  FAKE_S3_UNVERSIONED=1 store returns NO VersionIds (publisher must abort
                        fail-closed: VERSIONING_REQUIRED, P0-2)
  FAKE_S3_RACE_KEY=sub  after every `s3 sync`, a concurrent writer appends a
                        NEW corrupt version to any key containing `sub`
                        (HEAD then returns version B; the publisher's exact-
                        version GET of B must mismatch and abort)
  FAKE_S3_MANIFEST_RACE=1  `s3 ls` claims MANIFEST.json is absent even when
                        it exists — forcing the publisher through the
                        If-None-Match conditional create, which must refuse
                        the overwrite (write-once)

Object layout: <root>/<bucket>/<key>/__obj__/V000001, V000002…
(latest = highest). stdlib only.
"""
import json
import os
import shutil
import sys


def _root():
    r = os.environ.get("FAKE_S3_ROOT")
    if not r:
        sys.stderr.write("fake_aws: FAKE_S3_ROOT not set\n")
        raise SystemExit(70)
    return r


def _obj_dir(bucket, key):
    return os.path.join(_root(), bucket, *key.split("/"), "__obj__")


def _versions(bucket, key):
    d = _obj_dir(bucket, key)
    if not os.path.isdir(d):
        return []
    return sorted(v for v in os.listdir(d) if v.startswith("V"))


def _put_version(bucket, key, payload):
    d = _obj_dir(bucket, key)
    os.makedirs(d, exist_ok=True)
    vid = "V%06d" % (len(_versions(bucket, key)) + 1)
    with open(os.path.join(d, vid), "wb") as f:
        f.write(payload)
    return vid


def _iter_keys(bucket, prefix=""):
    broot = os.path.join(_root(), bucket)
    for base, dirs, _files in os.walk(broot):
        if os.path.basename(base) == "__obj__":
            dirs[:] = []
            key = os.path.relpath(os.path.dirname(base),
                                  broot).replace(os.sep, "/")
            if key.startswith(prefix):
                yield key


def _parse_url(url):
    rest = url[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def _apply_race(bucket):
    sub = os.environ.get("FAKE_S3_RACE_KEY")
    if not sub:
        return
    for key in list(_iter_keys(bucket)):
        if sub in key:
            _put_version(bucket, key, b"RACED-CONCURRENT-WRITER-BYTES")


def cmd_s3(args):
    if args[0] == "sync":
        src, url = args[1], args[2]
        bucket, prefix = _parse_url(url)
        for base, _d, files in os.walk(src):
            for fn in files:
                p = os.path.join(base, fn)
                rel = os.path.relpath(p, src).replace(os.sep, "/")
                if rel == "MANIFEST.json" and "--exclude" in args:
                    continue
                with open(p, "rb") as f:
                    _put_version(bucket, "%s/%s" % (prefix, rel), f.read())
        _apply_race(bucket)
        return 0
    if args[0] == "ls":
        bucket, key = _parse_url(args[1])
        if (os.environ.get("FAKE_S3_MANIFEST_RACE") == "1"
                and key.endswith("MANIFEST.json")):
            return 1
        hits = [k for k in _iter_keys(bucket) if k == key
                or k.startswith(key.rstrip("/") + "/")]
        if not hits:
            return 1
        for k in hits:
            print("2026-07-12 00:00:00 0 %s" % k.rsplit("/", 1)[-1])
        return 0
    sys.stderr.write("fake_aws: unsupported s3 %r\n" % args)
    return 70


def _flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def cmd_s3api(args):
    op = args[0]
    bucket, key = _flag(args, "--bucket"), _flag(args, "--key")
    unversioned = os.environ.get("FAKE_S3_UNVERSIONED") == "1"
    if op == "head-object":
        vs = _versions(bucket, key)
        if not vs:
            sys.stderr.write("Not Found\n")
            return 254
        path = os.path.join(_obj_dir(bucket, key), vs[-1])
        out = {"ContentLength": os.stat(path).st_size}
        if not unversioned:
            out["VersionId"] = vs[-1]
        print(json.dumps(out))
        return 0
    if op == "get-object":
        vid = _flag(args, "--version-id")
        outfile = args[-1]
        vs = _versions(bucket, key)
        if not vs:
            sys.stderr.write("NoSuchKey\n")
            return 254
        use = vid or vs[-1]
        path = os.path.join(_obj_dir(bucket, key), use)
        if not os.path.isfile(path):
            sys.stderr.write("NoSuchVersion\n")
            return 254
        shutil.copyfile(path, outfile)
        print(json.dumps({"ContentLength": os.stat(path).st_size}))
        return 0
    if op == "put-object":
        body = _flag(args, "--body")
        if "--if-none-match" in args and _versions(bucket, key):
            sys.stderr.write("An error occurred (PreconditionFailed): "
                             "At least one of the pre-conditions you "
                             "specified did not hold\n")
            return 254
        with open(body, "rb") as f:
            vid = _put_version(bucket, key, f.read())
        print(json.dumps({} if unversioned else {"VersionId": vid}))
        return 0
    if op == "list-object-versions":
        prefix = _flag(args, "--prefix") or ""
        versions = []
        for k in _iter_keys(bucket, prefix):
            for v in _versions(bucket, k):
                p = os.path.join(_obj_dir(bucket, k), v)
                versions.append({"Key": k, "VersionId": v,
                                 "Size": os.stat(p).st_size,
                                 "LastModified": "2026-07-12T00:00:00Z",
                                 "IsLatest": v == _versions(bucket, k)[-1]})
        print(json.dumps({} if unversioned else {"Versions": versions}))
        return 0
    sys.stderr.write("fake_aws: unsupported s3api %r\n" % args)
    return 70


def main(argv):
    if not argv:
        return 70
    if argv[0] == "s3":
        return cmd_s3(argv[1:])
    if argv[0] == "s3api":
        return cmd_s3api(argv[1:])
    if argv[0] == "configure":
        return 0
    sys.stderr.write("fake_aws: unsupported %r\n" % argv)
    return 70


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
