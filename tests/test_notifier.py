"""Offline tests: a fake XDC chain, fake Etherscan and fake ntfy.

Run:  python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import xdc_epoch_notifier as n  # noqa: E402

ADDR = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
TZ = ZoneInfo("Australia/Sydney")
X = n.WEI


class FakeWorld:
    """Chain where block b has timestamp T0 + 2b and balances are scripted."""

    T0 = 1_700_000_000

    def __init__(self, head, balances, txs=(), internal=(), archive=True, fail_rpc=False):
        self.head, self.balances = head, balances      # balances: {addr: fn(block)->wei}
        self.txs, self.internal = list(txs), list(internal)
        self.archive, self.fail_rpc = archive, fail_rpc
        self.sent = []

    def http(self, url, data=None, headers=None, timeout=20, attempts=3):
        if "etherscan" in url:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            src = self.txs if q["action"] == "txlist" else self.internal
            lo, hi = int(q["startblock"]), int(q["endblock"])
            res = [t for t in src if lo <= int(t["blockNumber"]) <= hi
                   and q["address"] in (t["from"], t.get("to"))]
            if not res:
                return json.dumps({"status": "0", "message": "No transactions found", "result": []})
            return json.dumps({"status": "1", "message": "OK", "result": res})
        if "ntfy" in url:
            self.sent.append(json.loads(data))
            return "{}"
        if self.fail_rpc:
            raise n.urllib.error.URLError("connection refused")
        body = json.loads(data)
        m, p = body["method"], body["params"]
        if m == "eth_blockNumber":
            r = hex(self.head)
        elif m == "eth_getBlockByNumber":
            r = {"timestamp": hex(self.T0 + 2 * int(p[0], 16))}
        elif m == "eth_getBalance":
            b = int(p[1], 16)
            if not self.archive and b < self.head - 128:
                return json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "missing trie node"}})
            r = hex(self.balances[p[0]](b))
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": r})


def tx(block, frm, to, value, gas_used=21000, gas_price=12_500_000_000, err="0"):
    return {"blockNumber": str(block), "from": frm, "to": to, "value": str(value),
            "gasUsed": str(gas_used), "gasPrice": str(gas_price), "isError": err}


def run(world, csv, state_file, extra_env=None, now=None):
    env = {"XDC_REWARDS_CSV": csv, "STATE_FILE": state_file, "NTFY_SERVER": "https://ntfy.test"}
    env.update(extra_env or {})
    old_http, old_env, old_time = n.http, dict(os.environ), n.time.time
    n.http = world.http
    n.time.sleep = lambda s: None
    if now:
        n.time.time = lambda: now
    os.environ.clear()
    os.environ.update(env)
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            code = n.main()
        return code, out.getvalue()
    finally:
        n.http, n.time.time = old_http, old_time
        os.environ.clear()
        os.environ.update(old_env)


class FormatTests(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(n.fmt_xdc(124_567_849_999_999_999_999), "124.5678")
        self.assertEqual(n.fmt_xdc(124_567_850_000_000_000_000), "124.5679")
        self.assertEqual(n.fmt_xdc(10_000_000 * X, 2), "10,000,000.00")
        self.assertEqual(n.fmt_xdc(5 * X, 2, sign=True), "+5.00")
        self.assertEqual(n.fmt_xdc(-5 * X, 2), "−5.00")
        self.assertEqual(n.fmt_xdc(0, sign=True), "0.0000")
        # 2^63 overflow that broke the bash version
        self.assertEqual(n.fmt_xdc(2**70), f"{2**70 // X:,}.{str(2**70 % X)[:4]}")

    def test_csv(self):
        csv = ("owner_address,ntfy_topic,key\r\n"
               f"  XDC{'AB'*20} , my-topic_1 , KEY1 \r\n"
               "# comment\n\n"
               f"0x{'cd'*20},topic2\n"
               "0xnothex,topic\n"
               f"0x{'ef'*20},bad topic!,K\n")
        rows, problems = n.parse_csv(csv, "DEFAULTKEY")
        self.assertEqual(rows, [(ADDR, "my-topic_1", "KEY1"), (OTHER, "topic2", "DEFAULTKEY")])
        self.assertEqual(len(problems), 2)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state.json")
        with open(self.state, "w") as f:
            f.write("\n")    # the repo's original (broken) empty file

    def world_day(self, head, **kw):
        # 10M XDC stake + 0.05 XDC reward per block from block 0
        return FakeWorld(head, {ADDR: lambda b: 10_000_000 * X + b * X // 20}, **kw)

    def test_first_run_with_archive_then_daily(self):
        head = 100_000
        w = self.world_day(head)
        now = FakeWorld.T0 + 2 * head
        code, out = run(w, f"{ADDR},topic,K", self.state, now=now)
        self.assertEqual(code, 0, out)
        msg = w.sent[0]
        # 24 h = 43,200 blocks; ends SAFETY_LAG blocks before head
        end = head - n.SAFETY_LAG_BLOCKS
        start = head - 43_200
        expected = (end - (start - 1)) * X // 20
        self.assertIn(n.fmt_xdc(expected, 2, sign=True), msg["title"])
        self.assertEqual(msg["priority"], 3)
        self.assertIn("xdcscan.com/address/" + ADDR, msg["click"])
        self.assertNotIn(ADDR, msg["message"])          # address masked in text
        with open(self.state) as f:
            saved = json.load(f)
        self.assertEqual(saved["version"], 2)
        self.assertNotIn(ADDR, json.dumps(saved))       # hashed in public repo

        # Next day, with an incoming transfer, an outgoing transfer + fee and a failed tx
        head2 = head + 43_200
        w2 = self.world_day(head2)
        bal = w2.balances[ADDR]
        fee = 21000 * 12_500_000_000
        w2.txs = [tx(head + 10, OTHER, ADDR, 500 * X),
                  tx(head + 20, ADDR, OTHER, 200 * X),
                  tx(head + 30, ADDR, OTHER, 999 * X, err="1")]
        w2.balances[ADDR] = lambda b: bal(b) + (500 * X if b >= head + 10 else 0) \
            - (200 * X + fee if b >= head + 20 else 0) - (fee if b >= head + 30 else 0)
        code, out = run(w2, f"{ADDR},topic,K", self.state, now=FakeWorld.T0 + 2 * head2)
        self.assertEqual(code, 0, out)
        reward = 43_200 * X // 20                          # exactly one day of block rewards
        self.assertIn(n.fmt_xdc(reward, 2, sign=True), w2.sent[0]["title"])
        self.assertIn("3 of your own txs", w2.sent[0]["message"])
        self.assertIn("48 epochs", w2.sent[0]["message"])

    def test_first_run_without_archive_sets_baseline(self):
        w = self.world_day(100_000, archive=False)
        code, out = run(w, f"{ADDR},topic,K", self.state, now=FakeWorld.T0 + 200_000)
        self.assertEqual(code, 0, out)
        self.assertEqual(w.sent[0]["title"], "XDC rewards: tracking started")

    def test_zero_rewards_is_loud(self):
        w = FakeWorld(100_000, {ADDR: lambda b: 10_000_000 * X})
        run(w, f"{ADDR},topic,K", self.state, now=FakeWorld.T0 + 200_000)
        self.assertEqual(w.sent[0]["priority"], 4)
        self.assertIn("none received", w.sent[0]["title"])
        self.assertIn("NO rewards", w.sent[0]["message"])

    def test_rpc_down_reports_error_and_keeps_state(self):
        w = self.world_day(100_000, fail_rpc=True)
        code, out = run(w, f"{ADDR},topic,K", self.state)
        self.assertEqual(code, 1)
        self.assertIn("check failed", w.sent[0]["title"])
        self.assertEqual(n.load_state(self.state), {})

    def test_two_nodes_one_topic_grouped(self):
        w = FakeWorld(100_000, {ADDR: lambda b: b * X // 20, OTHER: lambda b: b * X // 10})
        code, out = run(w, f"{ADDR},topic,K\n{OTHER},topic,K", self.state, now=FakeWorld.T0 + 200_000)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(w.sent), 1)
        self.assertIn("(2 nodes)", w.sent[0]["title"])
        self.assertIn("Total:", w.sent[0]["message"])

    def test_missing_secret_is_not_a_failure(self):
        code, out = run(FakeWorld(1, {}), "", self.state)
        self.assertEqual(code, 0)
        self.assertIn("not set", out)


if __name__ == "__main__":
    unittest.main()
