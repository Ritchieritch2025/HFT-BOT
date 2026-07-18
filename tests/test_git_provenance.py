#!/usr/bin/env python3
"""Offline fail-closed tests for mutation-time git provenance."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_eligibility_tagger as cet  # noqa: E402
import canonical_receipt_control as crc  # noqa: E402
import canonical_receipts as cr  # noqa: E402
import git_provenance as gp  # noqa: E402
import research_release as rr  # noqa: E402


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True).stdout.strip()


@pytest.fixture
def clean_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "critical.py").write_text("VALUE = 1\n")
    (root / "README.md").write_text("fixture\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "add", "--", "tools/critical.py", "README.md")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def test_clean_exact_head_passes_and_unrelated_dirty_is_ignored(clean_repo):
    expected = _git(clean_repo, "rev-parse", "HEAD")
    assert len(expected) == 40
    (clean_repo / "README.md").write_text("unrelated user change\n")
    assert gp.require_clean_head(
        clean_repo, ("tools/critical.py",)) == expected


def test_probe_uses_exact_safe_directory_and_sanitized_read_only_git_env(
        clean_repo, monkeypatch):
    expected = "c" * 40
    responses = iter([
        subprocess.CompletedProcess([], 0,
                                    stdout=(str(clean_repo) + "\n").encode(),
                                    stderr=b""),
        subprocess.CompletedProcess([], 0,
                                    stdout=(expected + "\n").encode(),
                                    stderr=b""),
        subprocess.CompletedProcess([], 0,
                                    stdout=b"tools/critical.py\x00", stderr=b""),
        subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
    ])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return next(responses)

    monkeypatch.setattr(gp.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_DIR", "/tmp/attacker.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/attacker-tree")
    assert gp.require_clean_head(
        clean_repo, ("tools/critical.py",)) == expected
    assert len(calls) == 4
    for argv, kwargs in calls:
        assert argv[0] == "/usr/bin/git"
        assert "safe.directory=%s" % clean_repo.resolve() in argv
        assert "core.fsmonitor=false" in argv
        assert "core.hooksPath=/dev/null" in argv
        assert kwargs["shell"] is False
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "GIT_DIR" not in kwargs["env"]
        assert "GIT_WORK_TREE" not in kwargs["env"]


def test_read_only_git_metadata_allows_clean_probe_but_still_detects_dirty(
        clean_repo):
    git_dir = clean_repo / ".git"
    index = git_dir / "index"
    old_dir_mode = git_dir.stat().st_mode & 0o777
    old_index_mode = index.stat().st_mode & 0o777
    try:
        index.chmod(0o444)
        git_dir.chmod(0o555)
        expected = _git(clean_repo, "rev-parse", "HEAD")
        assert gp.require_clean_head(
            clean_repo, ("tools/critical.py",)) == expected
        (clean_repo / "tools" / "critical.py").write_text("VALUE = 2\n")
        with pytest.raises(
                gp.GitProvenanceError, match="GIT_PROVENANCE_DIRTY"):
            gp.require_clean_head(clean_repo, ("tools/critical.py",))
    finally:
        git_dir.chmod(old_dir_mode)
        index.chmod(old_index_mode)


@pytest.mark.parametrize("state", ["unstaged", "staged", "deleted"])
def test_tracked_relevant_dirty_fails_closed(clean_repo, state):
    path = clean_repo / "tools" / "critical.py"
    if state == "deleted":
        path.unlink()
    else:
        path.write_text("VALUE = 2\n")
        if state == "staged":
            _git(clean_repo, "add", "--", "tools/critical.py")
    with pytest.raises(
            gp.GitProvenanceError, match="GIT_PROVENANCE_DIRTY"):
        gp.require_clean_head(clean_repo, ("tools/critical.py",))


def test_untracked_relevant_file_fails_closed(clean_repo):
    (clean_repo / "tools" / "new_mutator.py").write_text("VALUE = 1\n")
    with pytest.raises(
            gp.GitProvenanceError, match="GIT_PROVENANCE_DIRTY"):
        gp.require_clean_head(
            clean_repo, ("tools/critical.py", "tools/new_mutator.py"))


def test_unknown_head_fails_closed(tmp_path):
    root = tmp_path / "empty-repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "critical.py").write_text("VALUE = 1\n")
    with pytest.raises(
            gp.GitProvenanceError, match="GIT_PROVENANCE_UNKNOWN"):
        gp.require_clean_head(root, ("critical.py",))


def test_canonical_control_put_checks_before_subprocess(monkeypatch, tmp_path):
    calls = []

    def blocked():
        raise cr.ReceiptError("GIT_PROVENANCE_DIRTY", "fixture dirty")

    monkeypatch.setattr(cr, "_code_commit", blocked)
    monkeypatch.setattr(
        crc.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(cr.ReceiptError, match="GIT_PROVENANCE_DIRTY"):
        crc.AwsCliConditionalWriter("aws-fixture")._put(
            "bucket", "key", os.fspath(tmp_path / "body"))
    assert calls == []


def test_exact_tag_put_checks_before_subprocess(monkeypatch):
    calls = []

    def blocked():
        raise cr.ReceiptError("GIT_PROVENANCE_UNKNOWN", "fixture unknown")

    monkeypatch.setattr(cet, "_mutation_code_commit", blocked)
    monkeypatch.setattr(
        cet.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(cr.ReceiptError, match="GIT_PROVENANCE_UNKNOWN"):
        cet.AwsCliExactVersionTagger("aws-fixture").put_tags(
            "bucket", "key", "version-1", {"research-eligible": "true"})
    assert calls == []


@pytest.mark.parametrize("operation", ["tree", "manifest"])
def test_research_s3_mutations_check_before_subprocess(
        monkeypatch, tmp_path, operation):
    calls = []

    def blocked():
        raise SystemExit("ABORT GIT_PROVENANCE_DIRTY")

    monkeypatch.setattr(rr, "require_clean_mutation_provenance", blocked)
    monkeypatch.setattr(
        rr.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    dest = rr.S3Dest("s3://fixture/research")
    with pytest.raises(SystemExit, match="GIT_PROVENANCE_DIRTY"):
        if operation == "tree":
            dest.upload_tree(os.fspath(tmp_path), "releases/id")
        else:
            dest.upload_manifest(
                os.fspath(tmp_path / "MANIFEST.json"),
                "releases/id/MANIFEST.json")
    assert calls == []


def test_research_provenance_covers_dynamic_receipt_validator():
    assert "tools/canonical_receipts.py" in \
        rr.RESEARCH_RELEASE_PROVENANCE_PATHS
