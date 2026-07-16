"""Offline tests for the consolidated /ack digest (deploy/status_bot.py).

Operator ruling 2026-07-16: single /ack command; comprehensive cost block.
No network: aws/balance paths must degrade gracefully.
"""
import datetime as dt
import importlib.util
import os
import tempfile
import unittest


def load_bot(root):
    os.environ["KALSHI_ROOT"] = root
    spec = importlib.util.spec_from_file_location(
        "status_bot",
        os.path.join(os.path.dirname(__file__), "..", "deploy", "status_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAckDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        live = os.path.join(root, "work", "live")
        seals = os.path.join(root, "work", "warehouse", "seals")
        os.makedirs(live)
        os.makedirs(seals)
        now = dt.datetime.now(dt.timezone.utc)
        today = now.date()
        os.makedirs(os.path.join(root, "work", "raw", f"date={today}"))
        with open(os.path.join(root, "work", "raw", f"date={today}", "f.ndjson"),
                  "w") as f:
            f.write("x" * 2048)
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        for d in ("2026-07-12", "2026-07-13", yesterday):
            open(os.path.join(seals, f"date={d}.json"), "w").close()
        with open(os.path.join(live, "alerts.log"), "w") as f:
            old = now - dt.timedelta(hours=30)
            f.write(f"{old.strftime('%Y-%m-%dT%H:%M:%SZ')} ALERT: ancient\n")
            f.write(f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} ALERT: capture:gap\n")
            f.write(f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} WARN telegram x\n")
        self.bot = load_bot(root)
        # 离线:S3 读取直接判失败,避免测试机上意外调用 aws
        self.bot._s3_total_gb = lambda: None

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("KALSHI_ROOT", None)

    def test_handlers_single_command(self):
        cmds = [c for c in self.bot.HANDLERS if c not in ("/help", "/start")]
        self.assertEqual(cmds, ["/ack"])

    def test_cost_covers_major_lines_and_degrades(self):
        c = self.bot.cmd_cost()
        for token in ("生产 EC2", "EBS", "IPv4", "W09", "合计"):
            self.assertIn(token, c)
        self.assertIn("读取失败", c)  # 离线降级,不假装有数
        self.assertIn("$344", c)     # 0.4713*730 的整数位

    def test_ack_composes_all_sections(self):
        d = self.bot.cmd_ack()
        for token in ("今日原始数据", "昨日", "✅", "deep03 倒计时",
                      "月费用估算", "近 24h 报警", "capture:gap"):
            self.assertIn(token, d)
        self.assertNotIn("telegram x", d)   # 噪声行过滤
        self.assertLess(len(d), 4000)       # Telegram 单条上限内

    def test_missing_yesterday_seal_flags(self):
        y = (dt.datetime.now(dt.timezone.utc).date()
             - dt.timedelta(days=1)).isoformat()
        os.remove(os.path.join(self.tmp.name,
                               "work/warehouse/seals", f"date={y}.json"))
        self.assertIn("缺昨日封存", self.bot.cmd_ack())


if __name__ == "__main__":
    unittest.main()
