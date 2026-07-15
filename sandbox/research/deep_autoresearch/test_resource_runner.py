import importlib.util
import os
from pathlib import Path

import pytest


path = Path(__file__).with_name("resource_runner.py")
spec = importlib.util.spec_from_file_location("resource_runner", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def test_tree_bytes_counts_regular_files_and_ignores_symlinks(tmp_path):
    (tmp_path / "a").write_bytes(b"123")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b").write_bytes(b"12345")
    (tmp_path / "link").symlink_to(tmp_path / "a")
    assert mod.tree_bytes(tmp_path) == 8


def test_process_tree_rss_is_bounded_and_missing_pid_is_zero():
    if not Path("/proc/self/status").is_file():
        pytest.skip("Linux /proc resource sampling is exercised on W09")
    assert mod.process_tree_rss_kib(os.getpid()) > 0
    assert mod.process_tree_rss_kib(2**31 - 1) == 0
