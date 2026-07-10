#!/usr/bin/env python3
"""Validate tools.json against the Makefile (PLAN_PROD_V1 P1).

Always checks: valid JSON, required fields, safety/kind enums, pass_token where a
run-button parses output, no duplicate names, and COVERAGE — every buildable
Makefile $(BUILD)/<tool> target (minus non-tool artifacts and _tsan variants) has
a registry entry, and every registered ./build/<x> cmd maps to a real target.

REVERSE SCAN (2026-07-10, operator ruling ③ / STEP-6 audit N7): the checks
above only validated registry→disk, so a runnable file ON DISK but absent
from tools.json passed silently (how rtt_baseline_sampler.sh went unnoticed).
Every tools/*.py, tools/*.sh and work/research/*.py must now be referenced by
some registry cmd OR appear in SCRIPT_LIBS (import-only helper libraries,
never run directly) — anything else fails the gate (E3: the registry is
complete, machine-checked).

With --require-built: additionally assert each non-on_demand binary + every script
path actually exists (run after a full `make`). stdlib only.
"""
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAFETY = {"pure", "offline", "network_read", "live_order"}
KIND = {"test", "bench", "probe", "daemon", "check", "example", "tool"}
REQUIRED = ["name", "kind", "safety", "cmd", "description", "docs"]
# Makefile $(BUILD)/<x> targets that are NOT standalone tools.
NON_TOOLS = {"ixws", "scratch"}
# Reverse-scan allowlist: import-only helper libraries under the scanned
# globs — libraries, not tools; adding a RUNNABLE script here to dodge
# registration is an E3 violation.
SCRIPT_LIBS = {
    "tools/warehouse_common.py",   # shared config/path helpers (library)
    "tools/gold_io.py",            # gold layer library modules (imported by
    "tools/gold_dtype.py",         #  gold_build/gold_validate entries)
    "tools/gold_fsm.py",
    "tools/gold_merge.py",
    "tools/gold_load.py",
}
SCAN_GLOBS = ("tools/*.py", "tools/*.sh", "work/research/*.py")


def main():
    require_built = "--require-built" in sys.argv
    errs = []

    with open(os.path.join(ROOT, "tools.json")) as f:
        try:
            data = json.load(f)
        except Exception as e:
            print("REGISTRY FAIL\n  - tools.json is not valid JSON: %s" % e)
            return 1
    tools = data.get("tools", [])
    if not tools:
        print("REGISTRY FAIL\n  - tools.json has no 'tools' array")
        return 1

    names = set()
    build_bins = {}  # basename -> entry, for cmds of the form ./build/<x>
    referenced = set()  # repo-relative script paths referenced by any cmd/args
    for t in tools:
        n = t.get("name", "<noname>")
        for r in REQUIRED:
            if r not in t:
                errs.append("%s: missing required field '%s'" % (n, r))
        if t.get("safety") not in SAFETY:
            errs.append("%s: invalid safety '%s'" % (n, t.get("safety")))
        if t.get("kind") not in KIND:
            errs.append("%s: invalid kind '%s'" % (n, t.get("kind")))
        # A run-button parses output for test/check kinds -> pass_token required.
        if t.get("kind") in {"test", "check"} and not t.get("pass_token"):
            errs.append("%s: kind=%s requires a pass_token" % (n, t.get("kind")))
        if n in names:
            errs.append("%s: duplicate name" % n)
        names.add(n)

        cmd = t.get("cmd", "")
        cmd0 = cmd.split()[0] if cmd.split() else ""
        m = re.match(r"^\./(build/([A-Za-z0-9_]+))$", cmd0)
        if m:
            build_bins[m.group(2)] = t
            if require_built and t.get("build") != "on_demand":
                if not os.path.exists(os.path.join(ROOT, m.group(1))):
                    errs.append("%s: binary %s not built" % (n, m.group(1)))
        else:
            # Script/interpreter tool: the script file must exist (always cheap
            # to check). Accept both ./tools/x.py and python3 tools/x.py forms.
            parts = cmd.split()
            script = cmd0
            if os.path.basename(cmd0) in ("python3", "python", "bash", "sh") and len(parts) > 1:
                script = parts[1]
            rel = script[2:] if script.startswith("./") else script
            referenced.add(rel)
            if not os.path.exists(os.path.join(ROOT, rel)):
                errs.append("%s: cmd path '%s' does not exist" % (n, cmd))
        # wrapper entries (e.g. run_pytest.sh <suite>) name their real target
        # in args_template — count those as referenced too
        for tok in t.get("args_template", "").split():
            tok = tok[2:] if tok.startswith("./") else tok
            if tok.endswith((".py", ".sh")):
                referenced.add(tok)

    # Coverage: derive tool set from the Makefile and require registration.
    with open(os.path.join(ROOT, "Makefile")) as f:
        mk = f.read()
    targets = set(re.findall(r"^\$\(BUILD\)/([A-Za-z0-9_]+):", mk, re.M))
    tool_targets = {x for x in targets if x not in NON_TOOLS and not x.endswith("_tsan")}
    for tg in sorted(tool_targets):
        if tg not in build_bins:
            errs.append("Makefile builds '%s' but it is NOT registered in tools.json" % tg)
    for b in sorted(build_bins):
        if b not in tool_targets:
            errs.append("tools.json registers ./build/%s but no Makefile target builds it" % b)

    # Reverse scan: disk -> registry (see module docstring).
    n_scanned = 0
    for pat in SCAN_GLOBS:
        for path in sorted(glob.glob(os.path.join(ROOT, pat))):
            rel = os.path.relpath(path, ROOT)
            n_scanned += 1
            if rel not in referenced and rel not in SCRIPT_LIBS:
                errs.append("on disk but NOT in tools.json (E3 drift): %s — "
                            "register it or add to SCRIPT_LIBS if import-only"
                            % rel)
    for lib in sorted(SCRIPT_LIBS):
        if lib in referenced:
            errs.append("SCRIPT_LIBS lists %s but a registry cmd runs it — "
                        "remove from the allowlist" % lib)
        if not os.path.exists(os.path.join(ROOT, lib)):
            errs.append("SCRIPT_LIBS lists %s but it does not exist" % lib)

    if errs:
        print("REGISTRY FAIL")
        for e in errs:
            print("  - " + e)
        return 1

    lo = sorted(t["name"] for t in tools if t.get("safety") == "live_order")
    print("registry ok: %d tools, %d build targets covered; live_order (console-forbidden)=%s"
          % (len(tools), len(tool_targets), lo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
