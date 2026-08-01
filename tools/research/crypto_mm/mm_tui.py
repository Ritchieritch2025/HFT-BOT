#!/usr/bin/env python3
"""Terminal control panel for the crypto-MM canary.

Same authority model as mm_panel.py (no exchange calls: it writes the
validated control document, edits the launch env, and starts/stops the
engine process), rendered as a curses TUI so it runs straight inside SSH.

Keys
  u  unpause (money on)      p  pause            k  KILL (cancel+latch)
  e  edit a launch param     l  edit a limit      s  start engine
  x  stop engine             r  restart engine    q  quit
  tab  cycle detail pane (events / log / metrics)

Run:  MM_TUI_REFRESH=1.5 python3 mm_tui.py --dir /home/ubuntu/mm_live_canary_v2
"""
from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import mm_control  # noqa: E402

HARD_MAX_COST = 200.0
HARD_MAX_CLIP = 20.0
HARD_MAX_NET = 100

LAUNCH_PARAMS = [
    ("MM_MARGIN", "min edge, cents"),
    ("MM_ECON_HALT", "realized-loss halt, $"),
    ("MM_ORDER_TTL_S", "deadman expiry, s"),
    ("MM_GAMMA", "inventory retreat, c/contract"),
    ("MM_MIN_QUOTE_AGE_S", "min quote age, s"),
    ("MM_MIN_REQUOTE", "min reprice delta, c"),
    ("MM_ANCHOR_SHIELD", "fast-anchor shield 0/1"),
    ("MM_ANCHOR_BETA_BN", "Binance beta"),
    ("MM_ANCHOR_BETA_CB", "Coinbase beta"),
    ("MM_PAIR", "pairing loop 0/1"),
    ("MM_ONLY_TICKER", "restrict to ticker"),
    ("MM_REQUIRE_FLAT_START", "flat-start gate 0/1"),
]
LIMITS = [("max_open_cost", "exposure cap $"), ("clip", "contracts/order"),
          ("max_net", "net position cap")]

# Heartbeats: proof the guards are alive, but they drown the event pane.
NOISE_EVENTS = {"BUDGET_ASSESS", "RECON_OK", "QUOTE_EVAL", "RTI_HYDRATE",
                "RTI_HYDRATE_STATUS", "WSFILL_SUB", "CONTROL_APPLIED"}


class Ctl:
    def __init__(self, base: Path):
        self.base = base
        self.ctrl = base / "mm_control.json"
        self.status = base / "mm_control_status.json"
        self.env = base / "launch.env"
        self.start_sh = base / "start.sh"
        self.log = base / "engine.log"
        self.out = base / "out"

    def read_ctrl(self):
        try:
            return mm_control.read_control(
                self.ctrl, hard_max_cost=HARD_MAX_COST,
                hard_max_clip=HARD_MAX_CLIP, hard_max_net=HARD_MAX_NET)
        except Exception as exc:
            return {"error": str(exc)[:80]}

    def read_status(self):
        try:
            return json.loads(self.status.read_text())
        except Exception:
            return {}

    def read_env(self):
        out = {}
        try:
            for line in self.env.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
        return out

    def pid(self):
        try:
            r = subprocess.run(
                ["pgrep", "-f", f"{self.base}/release/mm_engine"],
                capture_output=True, text=True, timeout=4)
            pids = [int(x) for x in r.stdout.split()]
            return pids[0] if pids else None
        except Exception:
            return None

    def apply_ctrl(self, fields):
        cur = self.read_ctrl()
        if "error" in cur:
            return cur["error"]
        nxt = dict(cur)
        nxt.update(fields)
        nxt["revision"] = int(cur["revision"]) + 1
        nxt["updated_at"] = dt.datetime.now(
            dt.timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            v = mm_control.validate_control(
                nxt, hard_max_cost=HARD_MAX_COST,
                hard_max_clip=HARD_MAX_CLIP, hard_max_net=HARD_MAX_NET)
            mm_control.atomic_write_json(self.ctrl, v)
            return f"control rev {v['revision']}"
        except Exception as exc:
            return f"rejected: {str(exc)[:70]}"

    def apply_env(self, key, value):
        cur = self.read_env()
        cur[key] = value
        body = "".join(f"{k}={v}\n" for k, v in cur.items())
        tmp = self.env.with_suffix(".env.tmp")
        tmp.write_text(body)
        os.replace(tmp, self.env)
        return f"{key}={value} (restart to apply)"

    def proc(self, action):
        pid = self.pid()
        if action == "stop":
            if not pid:
                return "not running"
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            return f"stopped {pid}"
        if action == "start":
            if pid:
                return f"already running ({pid})"
            if not self.start_sh.exists():
                return "start.sh missing"
            subprocess.Popen(["nohup", "bash", str(self.start_sh)],
                             stdout=open(self.log, "ab"),
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
            time.sleep(3)
            return f"started ({self.pid()})"
        if action == "restart":
            self.proc("stop")
            time.sleep(1)
            return self.proc("start")
        return "?"

    def events(self, n=200, noise=True):
        """Latest receipts, newest first.  ``noise`` hides the 1Hz budget
        heartbeat and 5s reconciliation so the trading story is readable;
        they are still written to disk and still counted."""
        out = []
        try:
            files = sorted(self.out.glob("mm_*.ndjson"))
            for fn in reversed(files[-2:]):
                for line in reversed(fn.read_text(errors="ignore").splitlines()):
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("ev") in ("HEALTH",):
                        continue
                    if noise and r.get("ev") in NOISE_EVENTS:
                        continue
                    out.append(r)
                    if len(out) >= n:
                        return out
        except Exception:
            pass
        return out

    def log_tail(self, n=40):
        try:
            return self.log.read_text(errors="ignore").splitlines()[-n:]
        except Exception:
            return ["(no log)"]


def summarize(events):
    """Live counters straight off the receipts — same definitions as
    mm_metrics.py so the TUI never disagrees with the reports."""
    acks = [e for e in events if e.get("ev") == "ORDER_ACK"]
    rejs = [e for e in events if e.get("ev") == "ORDER_REJ"]
    fills = [e for e in events if e.get("ev") == "FILL"]
    cancels = [e for e in events if e.get("ev") in
               ("CANCEL_ACK", "INTENT_CANCEL")]
    pulls = [e for e in events if e.get("ev") == "ANCHOR_PULL"]
    evals = [e for e in events if e.get("ev") == "QUOTE_EVAL"]
    ms = sorted(float(e["ms"]) for e in acks if e.get("ms") is not None)
    fees = sum(float(e.get("fee_dollars") or 0) for e in fills)
    edges = [float(e["edge_c"]) for e in evals
             if e.get("edge_c") is not None]
    return {
        "acks": len(acks), "rejs": len(rejs), "fills": len(fills),
        "cancels": len(cancels), "pulls": len(pulls), "evals": len(evals),
        "ack_p50": (ms[len(ms) // 2] if ms else None),
        "ack_max": (ms[-1] if ms else None),
        "fees": fees,
        "edge_seen": (max(edges) if edges else None),
    }


def fmt(v, nd=2, dash="-"):
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def run(stdscr, ctl: Ctl, refresh: float):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    for i, fg in enumerate((curses.COLOR_GREEN, curses.COLOR_RED,
                            curses.COLOR_YELLOW, curses.COLOR_CYAN), start=1):
        curses.init_pair(i, fg, -1)
    GREEN, RED, YELLOW, CYAN = (curses.color_pair(i) for i in range(1, 5))

    pane = 0
    hide_noise = True
    msg = ("keys: u unpause  p pause  k KILL  e env  l limits  s/x/r proc  h heartbeats  tab pane  q quit")
    last = 0.0
    cache = {}

    def prompt(win, label, default=""):
        curses.curs_set(1)
        stdscr.nodelay(False)
        h, w = stdscr.getmaxyx()
        curses.echo()
        stdscr.addstr(h - 1, 0, " " * (w - 1))
        stdscr.addstr(h - 1, 0, f"{label}: ")
        stdscr.clrtoeol()
        try:
            val = stdscr.getstr(h - 1, len(label) + 2, 40).decode().strip()
        except Exception:
            val = ""
        curses.noecho()
        curses.curs_set(0)
        stdscr.nodelay(True)
        return val or default

    while True:
        now = time.time()
        if now - last >= refresh:
            last = now
            cache = {
                "ctrl": ctl.read_ctrl(), "status": ctl.read_status(),
                "env": ctl.read_env(), "pid": ctl.pid(),
                "events": ctl.events(noise=hide_noise),
                "log": ctl.log_tail(),
            }
            cache["sum"] = summarize(cache["events"])

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        c, s, e, sm = (cache.get("ctrl", {}), cache.get("status", {}),
                       cache.get("env", {}), cache.get("sum", {}))
        row = 0
        stdscr.addstr(row, 0, f" MM CANARY — {ctl.base.name} ".ljust(w - 1),
                      curses.A_REVERSE)
        row += 2

        halted = bool(s.get("halted"))
        paused = bool((s.get("effective") or {}).get("paused", True))
        running = cache.get("pid") is not None
        stdscr.addstr(row, 2, "engine ")
        stdscr.addstr(f"{'RUNNING ' + str(cache['pid']) if running else 'STOPPED':16}",
                      GREEN if running else RED)
        stdscr.addstr("mode ")
        stdscr.addstr(f"{s.get('mode', '-'):8}", CYAN)
        stdscr.addstr("paused ")
        stdscr.addstr(f"{str(paused):7}", RED if paused else GREEN)
        stdscr.addstr("halted ")
        stdscr.addstr(f"{str(halted):7}", RED if halted else GREEN)
        row += 1
        stdscr.addstr(row, 2,
                      f"realized ${fmt(s.get('realized'))}   "
                      f"exposure ${fmt(s.get('exposure'))}   "
                      f"open {fmt(s.get('open_orders'), 0)}   "
                      f"pending {fmt(s.get('pending_new'), 0)}   "
                      f"rev {fmt(c.get('revision'), 0)}")
        row += 1
        stdscr.addstr(row, 2,
                      f"budget ok={s.get('budget_startup_ok')} "
                      f"latched={s.get('budget_latched')} "
                      f"floor=${(s.get('budget_floor_micro_usd') or 0) / 1e6:.3f}",
                      YELLOW if s.get("budget_latched") else 0)
        row += 1
        if s.get("control_error"):
            stdscr.addstr(row, 2, f"control_error: {s['control_error'][:w - 20]}",
                          RED)
            row += 1
        row += 1

        stdscr.addstr(row, 2, "LIVE COUNTERS", curses.A_BOLD)
        row += 1
        stdscr.addstr(row, 2,
                      f"evals {sm.get('evals', 0):<6} acks {sm.get('acks', 0):<5} "
                      f"rejs {sm.get('rejs', 0):<4} fills {sm.get('fills', 0):<5} "
                      f"cancels {sm.get('cancels', 0):<5} pulls {sm.get('pulls', 0):<4}")
        row += 1
        stdscr.addstr(row, 2,
                      f"ack ms p50 {fmt(sm.get('ack_p50'), 1)} / max "
                      f"{fmt(sm.get('ack_max'), 1)}    fees ${fmt(sm.get('fees'), 4)}"
                      f"    best edge seen {fmt(sm.get('edge_seen'), 2)}c")
        row += 2

        stdscr.addstr(row, 2, "LIMITS (l)", curses.A_BOLD)
        stdscr.addstr(row, 26, "LAUNCH (e, needs restart)", curses.A_BOLD)
        row += 1
        base_row = row
        for i, (k, desc) in enumerate(LIMITS):
            stdscr.addstr(base_row + i, 2, f"{k:15} {fmt(c.get(k))}")
        for i, (k, desc) in enumerate(LAUNCH_PARAMS):
            if base_row + i >= h - 12:
                break
            stdscr.addstr(base_row + i, 26,
                          f"{k:24} {str(e.get(k, '-')):8} {desc[:24]}")
        row = base_row + max(len(LIMITS), min(len(LAUNCH_PARAMS), h - 12 - base_row)) + 1

        panes = ("events", "log")
        stdscr.addstr(row, 2,
                      f"[{panes[pane]}]"
                      f"{'  heartbeats hidden (h)' if hide_noise else '  ALL events (h)'}",
                      curses.A_BOLD)
        row += 1
        avail = h - row - 2
        if panes[pane] == "events":
            for ev in cache.get("events", [])[:max(0, avail)]:
                if row >= h - 2:
                    break
                t = ev.get("wall_ns")
                ts = (dt.datetime.fromtimestamp(
                    t / 1e9, dt.timezone.utc).strftime("%H:%M:%S")
                    if t else "--:--:--")
                line = (f"{ts} {str(ev.get('ev'))[:18]:18} "
                        f"{str(ev.get('mt') or ev.get('ticker') or '')[-12:]:12} "
                        f"{str(ev.get('side') or ''):7} "
                        f"px={fmt(ev.get('px'), 3)} qty={fmt(ev.get('qty'), 2)} "
                        f"code={fmt(ev.get('code'), 0)} ms={fmt(ev.get('ms'), 1)} "
                        f"{str(ev.get('reason') or ev.get('why') or '')[:28]}")
                attr = 0
                if ev.get("ev") in ("HALT", "BUDGET_TRIP", "ORDER_REJ",
                                    "CANCEL_FAIL", "ORDER_UNKNOWN"):
                    attr = RED
                elif ev.get("ev") in ("FILL", "ORDER_ACK", "PAIR_LOCK"):
                    attr = GREEN
                elif ev.get("ev") in ("ANCHOR_PULL", "SENTINEL_PAUSE"):
                    attr = YELLOW
                stdscr.addstr(row, 2, line[:w - 3], attr)
                row += 1
        else:
            for line in cache.get("log", [])[-max(0, avail):]:
                if row >= h - 2:
                    break
                stdscr.addstr(row, 2, line[:w - 3])
                row += 1

        stdscr.addstr(h - 1, 0, msg[:w - 1].ljust(w - 1), curses.A_REVERSE)
        stdscr.refresh()

        try:
            ch = stdscr.getch()
        except Exception:
            ch = -1
        if ch == -1:
            time.sleep(0.05)
            continue
        key = chr(ch) if 32 <= ch < 127 else ""
        if key == "q":
            return
        if ch == 9:
            pane = (pane + 1) % len(panes)
        elif key == "u":
            if prompt(stdscr, "UNPAUSE — type YES") == "YES":
                msg = ctl.apply_ctrl({"paused": False,
                                      "note": "tui unpause"})
            else:
                msg = "unpause cancelled"
        elif key == "p":
            msg = ctl.apply_ctrl({"paused": True, "note": "tui pause"})
        elif key == "k":
            if prompt(stdscr, "KILL (cancel all + latch) — type KILL") == "KILL":
                msg = ctl.apply_ctrl({"kill": True, "paused": True,
                                      "note": "tui kill"})
            else:
                msg = "kill cancelled"
        elif key == "l":
            k = prompt(stdscr, f"limit {[x[0] for x in LIMITS]}")
            if k in dict(LIMITS):
                v = prompt(stdscr, f"{k} value")
                try:
                    val = (float(v) if k == "max_open_cost"
                           else int(v) if k == "max_net" else v)
                    msg = ctl.apply_ctrl({k: val})
                except ValueError:
                    msg = "bad value"
            else:
                msg = "unknown limit"
        elif key == "e":
            k = prompt(stdscr, "env key")
            if k in dict(LAUNCH_PARAMS):
                v = prompt(stdscr, f"{k} value")
                msg = ctl.apply_env(k, v)
            else:
                msg = "unknown env key"
        elif key == "h":
            hide_noise = not hide_noise
            msg = ("heartbeats hidden (BUDGET_ASSESS/RECON_OK/QUOTE_EVAL)"
                   if hide_noise else "showing ALL events")
        elif key == "s":
            msg = ctl.proc("start")
        elif key == "x":
            msg = ctl.proc("stop")
        elif key == "r":
            if prompt(stdscr, "restart engine — type YES") == "YES":
                msg = ctl.proc("restart")
            else:
                msg = "restart cancelled"
        last = 0.0        # force refresh after any action


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    refresh = float(os.environ.get("MM_TUI_REFRESH", "2.0"))
    ctl = Ctl(Path(args.dir))
    curses.wrapper(run, ctl, refresh)


if __name__ == "__main__":
    main()
