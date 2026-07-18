import copy
import hashlib
import json
import os
import subprocess
import sys
from unittest import mock

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import canonical_eligibility_tagger as cet  # noqa: E402
import canonical_receipt_control as crc  # noqa: E402
import canonical_receipts as cr  # noqa: E402
import research_release as rr  # noqa: E402


DATE = "2026-07-12"
LATE_RECEIPT_DATE = "2026-07-14"
BUCKET = "fixture-canonical"
PREFIX = "ec2"
MODIFIED = "2026-07-13T04:00:00Z"
ROLE_ARN = "arn:aws:iam::123456789012:role/canonical-eligibility-tagger"
CALLER_ARN = ("arn:aws:sts::123456789012:assumed-role/"
              "canonical-eligibility-tagger/fixture-session")
USER_ARN = "arn:aws:iam::123456789012:user/canonical-eligibility-tagger"


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _object(*, key, logical, payload, kind, family, candidate, channel=None,
            exposure=None):
    return {
        "bucket": BUCKET,
        "key": key,
        "VersionId": "version-%s" % _sha(key.encode())[:16],
        "size": len(payload),
        "sha256": _sha(payload),
        "last_modified_utc": "2026-07-13T03:00:00Z",
        "logical_source_key": logical,
        "source_kind": kind,
        "family": family,
        "table": None,
        "channel": channel,
        "date": DATE,
        "seal_binding": None,
        "evidence_binding": {"fixture": True},
        "durability_verified": True,
        "research_eligible": False,
        "eligibility_tag_state": "SHADOW_NOT_TAGGED",
        "mutable_source": False,
        "required": True,
        "attestation_class": "DAY_SEAL_ATTESTED",
        "durability_scope": True,
        "research_candidate": candidate,
        "exposure_policy": exposure or (
            "PENDING_ELIGIBILITY_TAG" if candidate else "FORBIDDEN_RAW"),
        "version_resolution": "EXACT_VERSION",
        "canonical_source": "FIXTURE_CANONICAL",
        "verification_state": "EXACT_VERSION_FULL_SHA256",
        "verified_at_utc": "2026-07-13T03:30:00Z",
        "publisher_code_commit": "a" * 40,
    }


def _family(name, objects, policy="REQUIRED_CORE"):
    rows = [obj for obj in objects if obj["family"] == name]
    projection = [{
        "bucket": row["bucket"], "key": row["key"],
        "size": row["size"], "sha256": row["sha256"],
    } for row in rows]
    projection.sort(key=lambda row: (row["bucket"], row["key"]))
    return {
        "name": name,
        "policy": policy,
        "expected_basis": "fixture",
        "expected_count": len(rows),
        "observed_count": len(rows),
        "state": "PRESENT_VERIFIED",
        "reason_code": "NONE",
        "semantic_sha256": cr.canonical_sha256([]),
        "objects_digest": cr.canonical_sha256(projection),
    }


class ExactReceiptReader:
    def __init__(self, binding, body):
        self.binding = copy.deepcopy(binding)
        self.body = body
        self.ops = []

    def head(self, bucket, key, version_id):
        self.ops.append(("head", bucket, key, version_id))
        if (bucket, key, version_id) != (
                self.binding["bucket"], self.binding["key"],
                self.binding["VersionId"]):
            raise RuntimeError("missing exact receipt")
        return {
            "VersionId": version_id,
            "ContentLength": len(self.body),
            "LastModified": self.binding["last_modified_utc"],
        }

    def get_exact(self, bucket, key, version_id, destination):
        self.ops.append(("get", bucket, key, version_id))
        if (bucket, key, version_id) != (
                self.binding["bucket"], self.binding["key"],
                self.binding["VersionId"]):
            raise RuntimeError("missing exact receipt")
        with open(destination, "wb") as handle:
            handle.write(self.body)


class MultiExactReceiptReader:
    def __init__(self, rows):
        self.rows = {
            (binding["bucket"], binding["key"], binding["VersionId"]):
                (copy.deepcopy(binding), body)
            for binding, body in rows
        }

    def head(self, bucket, key, version_id):
        binding, body = self.rows[(bucket, key, version_id)]
        return {
            "VersionId": version_id,
            "ContentLength": len(body),
            "LastModified": binding["last_modified_utc"],
        }

    def get_exact(self, bucket, key, version_id, destination):
        _binding, body = self.rows[(bucket, key, version_id)]
        with open(destination, "wb") as handle:
            handle.write(body)


class PublisherExactTagReader:
    def get_tags(self, _bucket, key, version_id):
        tags = [{"Key": cet.TAG_KEY, "Value": cet.TAG_VALUE}]
        if "/raw/" in ("/" + key):
            tags.append({"Key": cet.RFQ_TAG_KEY, "Value": cet.RFQ_TAG_VALUE})
        return {
            "VersionId": version_id,
            "TagSet": tags,
        }


class FakeTagger:
    def __init__(self, tags=None, fail_get_at=None, fail_put_at=None,
                 identity=None):
        self.tags = copy.deepcopy(tags or {})
        self.gets = []
        self.puts = []
        self.events = []
        self.fail_get_at = fail_get_at
        self.fail_put_at = fail_put_at
        self.identity = copy.deepcopy(identity or {
            "Account": "123456789012",
            "Arn": CALLER_ARN,
            "UserId": "AROAFIXTURE:fixture-session",
        })
        self.identity_calls = 0

    def get_caller_identity(self):
        self.identity_calls += 1
        return copy.deepcopy(self.identity)

    def get_tags(self, bucket, key, version_id):
        self.gets.append((bucket, key, version_id))
        self.events.append(("get", bucket, key, version_id))
        if self.fail_get_at == len(self.gets):
            raise cr.ReceiptError("FIXTURE_GET_FAILED", key)
        return copy.deepcopy(self.tags.get((bucket, key, version_id), {}))

    def put_tags(self, bucket, key, version_id, tags):
        self.puts.append((bucket, key, version_id, copy.deepcopy(tags)))
        self.events.append(("put", bucket, key, version_id))
        if self.fail_put_at == len(self.puts):
            raise cr.ReceiptError("FIXTURE_PUT_FAILED", key)
        self.tags[(bucket, key, version_id)] = copy.deepcopy(tags)


class FakeReceiptWriter:
    def __init__(self):
        self.objects = {}
        self.calls = []

    @staticmethod
    def _binding(bucket, key, body):
        return {
            "bucket": bucket,
            "key": key,
            "VersionId": "receipt-%s" % _sha(key.encode())[:16],
            "size": len(body),
            "sha256": _sha(body),
            "last_modified_utc": MODIFIED,
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        }

    def ensure_equivalent_bytes(self, bucket, key, body, validator):
        self.calls.append((bucket, key, body))
        identity = (bucket, key)
        if identity in self.objects:
            stored = self.objects[identity]
            if not validator(stored):
                raise cr.ReceiptError("CONTROL_CONFLICT", key)
        else:
            self.objects[identity] = body
            stored = body
        return self._binding(bucket, key, stored), stored


class FailingReceiptWriter(FakeReceiptWriter):
    def ensure_equivalent_bytes(self, bucket, key, body, validator):
        self.calls.append((bucket, key, body))
        raise cr.ReceiptError("FIXTURE_RECEIPT_PUT_FAILED", key)


def _write_index_and_receipt(root, receipt):
    body = cet._json_bytes(receipt)
    digest = receipt["receipt_set_sha256"]
    binding = {
        "bucket": BUCKET,
        "key": ("%s/control/canonical-receipts/v1/date=%s/"
                "receipt-%s.json") % (PREFIX, DATE, digest),
        "VersionId": "byte-receipt-version",
        "size": len(body),
        "sha256": _sha(body),
        "last_modified_utc": MODIFIED,
        "verification_state": "EXACT_VERSION_FULL_SHA256",
    }
    index = {
        "schema_version": crc.DURABLE_INDEX_SCHEMA,
        "state": crc.DURABLE_STATE,
        "date": DATE,
        "receipt_set_sha256": digest,
        "receipt_object": binding,
        "receipt_payload_size": len(body),
        "receipt_payload_sha256": _sha(body),
        "complete": True,
        "prune_eligible": False,
    }
    path = root / "DURABLE-byte.json"
    path.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
    return path, index, binding, body


def _write_audit(root, receipt_sha):
    evidence = (json.dumps({
        "schema_version": "fixture-iam-policy-evidence-v1",
        "exclusive_writer": ROLE_ARN,
        "versionless_tagging_denied": True,
    }, sort_keys=True, indent=2) + "\n").encode()
    evidence_path = root / "single-writer-policy-evidence.json"
    evidence_path.write_bytes(evidence)
    audit = {
        "schema_version": cet.AUDIT_SCHEMA,
        "state": cet.AUDIT_STATE,
        "date": DATE,
        "bucket": BUCKET,
        "receipt_set_sha256": receipt_sha,
        "tag_key": cet.TAG_KEY,
        "dedicated_tagger_principal": ROLE_ARN,
        "dedicated_tagger_sts_caller_arn": CALLER_ARN,
        "exclusive_exact_version_writer": True,
        "versionless_tagging_denied": True,
        "other_automation_tag_writers_denied": True,
        "audited_at_utc": "2026-07-13T03:45:00Z",
        "auditor": "fixture-operator",
        "policy_evidence_size": len(evidence),
        "policy_evidence_sha256": _sha(evidence),
    }
    path = root / "single-writer-audit.json"
    path.write_text(json.dumps(audit, sort_keys=True, indent=2) + "\n")
    return path, audit, evidence_path


@pytest.fixture
def receipt_tree(tmp_path):
    seal_payload = b'{"sealed":true}\n'
    fact_payload = b"parquet-fixture"
    rfq_payload = b'{"rfq":true}\n'
    seal = _object(
        key="%s/warehouse/seals/date=%s.json" % (PREFIX, DATE),
        logical="warehouse/seals/date=%s.json" % DATE,
        payload=seal_payload, kind="seal", family="seal", candidate=True)
    fact = _object(
        key="%s/warehouse/facts/trades/date=%s/part.parquet" %
            (PREFIX, DATE),
        logical="warehouse/facts/trades/date=%s/part.parquet" % DATE,
        payload=fact_payload, kind="facts", family="facts", candidate=True)
    rfq = _object(
        key="%s/raw/date=%s/rfq_23.ndjson" % (PREFIX, DATE),
        logical="raw/date=%s/rfq_23.ndjson" % DATE,
        payload=rfq_payload, kind="raw_rfq", family="raw_durability",
        candidate=False, channel="rfq", exposure="FORBIDDEN_RFQ_DEFAULT")
    rfq_receipts = _object(
        key="%s/raw/date=%s/rfq_receipts_23.ndjson" % (PREFIX, DATE),
        logical="raw/date=%s/rfq_receipts_23.ndjson" % DATE,
        payload=b'{"rfq_receipt":true}\n', kind="raw_rfq_receipts",
        family="raw_durability", candidate=False, channel="rfq",
        exposure="FORBIDDEN_RFQ_DEFAULT")
    rfq["seal_binding"] = seal["sha256"]
    rfq_receipts["seal_binding"] = seal["sha256"]
    for obj in (rfq, rfq_receipts):
        obj["required"] = False
        obj["evidence_binding"] = "seal.raw_files"
    objects = [seal, fact, rfq, rfq_receipts]
    families = [
        _family("seal", objects), _family("facts", objects),
        _family("raw_durability", objects),
    ]
    receipt = {
        "schema_version": cr.RECEIPT_SCHEMA,
        "state": crc.DURABLE_STATE,
        "authority": crc.DURABLE_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": DATE,
        "seal": {
            "date": DATE,
            "status": "SEALED",
            "version": 2,
            "method": "full_v2",
            "bucket": BUCKET,
            "key": seal["key"],
            "VersionId": seal["VersionId"],
            "size": seal["size"],
            "sha256": seal["sha256"],
            "manifest_date_sha256": "c" * 64,
        },
        "families": families,
        "objects": objects,
        "verified_at_utc": "2026-07-13T03:30:00Z",
        "publisher_code_commit": "a" * 40,
    }
    binding = dict(receipt["seal"])
    binding["families"] = families
    receipt["receipt_set_sha256"] = cr.receipt_set_sha256(
        DATE, binding, objects)
    receipt["durability_set_sha256"] = cr.scoped_object_set_sha256(
        objects, "durability_scope")
    receipt["research_candidate_set_sha256"] = \
        cr.scoped_object_set_sha256(objects, "research_candidate")
    index_path, index, receipt_binding, body = _write_index_and_receipt(
        tmp_path, receipt)
    audit_path, audit, evidence_path = _write_audit(
        tmp_path, receipt["receipt_set_sha256"])
    return {
        "root": tmp_path,
        "receipt": receipt,
        "index": index,
        "index_path": index_path,
        "receipt_binding": receipt_binding,
        "body": body,
        "reader": ExactReceiptReader(receipt_binding, body),
        "audit_path": audit_path,
        "audit": audit,
        "evidence_path": evidence_path,
        "seal_payload": seal_payload,
    }


def _replace_byte_receipt(tree, receipt):
    for family in receipt["families"]:
        rows = [obj for obj in receipt["objects"]
                if obj["family"] == family["name"]]
        projection = [{
            "bucket": row["bucket"], "key": row["key"],
            "size": row["size"], "sha256": row["sha256"],
        } for row in rows]
        projection.sort(key=lambda row: (row["bucket"], row["key"]))
        family["expected_count"] = len(rows)
        family["observed_count"] = len(rows)
        family["objects_digest"] = cr.canonical_sha256(projection)
    binding = dict(receipt["seal"])
    binding["families"] = receipt["families"]
    receipt["receipt_set_sha256"] = cr.receipt_set_sha256(
        DATE, binding, receipt["objects"])
    receipt["durability_set_sha256"] = cr.scoped_object_set_sha256(
        receipt["objects"], "durability_scope")
    receipt["research_candidate_set_sha256"] = \
        cr.scoped_object_set_sha256(receipt["objects"], "research_candidate")
    path, index, receipt_binding, body = _write_index_and_receipt(
        tree["root"], receipt)
    audit_path, audit, evidence_path = _write_audit(
        tree["root"], receipt["receipt_set_sha256"])
    tree.update({
        "receipt": receipt,
        "index": index,
        "index_path": path,
        "receipt_binding": receipt_binding,
        "body": body,
        "reader": ExactReceiptReader(receipt_binding, body),
        "audit_path": audit_path,
        "audit": audit,
        "evidence_path": evidence_path,
    })


def _add_late_sealed_rfq_dependency(tree):
    """Add an RFQ object from a later receipt-day named by seal.raw_files."""
    receipt = copy.deepcopy(tree["receipt"])
    payload = b'{"rfq":true,"late_dependency":true}\n'
    logical = "raw/date=%s/rfq_00.ndjson.1" % LATE_RECEIPT_DATE
    late = _object(
        key="%s/%s" % (PREFIX, logical), logical=logical,
        payload=payload, kind="raw_rfq", family="raw_durability",
        candidate=False, channel="rfq", exposure="FORBIDDEN_RFQ_DEFAULT")
    late.update({
        "date": LATE_RECEIPT_DATE,
        "required": False,
        "seal_binding": receipt["seal"]["sha256"],
        "evidence_binding": "seal.raw_files",
    })
    receipt["objects"].append(late)
    _replace_byte_receipt(tree, receipt)
    return late


def _write_rfq_authority(tree):
    audit = copy.deepcopy(tree["audit"])
    audit.update({
        "rfq_tag_key": cet.RFQ_TAG_KEY,
        "rfq_tag_value": cet.RFQ_TAG_VALUE,
        "rfq_exact_version_scope_verified": True,
    })
    tree["audit_path"].write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n")
    tree["audit"] = audit
    rfq_objects = [obj for obj in tree["receipt"]["objects"]
                   if cet._is_rfq_object(obj)]
    rows = cet._sort_rfq_projection([
        cet._rfq_projection(obj) for obj in rfq_objects])
    evidence = {
        "schema_version": cet.RFQ_EVIDENCE_SCHEMA,
        "state": cet.RFQ_EVIDENCE_STATE,
        "date": DATE,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "byte_receipt_set_sha256":
            tree["receipt"]["receipt_set_sha256"],
        "seal_sha256": tree["receipt"]["seal"]["sha256"],
        "evidence_tier": "SEALED_CONFIRMATION",
        "integrity_state": "PASS",
        "quarantine_state": "CLEAR",
        "repair_branch_state": "CLOSED_NO_REPAIR",
        "rfq_exact_set_sha256": cr.canonical_sha256(rows),
        "objects": rows,
        "decided_at_utc": "2026-07-13T04:00:00Z",
        "decider": "fixture-rfq-decider",
        "source_evidence_sha256": "e" * 64,
    }
    path = tree["root"] / "rfq-eligibility-evidence.json"
    path.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    tree["rfq_evidence_path"] = path
    tree["rfq_evidence"] = evidence
    return path, evidence


def _run(tree, *, tagger=None, writer=None, output=None,
         include_sealed_rfq=False, rfq_evidence=None):
    tagger = tagger or FakeTagger()
    writer = writer or FakeReceiptWriter()
    with mock.patch.object(
            cet, "_mutation_code_commit", return_value="d" * 40):
        result = cet.run_eligibility_tagging(
            receipt_index=str(tree["index_path"]),
            single_writer_audit=str(tree["audit_path"]),
            policy_evidence=str(tree["evidence_path"]),
            reader=tree["reader"], tagger=tagger, receipt_writer=writer,
            output_root=str(output or (tree["root"] / "tagged")),
            expected_bucket=BUCKET, expected_prefix=PREFIX,
            verified_at="2026-07-13T04:10:00Z", tagger_commit="d" * 40,
            include_sealed_rfq=include_sealed_rfq,
            rfq_eligibility_evidence=(
                str(rfq_evidence) if rfq_evidence is not None else None))
    return result, tagger, writer


def test_all_tag_gets_finish_before_first_put(receipt_tree):
    tagger = FakeTagger(fail_get_at=2)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_GET_FAILED"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    assert len(tagger.gets) == 2
    assert tagger.puts == []
    assert writer.calls == []


def test_rfq_is_never_read_or_tagged_and_unknown_tags_are_preserved(
        receipt_tree):
    candidates = [obj for obj in receipt_tree["receipt"]["objects"]
                  if obj["research_candidate"]]
    tags = {
        (obj["bucket"], obj["key"], obj["VersionId"]): {"owner": "capture"}
        for obj in candidates
    }
    result, tagger, _writer = _run(
        receipt_tree, tagger=FakeTagger(tags))
    _path, _index, tagged = result
    assert all("/raw/" not in op[1] for op in tagger.gets)
    assert all("/raw/" not in op[1] for op in tagger.puts)
    data_puts = [op for op in tagger.puts if "/canonical-receipts/" not in op[1]]
    receipt_puts = [op for op in tagger.puts
                    if "/canonical-receipts/" in op[1]]
    assert all(op[3] == {"owner": "capture", "research-eligible": "true"}
               for op in data_puts)
    assert len(receipt_puts) == 1
    assert receipt_puts[0][3] == {"research-eligible": "true"}
    by_logical = {obj["logical_source_key"]: obj for obj in tagged["objects"]}
    assert by_logical["raw/date=%s/rfq_23.ndjson" % DATE][
        "research_eligible"] is False
    assert by_logical["raw/date=%s/rfq_23.ndjson" % DATE][
        "eligibility_tag_state"] == "SHADOW_NOT_TAGGED"


def test_candidate_rfq_aborts_before_any_tag_access(receipt_tree):
    receipt = copy.deepcopy(receipt_tree["receipt"])
    rfq = next(obj for obj in receipt["objects"]
               if obj["source_kind"] == "raw_rfq")
    rfq["research_candidate"] = True
    binding = dict(receipt["seal"])
    binding["families"] = receipt["families"]
    receipt["receipt_set_sha256"] = cr.receipt_set_sha256(
        DATE, binding, receipt["objects"])
    receipt["research_candidate_set_sha256"] = \
        cr.scoped_object_set_sha256(receipt["objects"], "research_candidate")
    path, index, binding_obj, body = _write_index_and_receipt(
        receipt_tree["root"], receipt)
    audit_path, _audit, evidence_path = _write_audit(
        receipt_tree["root"], receipt["receipt_set_sha256"])
    receipt_tree.update({
        "index_path": path, "index": index,
        "receipt_binding": binding_obj, "body": body,
        "reader": ExactReceiptReader(binding_obj, body),
        "audit_path": audit_path,
        "evidence_path": evidence_path,
    })
    tagger = FakeTagger()
    with pytest.raises(cr.ReceiptError, match="RFQ_TAGGING_FORBIDDEN"):
        _run(receipt_tree, tagger=tagger)
    assert tagger.gets == []
    assert tagger.puts == []


@pytest.mark.parametrize("include,evidence", [
    (True, None),
    (False, "unpaired-rfq-evidence.json"),
    (1, "rfq-evidence.json"),
])
def test_rfq_options_are_a_required_pair_before_any_remote_read(
        receipt_tree, include, evidence):
    tagger = FakeTagger()
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="RFQ_OPTION_PAIR_REQUIRED"):
        _run(
            receipt_tree, tagger=tagger, writer=writer,
            include_sealed_rfq=include, rfq_evidence=evidence)
    assert receipt_tree["reader"].ops == []
    assert tagger.identity_calls == 0
    assert tagger.gets == []
    assert tagger.puts == []
    assert writer.calls == []


def test_sealed_rfq_mode_tags_complete_exact_set_and_builds_fixed_binding(
        receipt_tree):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    all_targets = [obj for obj in receipt_tree["receipt"]["objects"]
                   if obj["research_candidate"] or cet._is_rfq_object(obj)]
    tags = {
        (obj["bucket"], obj["key"], obj["VersionId"]): {"owner": "capture"}
        for obj in all_targets
    }
    result, tagger, _writer = _run(
        receipt_tree, tagger=FakeTagger(tags),
        include_sealed_rfq=True, rfq_evidence=evidence_path)
    _path, index, tagged = result

    first_put = next(i for i, event in enumerate(tagger.events)
                     if event[0] == "put")
    assert first_put == len(all_targets)
    assert all(event[0] == "get" for event in tagger.events[:first_put])

    rfq_parent_ids = {
        (obj["bucket"], obj["key"], obj["VersionId"])
        for obj in receipt_tree["receipt"]["objects"]
        if cet._is_rfq_object(obj)
    }
    data_puts = [op for op in tagger.puts
                 if "/canonical-receipts/" not in op[1]]
    receipt_puts = [op for op in tagger.puts
                    if "/canonical-receipts/" in op[1]]
    assert len(data_puts) == len(all_targets)
    for bucket, key, version_id, desired in data_puts:
        if (bucket, key, version_id) in rfq_parent_ids:
            assert desired == {
                "owner": "capture",
                cet.TAG_KEY: cet.TAG_VALUE,
                cet.RFQ_TAG_KEY: cet.RFQ_TAG_VALUE,
            }
        else:
            assert desired == {
                "owner": "capture", cet.TAG_KEY: cet.TAG_VALUE}
    assert len(receipt_puts) == 1
    assert receipt_puts[0][3] == {cet.TAG_KEY: cet.TAG_VALUE}

    rfq_rows = [obj for obj in tagged["objects"]
                if cet._is_rfq_object(obj)]
    assert len(rfq_rows) == len(rfq_parent_ids) == 2
    assert all(obj["required"] is False for obj in rfq_rows)
    assert all(obj["research_candidate"] is True for obj in rfq_rows)
    assert all(obj["research_eligible"] is True for obj in rfq_rows)
    assert all(obj["eligibility_tag_state"] == cet.RFQ_DUAL_TAG_STATE
               for obj in rfq_rows)
    assert all(obj["exposure_policy"] == cet.RFQ_EXPOSURE_POLICY
               for obj in rfq_rows)
    bindings = [obj["evidence_binding"] for obj in rfq_rows]
    assert all(binding == bindings[0] for binding in bindings)
    assert set(bindings[0]) == cet.RFQ_BINDING_FIELDS
    assert bindings[0]["rfq_exact_set_sha256"] == \
        cet.rfq_exact_set_sha256(rfq_rows)
    assert bindings[0]["eligibility_evidence_sha256"] == \
        _sha(evidence_path.read_bytes())
    assert index["complete"] is True


def test_reference_publisher_accepts_real_rfq_tagger_output(receipt_tree):
    late = _add_late_sealed_rfq_dependency(receipt_tree)
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    result, _tagger, writer = _run(
        receipt_tree, include_sealed_rfq=True,
        rfq_evidence=evidence_path)
    index_path, index, tagged = result
    tagged_binding = index["receipt_object"]
    tagged_body = writer.objects[(BUCKET, tagged_binding["key"])]
    reader = MultiExactReceiptReader([
        (tagged_binding, tagged_body),
        (receipt_tree["receipt_binding"], receipt_tree["body"]),
        *[(obj, b"x" * obj["size"]) for obj in tagged["objects"]
          if obj["research_candidate"]],
    ])
    loaded, _source_binding = rr._load_authoritative_receipt(
        str(index_path), DATE, _sha(receipt_tree["seal_payload"]),
        len(receipt_tree["seal_payload"]), reader)
    rfq_rows = [obj for obj in loaded["objects"]
                if cet._is_rfq_object(obj)]
    assert len(rfq_rows) == 3
    assert any(obj["logical_source_key"] == late["logical_source_key"]
               and obj["date"] == LATE_RECEIPT_DATE for obj in rfq_rows)
    assert all(obj["eligibility_tag_state"] == cet.RFQ_DUAL_TAG_STATE
               for obj in rfq_rows)


def test_core_and_rfq_complete_preflight_finishes_before_any_write(
        receipt_tree):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    tagger = FakeTagger(fail_get_at=4)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_GET_FAILED"):
        _run(
            receipt_tree, tagger=tagger, writer=writer,
            include_sealed_rfq=True, rfq_evidence=evidence_path)
    assert len(tagger.gets) == 4
    assert tagger.puts == []
    assert writer.calls == []


@pytest.mark.parametrize("missing", [
    "rfq_tag_key", "rfq_tag_value", "rfq_exact_version_scope_verified",
])
def test_rfq_mode_requires_extended_single_writer_audit(
        receipt_tree, missing):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    audit = copy.deepcopy(receipt_tree["audit"])
    audit.pop(missing)
    receipt_tree["audit_path"].write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n")
    tagger = FakeTagger()
    with pytest.raises(cr.ReceiptError, match="SINGLE_WRITER_AUDIT_INVALID"):
        _run(
            receipt_tree, tagger=tagger, include_sealed_rfq=True,
            rfq_evidence=evidence_path)
    assert tagger.identity_calls == 0
    assert tagger.gets == []
    assert tagger.puts == []


@pytest.mark.parametrize("fault", [
    "schema", "state", "date", "bucket", "prefix", "parent", "seal",
    "tier", "integrity", "quarantine", "repair", "set_sha",
    "object_missing", "object_version", "object_extra_field", "root_extra",
    "future", "decider", "source_sha",
])
def test_rfq_evidence_tamper_fails_before_sts_or_tag_access(
        receipt_tree, fault):
    evidence_path, original = _write_rfq_authority(receipt_tree)
    evidence = copy.deepcopy(original)
    scalar_faults = {
        "schema": ("schema_version", "wrong-schema"),
        "state": ("state", "WRONG"),
        "date": ("date", "2026-07-11"),
        "bucket": ("bucket", "other-bucket"),
        "prefix": ("prefix", "other-prefix"),
        "parent": ("byte_receipt_set_sha256", "1" * 64),
        "seal": ("seal_sha256", "2" * 64),
        "tier": ("evidence_tier", "DEGRADED"),
        "integrity": ("integrity_state", "FAIL"),
        "quarantine": ("quarantine_state", "QUARANTINED"),
        "repair": ("repair_branch_state", "REPAIR_OPEN"),
        "set_sha": ("rfq_exact_set_sha256", "3" * 64),
        "future": ("decided_at_utc", "2026-07-13T04:15:01Z"),
        "decider": ("decider", ""),
        "source_sha": ("source_evidence_sha256", "not-a-sha"),
    }
    if fault in scalar_faults:
        field, value = scalar_faults[fault]
        evidence[field] = value
    elif fault == "object_missing":
        evidence["objects"].pop()
    elif fault == "object_version":
        evidence["objects"][0]["VersionId"] = "other-version"
    elif fault == "object_extra_field":
        evidence["objects"][0]["unexpected"] = True
    else:
        evidence["unexpected"] = True
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    tagger = FakeTagger()
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="RFQ_ELIGIBILITY"):
        _run(
            receipt_tree, tagger=tagger, writer=writer,
            include_sealed_rfq=True, rfq_evidence=evidence_path)
    assert tagger.identity_calls == 0
    assert tagger.gets == []
    assert tagger.puts == []
    assert writer.calls == []


@pytest.mark.parametrize("existing", [
    {cet.RFQ_TAG_KEY: "not-rfq"},
    {"tag-%02d" % index: "x" for index in range(9)},
])
def test_rfq_managed_tag_conflict_or_limit_fails_before_all_writes(
        receipt_tree, existing):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    rfq = next(obj for obj in receipt_tree["receipt"]["objects"]
               if cet._is_rfq_object(obj))
    tagger = FakeTagger({
        (rfq["bucket"], rfq["key"], rfq["VersionId"]): existing})
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError):
        _run(
            receipt_tree, tagger=tagger, writer=writer,
            include_sealed_rfq=True, rfq_evidence=evidence_path)
    assert tagger.puts == []
    assert writer.calls == []


def test_non_rfq_raw_candidate_remains_forbidden_in_rfq_mode(receipt_tree):
    receipt = copy.deepcopy(receipt_tree["receipt"])
    fact = next(obj for obj in receipt["objects"]
                if obj["source_kind"] == "facts")
    fact["source_kind"] = "raw_firehose"
    _replace_byte_receipt(receipt_tree, receipt)
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    tagger = FakeTagger()
    with pytest.raises(cr.ReceiptError, match="RAW_TAGGING_FORBIDDEN"):
        _run(
            receipt_tree, tagger=tagger, include_sealed_rfq=True,
            rfq_evidence=evidence_path)
    assert tagger.gets == []
    assert tagger.puts == []


def test_rfq_tag_readback_failure_never_publishes_receipt(receipt_tree):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    # Four-target preflight, four data PUTs, then GET #5 is first readback.
    tagger = FakeTagger(fail_get_at=5)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_GET_FAILED"):
        _run(
            receipt_tree, tagger=tagger, writer=writer,
            include_sealed_rfq=True, rfq_evidence=evidence_path)
    assert len(tagger.puts) == 4
    assert writer.calls == []


def test_rfq_mode_is_idempotent(receipt_tree):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    tagger = FakeTagger()
    writer = FakeReceiptWriter()
    first, tagger, writer = _run(
        receipt_tree, tagger=tagger, writer=writer,
        include_sealed_rfq=True, rfq_evidence=evidence_path)
    puts = len(tagger.puts)
    second, tagger, writer = _run(
        receipt_tree, tagger=tagger, writer=writer,
        include_sealed_rfq=True, rfq_evidence=evidence_path)
    assert len(tagger.puts) == puts == 5
    assert first[1]["receipt_set_sha256"] == second[1]["receipt_set_sha256"]
    assert len(writer.objects) == 1
    assert len(writer.calls) == 2


def test_rfq_evidence_is_bounded_and_rejects_symlink(
        receipt_tree, monkeypatch):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    link = receipt_tree["root"] / "rfq-evidence-link.json"
    os.symlink(evidence_path, link)
    for candidate in (link, evidence_path):
        if candidate == evidence_path:
            monkeypatch.setattr(
                cet, "MAX_RFQ_ELIGIBILITY_EVIDENCE_BYTES", 1)
        tagger = FakeTagger()
        with pytest.raises(cr.ReceiptError, match="RFQ_ELIGIBILITY"):
            _run(
                receipt_tree, tagger=tagger, include_sealed_rfq=True,
                rfq_evidence=candidate)
        assert tagger.gets == []
        assert tagger.puts == []


@pytest.mark.parametrize("existing", [
    {"research-eligible": "false"},
    {**{"tag-%02d" % index: "x" for index in range(10)}},
])
def test_tag_conflict_or_overflow_fails_before_writes(receipt_tree, existing):
    first = next(obj for obj in receipt_tree["receipt"]["objects"]
                 if obj["research_candidate"])
    tags = {(first["bucket"], first["key"], first["VersionId"]): existing}
    tagger = FakeTagger(tags)
    with pytest.raises(cr.ReceiptError):
        _run(receipt_tree, tagger=tagger)
    assert tagger.puts == []


def test_partial_tag_failure_never_publishes_tagged_receipt(receipt_tree):
    tagger = FakeTagger(fail_put_at=2)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_PUT_FAILED"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    assert len(tagger.puts) == 2
    assert writer.calls == []
    assert not (receipt_tree["root"] / "tagged").exists()


@pytest.mark.parametrize("failure", ["receipt_put", "receipt_readback"])
def test_receipt_tag_failure_leaves_pending_but_no_usable_index(
        receipt_tree, failure):
    # Two candidate data PUTs happen before the receipt object is published.
    # Publication itself re-reads both data tags, so receipt tag preflight is
    # GET #7, its PUT is #3, and its readback is GET #8.
    tagger = FakeTagger(
        fail_put_at=3 if failure == "receipt_put" else None,
        fail_get_at=8 if failure == "receipt_readback" else None)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError):
        _run(receipt_tree, tagger=tagger, writer=writer)
    tagged_root = receipt_tree["root"] / "tagged" / ("date=%s" % DATE)
    pending_paths = list(tagged_root.glob("TAGGED-PENDING-*.json"))
    assert len(pending_paths) == 1
    pending = json.loads(pending_paths[0].read_text())
    assert pending["schema_version"] == cet.PENDING_INDEX_SCHEMA
    assert pending["state"] == cet.PENDING_INDEX_STATE
    assert pending["complete"] is False
    assert pending["completed"] is False
    assert pending["receipt_object_eligibility_tag_state"] == \
        "PENDING_TAG_VERIFICATION"
    assert list(tagged_root.glob("TAGGED-DURABLE-*.json")) == []
    assert len(writer.objects) == 1
    stored = writer.objects[(BUCKET, pending["receipt_object"]["key"])]
    with pytest.raises(SystemExit, match="durable index"):
        rr._load_authoritative_receipt(
            str(pending_paths[0]), DATE, _sha(receipt_tree["seal_payload"]),
            len(receipt_tree["seal_payload"]),
            ExactReceiptReader(pending["receipt_object"], stored))


def test_aws_cli_tagger_always_supplies_exact_version(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        version = argv[argv.index("--version-id") + 1]
        if "get-object-tagging" in argv:
            body = {"VersionId": version, "TagSet": []}
        else:
            body = {"VersionId": version}
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(cet.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cet, "_mutation_code_commit", lambda: "d" * 40)
    tagger = cet.AwsCliExactVersionTagger("aws-fixture")
    assert tagger.get_tags(BUCKET, "ec2/warehouse/x", "v-exact") == {}
    tagger.put_tags(
        BUCKET, "ec2/warehouse/x", "v-exact",
        {"owner": "x", "research-eligible": "true"})
    assert len(calls) == 2
    assert all("--version-id" in call for call in calls)
    put = next(call for call in calls if "put-object-tagging" in call)
    assert put[put.index("--version-id") + 1] == "v-exact"
    tag_payload = json.loads(put[put.index("--tagging") + 1])
    assert tag_payload["TagSet"] == [
        {"Key": "owner", "Value": "x"},
        {"Key": "research-eligible", "Value": "true"},
    ]


def test_aws_cli_tagger_reads_ambient_sts_identity_with_short_timeout(
        monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({
                "Account": "123456789012",
                "Arn": CALLER_ARN,
                "UserId": "AROAFIXTURE:fixture-session",
            }), stderr="")

    monkeypatch.setattr(cet.subprocess, "run", fake_run)
    tagger = cet.AwsCliExactVersionTagger("aws-fixture")
    identity = tagger.get_caller_identity()
    assert identity["Arn"] == CALLER_ARN
    assert calls[0][0] == [
        "aws-fixture", "sts", "get-caller-identity", "--output", "json"]
    assert calls[0][1]["timeout"] == 60


@pytest.mark.parametrize("fault", [
    "index_version", "index_last_modified", "receipt_body", "audit_binding",
])
def test_index_receipt_and_audit_tamper_fail_closed(receipt_tree, fault):
    if fault.startswith("index_"):
        index = copy.deepcopy(receipt_tree["index"])
        field = "VersionId" if fault == "index_version" else \
            "last_modified_utc"
        index["receipt_object"][field] = (
            "tampered" if field == "VersionId" else "2026-07-13T04:01:00Z")
        receipt_tree["index_path"].write_text(
            json.dumps(index, sort_keys=True, indent=2) + "\n")
    elif fault == "receipt_body":
        receipt_tree["reader"].body = receipt_tree["body"] + b" "
    else:
        audit = copy.deepcopy(receipt_tree["audit"])
        audit["receipt_set_sha256"] = "f" * 64
        receipt_tree["audit_path"].write_text(
            json.dumps(audit, sort_keys=True, indent=2) + "\n")
    tagger = FakeTagger()
    writer = FakeReceiptWriter()
    with pytest.raises((cr.ReceiptError, RuntimeError)):
        _run(receipt_tree, tagger=tagger, writer=writer)
    assert tagger.puts == []
    assert writer.calls == []


def test_policy_evidence_bytes_must_match_audit(receipt_tree):
    receipt_tree["evidence_path"].write_bytes(
        receipt_tree["evidence_path"].read_bytes() + b"tampered\n")
    tagger = FakeTagger()
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="POLICY_EVIDENCE_MISMATCH"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    assert tagger.identity_calls == 0
    assert tagger.gets == []
    assert tagger.puts == []
    assert writer.calls == []


@pytest.mark.parametrize("audited_at", [
    "2026-07-12T04:09:59Z",
    "2026-07-13T04:15:01Z",
])
def test_stale_or_future_audit_fails_before_sts_and_tags(
        receipt_tree, audited_at):
    audit = copy.deepcopy(receipt_tree["audit"])
    audit["audited_at_utc"] = audited_at
    receipt_tree["audit_path"].write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n")
    tagger = FakeTagger()
    with pytest.raises(cr.ReceiptError, match="SINGLE_WRITER_AUDIT_STALE"):
        _run(receipt_tree, tagger=tagger)
    assert tagger.identity_calls == 0
    assert tagger.gets == []
    assert tagger.puts == []


def test_ambient_sts_caller_must_match_audited_role(receipt_tree):
    tagger = FakeTagger(identity={
        "Account": "123456789012",
        "Arn": ("arn:aws:sts::123456789012:assumed-role/"
                "some-other-role/fixture-session"),
        "UserId": "AROABAD:fixture-session",
    })
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="TAGGER_CALLER_MISMATCH"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    assert tagger.identity_calls == 1
    assert tagger.gets == []
    assert tagger.puts == []
    assert writer.calls == []


def test_iam_user_principal_is_supported_with_exact_sts_identity(receipt_tree):
    user_arn = "arn:aws:iam::123456789012:user/canonical-tagger"
    audit = copy.deepcopy(receipt_tree["audit"])
    audit["dedicated_tagger_principal"] = user_arn
    audit["dedicated_tagger_sts_caller_arn"] = user_arn
    receipt_tree["audit_path"].write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n")
    tagger = FakeTagger(identity={
        "Account": "123456789012",
        "Arn": user_arn,
        "UserId": "AIDAFIXTURE",
    })
    result, tagger, _writer = _run(receipt_tree, tagger=tagger)
    assert result[1]["complete"] is True
    assert tagger.identity_calls == 1


@pytest.mark.parametrize("source", ["index", "audit", "evidence"])
def test_local_authority_inputs_reject_final_component_symlinks(
        receipt_tree, source):
    original = {
        "index": receipt_tree["index_path"],
        "audit": receipt_tree["audit_path"],
        "evidence": receipt_tree["evidence_path"],
    }[source]
    link = receipt_tree["root"] / ("linked-%s.json" % source)
    os.symlink(original, link)
    if source == "index":
        with pytest.raises(cr.ReceiptError, match="DURABLE_INDEX_INVALID"):
            cet.load_durable_byte_receipt(
                str(link), receipt_tree["reader"],
                expected_bucket=BUCKET, expected_prefix=PREFIX)
        assert receipt_tree["reader"].ops == []
    else:
        audit_path = link if source == "audit" else receipt_tree["audit_path"]
        evidence_path = (link if source == "evidence" else
                         receipt_tree["evidence_path"])
        with pytest.raises(cr.ReceiptError):
            cet.load_single_writer_audit(
                str(audit_path), str(evidence_path), date=DATE,
                bucket=BUCKET,
                receipt_set_sha256=receipt_tree["receipt"][
                    "receipt_set_sha256"],
                now_utc="2026-07-13T04:10:00Z")


@pytest.mark.parametrize("source", ["index", "audit", "evidence"])
def test_local_authority_inputs_are_size_bounded(
        receipt_tree, source, monkeypatch):
    if source == "index":
        monkeypatch.setattr(cet, "MAX_DURABLE_INDEX_BYTES", 1)
        with pytest.raises(cr.ReceiptError, match="DURABLE_INDEX_INVALID"):
            cet.load_durable_byte_receipt(
                str(receipt_tree["index_path"]), receipt_tree["reader"],
                expected_bucket=BUCKET, expected_prefix=PREFIX)
        assert receipt_tree["reader"].ops == []
        return
    if source == "audit":
        monkeypatch.setattr(cet, "MAX_SINGLE_WRITER_AUDIT_BYTES", 1)
    else:
        monkeypatch.setattr(cet, "MAX_POLICY_EVIDENCE_BYTES", 1)
    with pytest.raises(cr.ReceiptError):
        cet.load_single_writer_audit(
            str(receipt_tree["audit_path"]),
            str(receipt_tree["evidence_path"]), date=DATE, bucket=BUCKET,
            receipt_set_sha256=receipt_tree["receipt"][
                "receipt_set_sha256"],
            now_utc="2026-07-13T04:10:00Z")


def test_publish_primitive_cannot_bypass_data_tag_reverification(
        receipt_tree):
    byte_index, byte_receipt, prefix = cet.load_durable_byte_receipt(
        str(receipt_tree["index_path"]), receipt_tree["reader"],
        expected_bucket=BUCKET, expected_prefix=PREFIX)
    _audit, audit_sha = cet.load_single_writer_audit(
        str(receipt_tree["audit_path"]),
        str(receipt_tree["evidence_path"]), date=DATE, bucket=BUCKET,
        receipt_set_sha256=byte_receipt["receipt_set_sha256"],
        now_utc="2026-07-13T04:10:00Z")
    payload = cet.build_tagged_receipt(
        byte_receipt, audit_sha256=audit_sha,
        verified_at="2026-07-13T04:10:00Z", tagger_commit="d" * 40)
    writer = FakeReceiptWriter()
    output = receipt_tree["root"] / "tagged-direct-bypass"
    with pytest.raises(cr.ReceiptError, match="DATA_TAG_REVERIFY_FAILED"):
        cet.publish_tagged_receipt(
            payload, byte_index=byte_index, byte_receipt=byte_receipt,
            audit_sha=audit_sha, writer=writer, tagger=FakeTagger(),
            output_root=str(output), bucket=BUCKET, prefix=prefix)
    assert writer.calls == []
    assert not output.exists()


def test_publish_rejects_mismatched_byte_parent_and_transition(receipt_tree):
    byte_index, byte_receipt, prefix = cet.load_durable_byte_receipt(
        str(receipt_tree["index_path"]), receipt_tree["reader"],
        expected_bucket=BUCKET, expected_prefix=PREFIX)
    _audit, audit_sha = cet.load_single_writer_audit(
        str(receipt_tree["audit_path"]),
        str(receipt_tree["evidence_path"]), date=DATE, bucket=BUCKET,
        receipt_set_sha256=byte_receipt["receipt_set_sha256"],
        now_utc="2026-07-13T04:10:00Z")
    payload = cet.build_tagged_receipt(
        byte_receipt, audit_sha256=audit_sha,
        verified_at="2026-07-13T04:10:00Z", tagger_commit="d" * 40)
    tags = {
        (obj["bucket"], obj["key"], obj["VersionId"]):
            {cet.TAG_KEY: cet.TAG_VALUE}
        for obj in byte_receipt["objects"] if obj["research_candidate"]
    }
    bad_index = copy.deepcopy(byte_index)
    bad_index["receipt_payload_sha256"] = "f" * 64
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="BYTE_PARENT_MISMATCH"):
        cet.publish_tagged_receipt(
            payload, byte_index=bad_index, byte_receipt=byte_receipt,
            audit_sha=audit_sha, writer=writer, tagger=FakeTagger(tags),
            output_root=str(receipt_tree["root"] / "bad-parent"),
            bucket=BUCKET, prefix=prefix)
    tampered = copy.deepcopy(payload)
    tampered["unexpected_parent_mutation"] = True
    with pytest.raises(cr.ReceiptError, match="TAGGED_PARENT_MISMATCH"):
        cet.publish_tagged_receipt(
            tampered, byte_index=byte_index, byte_receipt=byte_receipt,
            audit_sha=audit_sha, writer=writer, tagger=FakeTagger(tags),
            output_root=str(receipt_tree["root"] / "bad-transition"),
            bucket=BUCKET, prefix=prefix)
    assert writer.calls == []


def test_receipt_writer_failure_leaves_prepublication_intent(receipt_tree):
    writer = FailingReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_RECEIPT_PUT_FAILED"):
        _run(receipt_tree, writer=writer)
    tagged_root = receipt_tree["root"] / "tagged" / ("date=%s" % DATE)
    pending = json.loads(next(tagged_root.glob(
        "TAGGED-PENDING-*.json")).read_text())
    assert pending["complete"] is False
    assert pending["receipt_object_eligibility_tag_state"] == \
        "NOT_YET_PUBLISHED"
    assert "expected_receipt_object" in pending
    assert "receipt_object" not in pending
    assert list(tagged_root.glob("TAGGED-DURABLE-*.json")) == []


def test_post_receipt_data_reverify_failure_never_writes_final_index(
        receipt_tree):
    # GET #9 is the first full-data re-read after the receipt tag readback.
    tagger = FakeTagger(fail_get_at=9)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_GET_FAILED"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    tagged_root = receipt_tree["root"] / "tagged" / ("date=%s" % DATE)
    pending = json.loads(next(tagged_root.glob(
        "TAGGED-PENDING-*.json")).read_text())
    assert pending["receipt_object_eligibility_tag_state"] == \
        "PENDING_TAG_VERIFICATION"
    assert list(tagged_root.glob("TAGGED-DURABLE-*.json")) == []


def test_idempotent_rerun_reuses_tags_and_tagged_receipt(receipt_tree):
    tagger = FakeTagger()
    writer = FakeReceiptWriter()
    first, tagger, writer = _run(
        receipt_tree, tagger=tagger, writer=writer)
    first_put_count = len(tagger.puts)
    second, tagger, writer = _run(
        receipt_tree, tagger=tagger, writer=writer)
    assert len(tagger.puts) == first_put_count
    assert first[1]["receipt_set_sha256"] == second[1]["receipt_set_sha256"]
    assert len(writer.objects) == 1
    assert len(writer.calls) == 2
    assert first[1]["receipt_object_eligibility_tag_state"] == \
        "TAGGED_VERIFIED"
    assert first[1]["complete"] is True
    assert not list((receipt_tree["root"] / "tagged" /
                     ("date=%s" % DATE)).glob("TAGGED-PENDING-*.json"))


def test_crash_after_receipt_tag_before_final_index_is_rerunnable(
        receipt_tree, monkeypatch):
    tagger = FakeTagger()
    writer = FakeReceiptWriter()
    original_write = cr.write_atomic_json
    crashed = {"done": False}

    def crash_at_final(path, payload):
        if ("TAGGED-DURABLE-" in os.fspath(path)
                and not crashed["done"]):
            crashed["done"] = True
            raise OSError("simulated crash before final index replace")
        return original_write(path, payload)

    monkeypatch.setattr(cr, "write_atomic_json", crash_at_final)
    with pytest.raises(OSError, match="simulated crash"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    tagged_root = receipt_tree["root"] / "tagged" / ("date=%s" % DATE)
    assert len(list(tagged_root.glob("TAGGED-PENDING-*.json"))) == 1
    assert list(tagged_root.glob("TAGGED-DURABLE-*.json")) == []
    puts_after_crash = len(tagger.puts)

    monkeypatch.setattr(cr, "write_atomic_json", original_write)
    result, _tagger, _writer = _run(
        receipt_tree, tagger=tagger, writer=writer)
    assert result[0].name.startswith("TAGGED-DURABLE-")
    assert result[1]["complete"] is True
    assert result[1]["receipt_object_eligibility_tag_state"] == \
        "TAGGED_VERIFIED"
    assert len(tagger.puts) == puts_after_crash


def _make_precommit_proof(receipt_tree, result, tagger, writer, *,
                          include_sealed_rfq=False):
    index_path, index, tagged = result
    binding = index["receipt_object"]
    tagged_body = writer.objects[(BUCKET, binding["key"])]
    reader = MultiExactReceiptReader([
        (binding, tagged_body),
        (receipt_tree["receipt_binding"], receipt_tree["body"]),
    ])
    tagger.identity = {
        "Account": "123456789012",
        "Arn": USER_ARN,
        "UserId": "AIDAFIXTURETAGGER",
    }
    return cet.create_precommit_proof(
        tagged_index=str(index_path),
        byte_receipt_index=str(receipt_tree["index_path"]),
        reader=reader, tagger=tagger,
        output_root=str(receipt_tree["root"] / "tag-precommit"),
        expected_tagger_principal=USER_ARN,
        expected_bucket=BUCKET, expected_prefix=PREFIX,
        generated_at="2026-07-13T04:11:00Z",
        include_sealed_rfq=include_sealed_rfq)


def test_verify_only_proves_receipt_and_complete_non_rfq_set_without_puts(
        receipt_tree):
    result, tagger, writer = _run(receipt_tree)
    puts_before = len(tagger.puts)
    path, proof_sha, proof = _make_precommit_proof(
        receipt_tree, result, tagger, writer)
    assert path.name == "PRECOMMIT-%s.json" % proof_sha
    assert _sha(path.read_bytes()) == proof_sha
    assert proof["state"] == cet.PRECOMMIT_PROOF_STATE
    assert proof["rfq"] == "OFF"
    assert proof["tag_puts"] == 0
    assert proof["target_count"] == 1 + sum(
        obj["research_candidate"] for obj in result[2]["objects"])
    assert proof["research_candidate_count"] == proof["target_count"] - 1
    assert proof["target_set_sha256"] == cr.canonical_sha256(
        proof["targets"])
    assert proof["tagger_sts_caller_arn"] == USER_ARN
    assert len(tagger.puts) == puts_before
    assert all(row["role"] != "RESEARCH_CANDIDATE"
               or "/raw/" not in ("/" + row["source_key"])
               for row in proof["targets"])


def test_verify_only_proves_complete_dual_tagged_fresh_rfq_set(receipt_tree):
    evidence_path, _evidence = _write_rfq_authority(receipt_tree)
    result, tagger, writer = _run(
        receipt_tree, include_sealed_rfq=True,
        rfq_evidence=evidence_path)
    puts_before = len(tagger.puts)
    _path, _proof_sha, proof = _make_precommit_proof(
        receipt_tree, result, tagger, writer, include_sealed_rfq=True)

    assert proof["rfq"] == cet.RFQ_RESEARCH_MODE
    rfq_rows = [row for row in proof["targets"]
                if row["required_tags"].get(cet.RFQ_TAG_KEY) ==
                cet.RFQ_TAG_VALUE]
    assert len(rfq_rows) == 2
    assert all(row["required_tags"] == {
        cet.TAG_KEY: cet.TAG_VALUE,
        cet.RFQ_TAG_KEY: cet.RFQ_TAG_VALUE,
    } for row in rfq_rows)
    assert proof["research_candidate_count"] == \
        sum(obj["research_candidate"] for obj in result[2]["objects"])
    assert len(tagger.puts) == puts_before

    with pytest.raises(cr.ReceiptError, match="RFQ_PRECOMMIT_MODE_MISMATCH"):
        _make_precommit_proof(receipt_tree, result, tagger, writer)


def test_tag_removed_before_verify_only_fails_and_emits_no_proof(receipt_tree):
    result, tagger, writer = _run(receipt_tree)
    candidate = next(obj for obj in result[2]["objects"]
                     if obj["research_candidate"])
    tagger.tags[(candidate["bucket"], candidate["key"],
                 candidate["VersionId"])].pop(cet.TAG_KEY)
    puts_before = len(tagger.puts)
    with pytest.raises(cr.ReceiptError, match="DATA_TAG_REVERIFY_FAILED"):
        _make_precommit_proof(receipt_tree, result, tagger, writer)
    assert len(tagger.puts) == puts_before
    assert not (receipt_tree["root"] / "tag-precommit").exists()


def test_historical_recovery_requires_existing_exact_remote_receipt(
        receipt_tree):
    tagger = FakeTagger(fail_put_at=3)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_PUT_FAILED"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    pending = next((receipt_tree["root"] / "tagged" /
                    ("date=%s" % DATE)).glob("TAGGED-PENDING-*.json"))
    intent = json.loads(pending.read_text())
    binding = intent["receipt_object"]
    stored = writer.objects[(BUCKET, binding["key"])]
    reader = MultiExactReceiptReader([
        (binding, stored),
        (receipt_tree["receipt_binding"], receipt_tree["body"]),
    ])
    tagger.fail_put_at = None
    tagger.fail_get_at = None
    result = cet.recover_existing_tagged_receipt(
        pending_index=str(pending),
        receipt_index=str(receipt_tree["index_path"]),
        single_writer_audit=str(receipt_tree["audit_path"]),
        policy_evidence=str(receipt_tree["evidence_path"]),
        reader=reader, tagger=tagger,
        output_root=str(receipt_tree["root"] / "tagged"),
        expected_bucket=BUCKET, expected_prefix=PREFIX,
        now_utc="2026-07-14T04:10:01Z")
    assert result[1]["complete"] is True
    assert result[1]["receipt_object"] == binding
    assert not pending.exists()
    assert len(writer.calls) == 1


def test_historical_precreate_crash_is_abandoned_without_aws_writes(
        receipt_tree):
    tagger = FakeTagger()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_RECEIPT_PUT_FAILED"):
        _run(receipt_tree, tagger=tagger, writer=FailingReceiptWriter())
    pending = next((receipt_tree["root"] / "tagged" /
                    ("date=%s" % DATE)).glob("TAGGED-PENDING-*.json"))
    gets_before, puts_before = len(tagger.gets), len(tagger.puts)
    with pytest.raises(cr.ReceiptError, match="ABANDON_REQUIRES_FRESH_AUDIT"):
        cet.recover_existing_tagged_receipt(
            pending_index=str(pending),
            receipt_index=str(receipt_tree["index_path"]),
            single_writer_audit=str(receipt_tree["audit_path"]),
            policy_evidence=str(receipt_tree["evidence_path"]),
            reader=receipt_tree["reader"], tagger=tagger,
            output_root=str(receipt_tree["root"] / "tagged"),
            expected_bucket=BUCKET, expected_prefix=PREFIX,
            now_utc="2026-07-14T04:10:01Z")
    assert len(tagger.gets) == gets_before
    assert len(tagger.puts) == puts_before


def test_historical_missing_bound_remote_receipt_is_abandoned_without_writes(
        receipt_tree):
    tagger = FakeTagger(fail_put_at=3)
    writer = FakeReceiptWriter()
    with pytest.raises(cr.ReceiptError, match="FIXTURE_PUT_FAILED"):
        _run(receipt_tree, tagger=tagger, writer=writer)
    pending = next((receipt_tree["root"] / "tagged" /
                    ("date=%s" % DATE)).glob("TAGGED-PENDING-*.json"))
    assert json.loads(pending.read_text())["receipt_object"]["VersionId"]
    tagger.fail_put_at = None
    gets_before, puts_before = len(tagger.gets), len(tagger.puts)
    # The fixture reader knows the byte parent but not the newly created tagged
    # receipt VersionId, exactly modelling a missing/unreadable remote object.
    with pytest.raises(cr.ReceiptError, match="ABANDON_REQUIRES_FRESH_AUDIT"):
        cet.recover_existing_tagged_receipt(
            pending_index=str(pending),
            receipt_index=str(receipt_tree["index_path"]),
            single_writer_audit=str(receipt_tree["audit_path"]),
            policy_evidence=str(receipt_tree["evidence_path"]),
            reader=receipt_tree["reader"], tagger=tagger,
            output_root=str(receipt_tree["root"] / "tagged"),
            expected_bucket=BUCKET, expected_prefix=PREFIX,
            now_utc="2026-07-14T04:10:01Z")
    assert len(tagger.gets) == gets_before
    assert len(tagger.puts) == puts_before
    assert pending.exists()


def test_existing_only_recovery_rejects_under_24h_pending(receipt_tree):
    with pytest.raises(cr.ReceiptError, match="FIXTURE_RECEIPT_PUT_FAILED"):
        _run(receipt_tree, writer=FailingReceiptWriter())
    pending = next((receipt_tree["root"] / "tagged" /
                    ("date=%s" % DATE)).glob("TAGGED-PENDING-*.json"))
    with pytest.raises(cr.ReceiptError, match="TAGGER_RECOVERY_NOT_HISTORICAL"):
        cet.recover_existing_tagged_receipt(
            pending_index=str(pending),
            receipt_index=str(receipt_tree["index_path"]),
            single_writer_audit=str(receipt_tree["audit_path"]),
            policy_evidence=str(receipt_tree["evidence_path"]),
            reader=receipt_tree["reader"], tagger=FakeTagger(),
            output_root=str(receipt_tree["root"] / "tagged"),
            expected_bucket=BUCKET, expected_prefix=PREFIX,
            now_utc="2026-07-14T04:09:59Z")


def test_byte_receipt_exact_get_has_16_mib_limit(receipt_tree):
    index = copy.deepcopy(receipt_tree["index"])
    oversized = crc.MAX_DURABLE_RECEIPT_BYTES + 1
    index["receipt_object"]["size"] = oversized
    index["receipt_payload_size"] = oversized
    receipt_tree["index_path"].write_text(
        json.dumps(index, sort_keys=True, indent=2) + "\n")
    with pytest.raises(cr.ReceiptError, match="DURABLE_RECEIPT_TOO_LARGE"):
        cet.load_durable_byte_receipt(
            str(receipt_tree["index_path"]), receipt_tree["reader"],
            expected_bucket=BUCKET, expected_prefix=PREFIX)
    assert receipt_tree["reader"].ops == []


def test_tagged_receipt_is_bounded_before_conditional_put(
        receipt_tree, monkeypatch):
    _index, byte_receipt, prefix = cet.load_durable_byte_receipt(
        str(receipt_tree["index_path"]), receipt_tree["reader"],
        expected_bucket=BUCKET, expected_prefix=PREFIX)
    _audit, audit_sha = cet.load_single_writer_audit(
        str(receipt_tree["audit_path"]),
        str(receipt_tree["evidence_path"]), date=DATE, bucket=BUCKET,
        receipt_set_sha256=byte_receipt["receipt_set_sha256"],
        now_utc="2026-07-13T04:10:00Z")
    payload = cet.build_tagged_receipt(
        byte_receipt, audit_sha256=audit_sha,
        verified_at="2026-07-13T04:10:00Z", tagger_commit="d" * 40)
    writer = FakeReceiptWriter()
    monkeypatch.setattr(crc, "MAX_DURABLE_RECEIPT_BYTES", 1)
    with pytest.raises(cr.ReceiptError, match="TAGGED_RECEIPT_TOO_LARGE"):
        cet.publish_tagged_receipt(
            payload, byte_index=receipt_tree["index"],
            byte_receipt=byte_receipt, audit_sha=audit_sha,
            writer=writer, tagger=FakeTagger(),
            output_root=str(receipt_tree["root"] / "tagged-limit"),
            bucket=BUCKET, prefix=prefix)
    assert writer.calls == []


def test_tagged_receipt_has_new_digest_and_reference_publisher_accepts_it(
        receipt_tree):
    result, _tagger, writer = _run(receipt_tree)
    index_path, index, tagged = result
    assert index["receipt_set_sha256"] != \
        receipt_tree["receipt"]["receipt_set_sha256"]
    assert tagged["byte_attestation_receipt_set_sha256"] == \
        receipt_tree["receipt"]["receipt_set_sha256"]
    for before, after in zip(receipt_tree["receipt"]["objects"],
                             tagged["objects"]):
        for field in ("bucket", "key", "VersionId", "size", "sha256",
                      "last_modified_utc", "verification_state"):
            assert after[field] == before[field]
    tagged_binding = index["receipt_object"]
    tagged_body = writer.objects[(BUCKET, tagged_binding["key"])]
    reader = MultiExactReceiptReader([
        (tagged_binding, tagged_body),
        (receipt_tree["receipt_binding"], receipt_tree["body"]),
        *[(obj, b"x" * obj["size"]) for obj in tagged["objects"]
          if obj["research_candidate"]],
    ])
    loaded, source_binding = rr._load_authoritative_receipt(
        str(index_path), DATE, _sha(receipt_tree["seal_payload"]),
        len(receipt_tree["seal_payload"]), reader)
    assert loaded["receipt_set_sha256"] == index["receipt_set_sha256"]
    assert source_binding["version_id"] == tagged_binding["VersionId"]
    candidates = [obj for obj in loaded["objects"]
                  if obj["research_candidate"]]
    assert candidates
    assert all(obj["research_eligible"] is True for obj in candidates)
    assert all(obj["eligibility_tag_state"] == "TAGGED_VERIFIED"
               for obj in candidates)


def test_cli_refuses_without_explicit_operator_approval(receipt_tree):
    assert cet.main([
        "--receipt-index", str(receipt_tree["index_path"]),
        "--single-writer-audit", str(receipt_tree["audit_path"]),
        "--policy-evidence", str(receipt_tree["evidence_path"]),
    ]) == 2


@pytest.mark.parametrize("rfq_args", [
    ["--include-sealed-rfq"],
    ["--rfq-eligibility-evidence", "rfq-evidence.json"],
])
def test_cli_requires_paired_rfq_options_before_aws_setup(
        receipt_tree, rfq_args, capsys):
    assert cet.main([
        "--receipt-index", str(receipt_tree["index_path"]),
        "--single-writer-audit", str(receipt_tree["audit_path"]),
        "--policy-evidence", str(receipt_tree["evidence_path"]),
        "--operator-approved",
        *rfq_args,
    ]) == 2
    assert "required pair" in capsys.readouterr().err
