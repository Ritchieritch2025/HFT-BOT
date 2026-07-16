#!/usr/bin/env python3
"""Kalshi pipeline status bot — read-only Telegram monitor.

Long-polls Telegram getUpdates and answers status queries from the pipeline's
on-disk state (work/live/*.json, the seal directory, process table, disk).
STRICTLY READ-ONLY: it never restarts, seals, deletes, orders, or mutates
anything. It only reads state and reports. Commands that would change system
state are intentionally absent — this is a monitor, not a control plane.

Runs on the EC2 box next to the pipeline. Credentials come from
~/.kalshi/env.sh (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, operator-created).
Only the allowlisted chat id (TELEGRAM_CHAT_ID) is answered; anything else is
ignored. stdlib only (urllib), matching the house SigV4 style.
"""
import datetime
import glob
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

ROOT = os.environ.get("KALSHI_ROOT", os.path.expanduser("~/hft-bot"))
LIVE = os.path.join(ROOT, "work", "live")
SEALS = os.path.join(ROOT, "work", "warehouse", "seals")
RAW = os.path.join(ROOT, "work", "raw")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
API = "https://api.telegram.org/bot%s/" % TOKEN


def _tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(API + method, data=data, timeout=65) as r:
            return json.load(r)
    except Exception as e:
        print("tg %s error: %s" % (method, e), flush=True)
        return {}


def send(text):
    _tg("sendMessage", chat_id=CHAT, text=text, disable_web_page_preview=1)


def _load(name):
    try:
        return json.load(open(os.path.join(LIVE, name)))
    except Exception:
        return None


def _ingest_alive():
    try:
        out = subprocess.run(["pgrep", "-f", "ingest.py --loop"],
                             capture_output=True, text=True, timeout=10)
        pids = [p for p in out.stdout.split() if p]
        return pids[0] if pids else None
    except Exception:
        return None


def _feed_fresh_secs():
    """Age (s) of the newest raw file, or None if raw dir missing/empty."""
    newest = 0.0
    for p in glob.glob(os.path.join(RAW, "date=*", "*")):
        try:
            m = os.path.getmtime(p)
            if m > newest:
                newest = m
        except OSError:
            pass
    if not newest:
        return None
    return time.time() - newest


def _sealed_days(n=6):
    days = sorted(os.path.basename(f)[5:-5]
                  for f in glob.glob(os.path.join(SEALS, "date=*.json")))
    return days[-n:]


def _disk_pct():
    try:
        out = subprocess.run(["df", "/"], capture_output=True, text=True, timeout=10)
        return int(out.stdout.splitlines()[1].split()[4].rstrip("%"))
    except Exception:
        return None


def cmd_status():
    cap = _load("capture_alert.json") or {}
    cap_ok = cap.get("status") == "ok"
    pid = _ingest_alive()
    fresh = _feed_fresh_secs()
    disk = _disk_pct()
    days = _sealed_days()
    latest_seal = days[-1] if days else "none"
    alarm = _load("seal_alarm.json")
    bad = []
    if not cap_ok:
        bad.append("capture=%s" % cap.get("status", "?"))
    if not pid:
        bad.append("ingest DOWN")
    if fresh is not None and fresh > 120:
        bad.append("feed stale %ds" % int(fresh))
    if disk is not None and disk > 80:
        bad.append("disk %d%%" % disk)
    if alarm:
        bad.append("seal_alarm:%s" % alarm.get("kind", "?"))
    verdict = "✅ 全绿" if not bad else "⚠️ " + " · ".join(bad)
    lines = [
        "%s" % verdict,
        "",
        "capture: %s" % cap.get("status", "?"),
        "ingest: %s" % ("pid %s" % pid if pid else "DOWN"),
        "feed: %s" % ("%.0fs ago" % fresh if fresh is not None else "n/a"),
        "disk /: %s" % ("%d%%" % disk if disk is not None else "n/a"),
        "latest seal: %s" % latest_seal,
        "",
        "%s UTC" % datetime.datetime.now(datetime.timezone.utc).strftime("%m-%d %H:%M"),
    ]
    return "\n".join(lines)


def cmd_seals():
    days = _sealed_days(8)
    if not days:
        return "no seals found"
    today = datetime.datetime.now(datetime.timezone.utc).date()
    span = [(today - datetime.timedelta(days=i)).isoformat() for i in range(1, 8)]
    rows = ["封印状态(近 7 日,不含今天):"]
    for d in reversed(span):
        rows.append("%s  %s" % (d, "✅ sealed" if d in days else "❌ 未封印"))
    return "\n".join(rows)


def cmd_pipeline():
    pid = _ingest_alive()
    cap = _load("capture_alert.json") or {}
    fresh = _feed_fresh_secs()
    ret = _load("raw_retention_alert.json") or {}
    lines = [
        "入库 daemon: %s" % ("pid %s (alive)" % pid if pid else "DOWN"),
        "capture: %s (silent %.1fs)" % (cap.get("status", "?"),
                                        cap.get("silent_secs", 0) or 0),
        "feed 最新: %s" % ("%.0fs ago" % fresh if fresh is not None else "n/a"),
        "raw 保留: cutoff %s, deleted %s" % (ret.get("cutoff", "?"),
                                             ret.get("deleted", "?")),
    ]
    return "\n".join(lines)


def cmd_disk():
    try:
        out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=10)
        return "```\n%s\n```" % out.stdout.strip()
    except Exception as e:
        return "disk read error: %s" % e


def cmd_balance():
    """Live READ-ONLY GET /portfolio/balance via the balance probe."""
    try:
        import importlib.util
        p = os.path.join(ROOT, "deploy", "balance_probe.py")
        spec = importlib.util.spec_from_file_location("balance_probe", p)
        bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bp)
        return bp.balance_line()
    except Exception as e:
        return "balance error: %s" % str(e)[:120]


def cmd_dates():
    """研究桶已封存可研究日期 + deep03 20 天倒计时。"""
    days = sorted(os.path.basename(f)[5:-5]
                  for f in glob.glob(os.path.join(SEALS, "date=*.json")))
    qual = [d for d in days if d >= "2026-07-12"]
    countdown = max(0, 20 - len(qual))
    rows = ["研究桶(已封存日期,合格口径以研究会话盘点为准):"]
    rows += ["  %s" % d for d in qual] or ["  (无)"]
    rows.append("deep03 倒计时: %d/20,还需 ~%d 天" % (len(qual), countdown))
    return "\n".join(rows)


def _s3_total_gb():
    """整桶(不只 research/)字节 → GB;失败返回 None。"""
    try:
        out = subprocess.run(
            ["bash", "-lc", ". ~/.kalshi/env.sh 2>/dev/null; "
             "aws s3 ls s3://kalshi-vault-ritcardo --recursive --summarize "
             "2>/dev/null | awk '/Total Size/{print $3}'"],
            capture_output=True, text=True, timeout=90)
        return float(out.stdout.strip()) / 1e9
    except Exception:
        return None


def cmd_cost():
    """月费用估算 — 覆盖全部主要科目(操作员更正 2026-07-16:旧版只算
    research/ 前缀,漏了生产 EC2/EBS/EIP/全桶,低估两个数量级)。

    费率出处:$0.4713/h r8g.2xlarge、gp3、IPv4 与 us-east-2 rate card 见
    deploy/w09/cost-contract.json 及 tools/research_data.py 同一费率卡;
    生产机与 W09 同型号同区。精确账永远以 AWS 账单为准。"""
    ec2 = 0.4713 * 730                      # 生产 r8g.2xlarge,24/7
    ebs = (600 + 300) * 0.08                # 生产 600GB + W09 300GB,gp3
    ipv4 = 2 * 0.005 * 730                  # 两个公网 IPv4
    gb = _s3_total_gb()
    s3 = gb * 0.023 if gb is not None else None
    parts = [
        "月费用估算(us-east-2 费率卡,精确以账单为准):",
        "  生产 EC2 r8g.2xlarge 24/7: $%.0f" % ec2,
        "  EBS 磁盘 900GB gp3: $%.0f" % ebs,
        ("  S3 全桶 %.0f GB: $%.1f(每日 +~27GB ≈ +$0.6/日)" % (gb, s3))
        if s3 is not None else "  S3 全桶: (读取失败)",
        "  公网 IPv4 ×2: $%.1f" % ipv4,
        "  W09 研究机: 按小时计,仅运行时 $0.4713/h",
        "  —— 合计 ≈ $%.0f/月 + W09 使用小时" % (
            ec2 + ebs + ipv4 + (s3 or 0)),
    ]
    return "\n".join(parts)


def _alerts_24h():
    """近 24h 报警事件(过滤 telegram 噪声行)。"""
    path = os.path.join(LIVE, "alerts.log")
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=24))
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                when = datetime.datetime.strptime(
                    line[:20].strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
            if when >= cutoff and "telegram" not in line:
                out.append(line.strip())
    except FileNotFoundError:
        pass
    return out[-6:]


def _raw_today_gb():
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    total = 0
    for p in glob.glob(os.path.join(RAW, "date=%s" % today, "*")):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total / 1e9


def cmd_ack():
    """唯一口令(操作员裁定 2026-07-16):一条消息 = 完整管道日报。"""
    yesterday = (datetime.datetime.now(datetime.timezone.utc).date()
                 - datetime.timedelta(days=1)).isoformat()
    days = _sealed_days(30)
    seal_y = "✅" if yesterday in days else "❌ 缺昨日封存!"
    alerts = _alerts_24h()
    alert_block = ("\n".join("  " + a for a in alerts)
                   if alerts else "  (无事件)")
    try:
        balance = cmd_balance()
    except Exception:
        balance = "balance: n/a"
    return "\n".join([
        cmd_status(),
        "",
        "今日原始数据: %.1f GB" % _raw_today_gb(),
        "昨日(%s)封存: %s" % (yesterday, seal_y),
        "",
        cmd_dates(),
        "",
        balance,
        "",
        cmd_cost(),
        "",
        "近 24h 报警:",
        alert_block,
    ])


def cmd_last():
    """最近一次封存 + 发布详情。"""
    days = sorted(os.path.basename(f)[5:-5]
                  for f in glob.glob(os.path.join(SEALS, "date=*.json")))
    if not days:
        return "无封存记录"
    last = days[-1]
    info = ["最近封存: %s" % last]
    try:
        s = json.load(open(os.path.join(SEALS, "date=%s.json" % last)))
        rows = s.get("archive_rows")
        if rows:
            info.append("  归档行数: %s" % f"{rows:,}")
        info.append("  method: %s" % s.get("method", "?"))
        info.append("  status: %s" % s.get("status", "?"))
    except Exception:
        pass
    return "\n".join(info)


HELP = (
    "Kalshi 管道监控 bot(只读)\n"
    "唯一口令:/ack — 一条消息返回完整管道日报\n"
    "(健康裁决 · 采集/入库 · 封存 · deep03 倒计时 · 余额 · 费用 · 24h 报警)\n"
    "\n只读监控,不含任何控制/下单命令。"
)

# 操作员裁定 2026-07-16:全部口令合并为一条 /ack。
# 旧命令函数保留为 /ack 的组成部件,不再单独暴露。
HANDLERS = {
    "/ack": cmd_ack,
    "/help": lambda: HELP, "/start": lambda: HELP,
}


def main():
    if not TOKEN or not CHAT:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset "
                         "(source ~/.kalshi/env.sh)")
    print("status bot up; allowlisted chat=%s" % CHAT, flush=True)
    offset = None
    # skip backlog: start from the latest pending update
    init = _tg("getUpdates", timeout=0)
    if init.get("ok") and init.get("result"):
        offset = init["result"][-1]["update_id"] + 1
    while True:
        resp = _tg("getUpdates", timeout=50, offset=offset)
        if not resp.get("ok"):
            time.sleep(3)
            continue
        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message") or {}
            chat = str((m.get("chat") or {}).get("id", ""))
            text = (m.get("text") or "").strip()
            if chat != str(CHAT):
                continue  # allowlist: ignore everyone else
            cmd = text.split()[0].lower() if text else ""
            cmd = cmd.split("@")[0]  # strip @botname suffix
            fn = HANDLERS.get(cmd)
            try:
                send(fn() if fn else "只有一条口令:/ack(管道日报)")
            except Exception as e:
                send("命令出错: %s" % e)


if __name__ == "__main__":
    main()
