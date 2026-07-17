import json
import os
import sys

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import render_research_iam_bundle as renderer  # noqa: E402


ROLE = "arn:aws:iam::321572485933:role/canonical-eligibility-tagger"
POLICY_ROOT = os.path.join(ROOT, "docs", "plan_releases", "pipeline")


def _source(path, principal="TAGGER_ARN"):
    path.write_text(json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:PutObjectVersionTagging",
            "Resource": "arn:aws:s3:::bucket/key",
            "Condition": {"ArnNotEquals": {
                "aws:PrincipalArn": principal}},
        }],
    }))


def test_render_replaces_exactly_one_placeholder_without_aws(tmp_path):
    source = tmp_path / "source.json"
    output = tmp_path / "rendered.json"
    _source(source)
    result = renderer.render(str(source), str(output), ROLE)
    value = json.loads(output.read_text())
    assert value["Statement"][0]["Condition"]["ArnNotEquals"][
        "aws:PrincipalArn"] == ROLE
    assert "TAGGER_ARN" not in output.read_text()
    assert result["aws_writes"] == 0
    assert result["state"] == "IAM_BUCKET_FRAGMENT_RENDERED_NOT_APPLIED"


@pytest.mark.parametrize("role", [
    "TAGGER_ARN",
    "arn:aws:sts::321572485933:assumed-role/tagger/session",
    "arn:aws:iam::321572485933:role/service-role/tagger",
    "arn:aws:iam::123:role/tagger",
])
def test_render_rejects_placeholder_sts_path_or_bad_account(tmp_path, role):
    source = tmp_path / "source.json"
    _source(source)
    with pytest.raises(renderer.RenderError, match="pathless IAM role ARN"):
        renderer.render(str(source), str(tmp_path / "out.json"), role)


def test_render_refuses_missing_or_duplicate_placeholder(tmp_path):
    source = tmp_path / "source.json"
    _source(source, principal="arn:aws:iam::321572485933:role/already-set")
    with pytest.raises(renderer.RenderError, match="exactly once"):
        renderer.render(str(source), str(tmp_path / "out.json"), ROLE)
    _source(source, principal=["TAGGER_ARN", "TAGGER_ARN"])
    with pytest.raises(renderer.RenderError, match="exactly once"):
        renderer.render(str(source), str(tmp_path / "out.json"), ROLE)


def test_render_never_overwrites_existing_output(tmp_path):
    source = tmp_path / "source.json"
    output = tmp_path / "rendered.json"
    _source(source)
    output.write_text("operator-owned\n")
    with pytest.raises(renderer.RenderError, match="existing output"):
        renderer.render(str(source), str(output), ROLE)
    assert output.read_text() == "operator-owned\n"


def _policy(name):
    with open(os.path.join(POLICY_ROOT, name), encoding="utf-8") as handle:
        return json.load(handle)


def _resources(statement):
    value = statement.get("Resource", [])
    return [value] if isinstance(value, str) else value


def test_base_iam_enforces_rfq_aws_hard_off():
    names = {
        "w09": "W-PUB-REF-01C_W09_IDENTITY_POLICY.json",
        "publisher": "W-PUB-REF-01C_PUBLISHER_TAG_INSPECTION_DELTA.json",
        "tagger": "W-PUB-REF-01C_TAGGER_IDENTITY_POLICY.json",
    }
    policies = {name: _policy(path) for name, path in names.items()}
    for policy in policies.values():
        for statement in policy["Statement"]:
            if statement["Effect"] == "Allow":
                assert not any("/ec2/raw/" in resource
                               for resource in _resources(statement))

    w09_raw_denies = [statement for statement in policies["w09"]["Statement"]
                      if statement["Effect"] == "Deny"
                      and any("/ec2/raw/" in resource
                              for resource in _resources(statement))]
    assert w09_raw_denies
    assert {"s3:GetObject", "s3:GetObjectVersion"}.issubset(
        set(w09_raw_denies[0]["Action"]))

    tagger_raw_denies = [
        statement for statement in policies["tagger"]["Statement"]
        if statement["Effect"] == "Deny"
        and any("/ec2/raw/" in resource
                for resource in _resources(statement))]
    assert tagger_raw_denies
    assert "s3:PutObjectVersionTagging" in tagger_raw_denies[0]["Action"]

    bucket = _policy("W-PUB-REF-01C_BUCKET_POLICY_MERGE_FRAGMENT.json")
    rfq_deny = next(statement for statement in bucket["Statement"]
                    if statement["Sid"] ==
                    "DenyAllRfqVersionTagMutationWhileCapabilityOff")
    assert rfq_deny["Principal"] == "*"
    assert "Condition" not in rfq_deny
    assert set(rfq_deny["Action"]) == {
        "s3:PutObjectVersionTagging", "s3:DeleteObjectVersionTagging"}


def test_large_corrections_use_direct_canonical_exact_version_scope():
    expected = (
        "arn:aws:s3:::kalshi-vault-ritcardo/ec2/warehouse/corrections/"
        "date=????-??-??/late_rows.ndjson"
    )
    obsolete = "/publication-snapshots/v1/date=*/corrections/late_rows/"
    names = [
        "W-PUB-REF-01C_W09_IDENTITY_POLICY.json",
        "W-PUB-REF-01C_PUBLISHER_TAG_INSPECTION_DELTA.json",
        "W-PUB-REF-01C_TAGGER_IDENTITY_POLICY.json",
        "W-PUB-REF-01C_BUCKET_POLICY_MERGE_FRAGMENT.json",
    ]
    for name in names:
        policy = _policy(name)
        resources = [
            resource
            for statement in policy["Statement"]
            for resource in _resources(statement)
        ]
        assert expected in resources
        assert not any(obsolete in resource for resource in resources)
