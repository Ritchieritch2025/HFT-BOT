import copy
import json
import os
import sys

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import merge_research_bucket_policy as merger  # noqa: E402
import render_research_iam_bundle as renderer  # noqa: E402


FRAGMENT_SOURCE = os.path.join(
    ROOT, "docs", "plan_releases", "pipeline",
    "W-PUB-REF-01C_BUCKET_POLICY_MERGE_FRAGMENT.json")


def _current(path):
    value = {
        "Version": "2012-10-17",
        "Id": "operator-owned-policy",
        "Statement": [{
            "Sid": "ExistingNoDeletion",
            "Effect": "Deny",
            "Principal": "*",
            "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
            "Resource": "arn:aws:s3:::kalshi-vault-ritcardo/ec2/*",
        }],
    }
    path.write_text(json.dumps(value, sort_keys=False) + "\n")
    return value


def _fragment(tmp_path):
    path = tmp_path / "fragment.json"
    renderer.render(
        FRAGMENT_SOURCE, str(path), renderer.APPROVED_TAGGER_ARN)
    return path


def test_merge_appends_and_preserves_existing_policy(tmp_path):
    current_path = tmp_path / "current.json"
    current = _current(current_path)
    fragment_path = _fragment(tmp_path)
    output = tmp_path / "target.json"
    result = merger.merge(str(current_path), str(fragment_path), str(output))
    target = json.loads(output.read_text())
    fragment = json.loads(fragment_path.read_text())

    assert target["Id"] == current["Id"]
    assert target["Statement"][:1] == current["Statement"]
    assert target["Statement"][1:] == fragment["Statement"]
    assert result["preserved_current_statements"] is True
    assert result["current_statement_count"] == 1
    assert result["appended_statement_count"] == len(fragment["Statement"])
    assert result["aws_writes"] == 0
    assert os.path.getsize(output) <= merger.MAX_POLICY_BYTES


def test_merge_rejects_sid_collision(tmp_path):
    current_path = tmp_path / "current.json"
    current = _current(current_path)
    current["Statement"][0]["Sid"] = merger.MANIFEST_PUT_SID
    current_path.write_text(json.dumps(current))
    with pytest.raises(merger.MergeError, match="Sid collision"):
        merger.merge(
            str(current_path), str(_fragment(tmp_path)),
            str(tmp_path / "target.json"))


@pytest.mark.parametrize("mutation", ["allow", "manifest", "principal"])
def test_merge_rejects_unsafe_fragment(tmp_path, mutation):
    current_path = tmp_path / "current.json"
    _current(current_path)
    rendered = _fragment(tmp_path)
    fragment = json.loads(rendered.read_text())
    if mutation == "allow":
        fragment["Statement"][0]["Effect"] = "Allow"
    elif mutation == "manifest":
        statement = next(
            row for row in fragment["Statement"]
            if row["Sid"] == merger.MANIFEST_PUT_SID)
        statement.pop("Effect")
    else:
        statement = next(
            row for row in fragment["Statement"]
            if row["Sid"] == merger.TAGGER_DENY_SID)
        statement["Condition"]["ArnNotEquals"]["aws:PrincipalArn"] = (
            "arn:aws:iam::321572485933:user/wrong")
    unsafe = tmp_path / ("unsafe-%s.json" % mutation)
    unsafe.write_text(json.dumps(fragment))
    with pytest.raises(merger.MergeError):
        merger.merge(
            str(current_path), str(unsafe), str(tmp_path / "target.json"))


def test_merge_refuses_existing_output(tmp_path):
    current_path = tmp_path / "current.json"
    _current(current_path)
    output = tmp_path / "target.json"
    output.write_text("operator-owned\n")
    with pytest.raises(merger.MergeError, match="existing output"):
        merger.merge(
            str(current_path), str(_fragment(tmp_path)), str(output))
    assert output.read_text() == "operator-owned\n"


def test_validate_fragment_rejects_placeholder(tmp_path):
    current_path = tmp_path / "current.json"
    _current(current_path)
    fragment = json.loads(open(FRAGMENT_SOURCE, encoding="utf-8").read())
    placeholder = tmp_path / "placeholder.json"
    placeholder.write_text(json.dumps(copy.deepcopy(fragment)))
    with pytest.raises(merger.MergeError, match="TAGGER_ARN"):
        merger.merge(
            str(current_path), str(placeholder),
            str(tmp_path / "target.json"))
