"""Module-integrity guards: catch the class of bug that only shows up at
process start.

Four live ignitions on 2026-07-27 died inside the first minutes, three of
them on defects a unit test could never see because nothing imported the
failing path: a truncated edit left ALL-CAPS constants referenced but never
assigned (TAIL_CHEAP_ONLY, then TAIL_LOW_C/TAIL_HIGH_C/TAIL_CHEAP_MAX_C),
and the START receipt that dumps every knob is the first code to touch them.

These tests read the engine as source, so they hold regardless of whether a
given branch is exercised at runtime.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

ENGINE = (Path(__file__).resolve().parent.parent
          / "tools" / "research" / "crypto_mm" / "mm_engine.py")


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    def add_targets(node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(sub, ast.AnnAssign) and isinstance(
                    sub.target, ast.Name):
                names.add(sub.target.id)

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.If, ast.Try,
                             ast.With)):
            add_targets(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def test_no_undefined_module_constants():
    """Every ALL-CAPS global the engine reads must be assigned somewhere.

    A miss here is exactly the failure that killed two ignitions: import
    succeeds, tests pass, and the process dies when the START receipt (or
    any first-touch path) evaluates the missing name.
    """
    tree = ast.parse(ENGINE.read_text())
    known = _module_level_names(tree) | set(dir(builtins))
    # locals of every function are legitimate too
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            known.add(target.id)
                elif isinstance(sub, ast.arg):
                    known.add(sub.arg)
    missing = sorted({
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and n.id.isupper() and len(n.id) > 2 and n.id not in known
    })
    assert not missing, f"undefined ALL-CAPS constants: {missing}"


def test_no_undefined_helper_calls():
    """Every bare function call must resolve to something defined here,
    imported, or a builtin — the same first-touch trap for functions."""
    tree = ast.parse(ENGINE.read_text())
    known = _module_level_names(tree) | set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            known.add(node.name)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            known.add(target.id)
                elif isinstance(sub, ast.arg):
                    known.add(sub.arg)
                elif isinstance(sub, (ast.comprehension, ast.For,
                                      ast.AsyncFor)):
                    for tgt in ast.walk(sub.target):
                        if isinstance(tgt, ast.Name):
                            known.add(tgt.id)
                elif isinstance(sub, ast.withitem) and sub.optional_vars:
                    for tgt in ast.walk(sub.optional_vars):
                        if isinstance(tgt, ast.Name):
                            known.add(tgt.id)
    unresolved = sorted({
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id not in known
    })
    assert not unresolved, f"calls to undefined names: {unresolved}"
