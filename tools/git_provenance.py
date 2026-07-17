#!/usr/bin/env python3
"""Fail-closed git provenance checks for irreversible remote mutations.

The check is deliberately scoped to an explicit list of mutation-critical
files.  Unrelated working-tree changes do not stop an operation, but every
listed file must be tracked and clean in both the index and working tree.
"""
import os
import re
import subprocess


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class GitProvenanceError(RuntimeError):
    """A stable, fail-closed provenance failure."""

    def __init__(self, code, detail):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def _run_git(repo_root, args):
    try:
        result = subprocess.run(
            ["git"] + list(args), cwd=repo_root, capture_output=True,
            timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitProvenanceError("GIT_PROVENANCE_UNKNOWN", str(exc))
    if result.returncode:
        detail = (result.stderr or result.stdout or b"").decode(
            "utf-8", "replace").strip()[-1000:]
        raise GitProvenanceError(
            "GIT_PROVENANCE_UNKNOWN",
            detail or "git %s exited %d" % (args[0], result.returncode))
    return result.stdout


def _safe_relevant_paths(paths):
    if not isinstance(paths, (tuple, list)) or not paths:
        raise GitProvenanceError(
            "GIT_PROVENANCE_UNKNOWN", "relevant path set is empty")
    clean = []
    seen = set()
    for value in paths:
        if (not isinstance(value, str) or not value
                or any(ord(char) < 32 for char in value)
                or "\\" in value or value.startswith("/")):
            raise GitProvenanceError(
                "GIT_PROVENANCE_UNKNOWN",
                "unsafe relevant path %r" % (value,))
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise GitProvenanceError(
                "GIT_PROVENANCE_UNKNOWN",
                "unsafe relevant path %r" % (value,))
        if value not in seen:
            clean.append(value)
            seen.add(value)
    return tuple(clean)


def require_clean_head(repo_root, relevant_paths):
    """Return the exact 40-hex HEAD after proving relevant files are clean.

    The proof covers tracked working-tree changes, staged changes, deletions,
    and untracked critical files.  Any unavailable or ambiguous git state is
    an error; ``UNKNOWN`` is never a usable provenance value.
    """
    try:
        root = os.path.realpath(os.fspath(repo_root))
    except (TypeError, ValueError, OSError) as exc:
        raise GitProvenanceError("GIT_PROVENANCE_UNKNOWN", str(exc))
    if not os.path.isdir(root):
        raise GitProvenanceError(
            "GIT_PROVENANCE_UNKNOWN", "repository root is not a directory")
    paths = _safe_relevant_paths(relevant_paths)

    top = _run_git(root, ["rev-parse", "--show-toplevel"])
    try:
        top_text = top.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as exc:
        raise GitProvenanceError("GIT_PROVENANCE_UNKNOWN", str(exc))
    if os.path.realpath(top_text) != root:
        raise GitProvenanceError(
            "GIT_PROVENANCE_UNKNOWN",
            "configured root is not the repository top level")

    raw_head = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    try:
        head = raw_head.decode("ascii", "strict").strip()
    except UnicodeDecodeError as exc:
        raise GitProvenanceError("GIT_PROVENANCE_UNKNOWN", str(exc))
    if _COMMIT_RE.fullmatch(head) is None:
        raise GitProvenanceError(
            "GIT_PROVENANCE_UNKNOWN", "HEAD is not an exact 40-hex commit")

    # Requiring each path to be tracked makes a newly introduced, untracked
    # mutation implementation a hard failure rather than an invisible file.
    tracked_raw = _run_git(
        root, ["--literal-pathspecs", "ls-files", "-z", "--"] + list(paths))
    tracked = {
        item.decode("utf-8", "strict")
        for item in tracked_raw.split(b"\x00") if item
    }
    if tracked != set(paths):
        missing = sorted(set(paths) - tracked)
        raise GitProvenanceError(
            "GIT_PROVENANCE_DIRTY",
            "mutation-critical files are untracked or missing: %s" %
            ",".join(missing))

    status = _run_git(
        root, ["--literal-pathspecs", "status", "--porcelain=v1", "-z",
               "--untracked-files=all", "--"] + list(paths))
    if status:
        display = status.replace(b"\x00", b"\n").decode(
            "utf-8", "replace").strip()[:1000]
        raise GitProvenanceError(
            "GIT_PROVENANCE_DIRTY",
            "mutation-critical files are dirty: %s" % display)
    return head
