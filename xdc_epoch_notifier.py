#!/usr/bin/env python3
"""
XDC Epoch Rewards Notifier

Works out the masternode epoch rewards each owner address received since the
last run and pushes a report to ntfy.

Epoch rewards are not ordinary transactions, so they are inferred the same way
as XDC_Masternode_Rewards_Calculator does it:

    rewards = (balance_now - balance_then) - (transfers_in - transfers_out - fees)

"then" is the exact block the previous run stopped at (kept in the state file),
so a late, skipped or failed run never loses or double-counts rewards: the next
report simply covers the longer period.

Standard library only. Configuration comes from environment variables:

  XDC_REWARDS_CSV    required. One line per masternode:
                     owner_address,ntfy_topic[,etherscan_api_key]
  ETHERSCAN_API_KEY  optional. Used for lines that don't give their own key.
  NTFY_SERVER        optional. Default https://ntfy.sh
  NTFY_TOKEN         optional. Access token for protected ntfy topics.
  REPORT_TZ          optional. IANA time zone for dates. Default Australia/Sydney
  XDC_RPC_URLS       optional. Comma-separated RPC endpoints, tried in order.
  STATE_FILE         optional. Default last_balances.json
  DRY_RUN            optional. 1 = print notifications instead of sending them.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

WEI = 10**18
EPOCH_BLOCKS = 900               # XDPoS v2 epoch length
ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
XDC_CHAIN_ID = 50
DEFAULT_RPCS = ["https://erpc.xinfin.network", "https://rpc.ankr.com/xdc"]
# Stop a little behind the chain head so Etherscan's index has caught up with
# every transaction we count. Otherwise a transfer in the last few blocks could
# show up in the balance but not in the tx list, and be reported as a reward.
SAFETY_LAG_BLOCKS = 30
HISTORY_KEEP = 40
USER_AGENT = "XDC-Epoch-Notifier/2.0 (+https://github.com/s4njk4n/XDC_Epoch_Notifier)"


class CheckError(Exception):
    """A problem worth telling the user about in plain words."""


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def fmt_xdc(wei, decimals=4, sign=False):
    """Exact integer wei -> '1,234.5678' (no float rounding errors)."""
    neg = wei < 0
    wei = abs(wei)
    scale = 10 ** (18 - decimals)
    q = (wei + scale // 2) // scale            # round half up to `decimals`
    whole, frac = divmod(q, 10**decimals)
    s = f"{whole:,}"
    if decimals:
        s += "." + str(frac).rjust(decimals, "0")
    if neg and q:
        return "−" + s
    return ("+" + s) if sign and q else s


def short_addr(addr):
    """0xabc...  ->  xdcabcd…1234 (enough to recognise, not the whole thing)."""
    return "xdc" + addr[2:6] + "…" + addr[-4:]


def addr_key(addr):
    """State is keyed by a hash so the public repo doesn't list your address."""
    return hashlib.sha256(addr.encode()).hexdigest()[:16]


def local_stamp(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime("%-d %b %H:%M")


def tz_abbrev(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime("%Z")


def duration_text(seconds):
    hours = seconds / 3600
    if hours < 36:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f} days"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http(url, data=None, headers=None, timeout=20, attempts=3):
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429:
                break
            time.sleep(2 * (i + 1))
    raise last


class Rpc:
    def __init__(self, urls):
        self.urls = urls

    def call(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
        errors = []
        for url in self.urls:
            try:
                data = json.loads(http(url, body, {"Content-Type": "application/json"}))
                if data.get("error"):
                    raise CheckError(data["error"].get("message", str(data["error"])))
                if data.get("result") is None:
                    raise CheckError("empty result")
                return data["result"]
            except Exception as e:  # try the next endpoint
                errors.append(f"{urllib.parse.urlparse(url).netloc}: {e}")
        raise CheckError(f"XDC RPC {method} failed ({'; '.join(errors)})")

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def block_time(self, n):
        return int(self.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)

    def balance(self, addr, n):
        return int(self.call("eth_getBalance", [addr, hex(n)]), 16)

    def block_at_or_after(self, ts, latest):
        """First block with timestamp >= ts (binary search)."""
        lo, hi = 0, latest
        # XDC makes a block about every 2 s; start with a narrow guess.
        guess = max(0, latest - int((time.time() - ts) / 2 * 1.3) - 1000)
        if self.block_time(guess) < ts:
            lo = guess
        while lo < hi:
            mid = (lo + hi) // 2
            if self.block_time(mid) < ts:
                lo = mid + 1
            else:
                hi = mid
        return lo


class Etherscan:
    def __init__(self, key):
        self.key = key
        self._last = 0.0

    def _get(self, params):
        for attempt in range(4):
            wait = 0.4 - (time.time() - self._last)        # free tier: 3 calls/s
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            q = urllib.parse.urlencode({**params, "chainid": XDC_CHAIN_ID, "apikey": self.key})
            data = json.loads(http(f"{ETHERSCAN_URL}?{q}"))
            if data.get("status") == "1":
                return data["result"]
            result = data.get("result")
            detail = result if isinstance(result, str) else data.get("message", "")
            if "No transactions found" in (detail, data.get("message")):
                return []
            if re.search(r"rate limit", detail, re.I):
                time.sleep(1.5 * (attempt + 1))
                continue
            if re.search(r"invalid api key|missing", detail, re.I):
                raise CheckError("Etherscan rejected the API key. Check it's copied in full.")
            raise CheckError(f"Etherscan: {detail or 'unknown error'}")
        raise CheckError("Etherscan rate limit kept being hit. Will retry next run.")

    def txs(self, action, addr, start, end):
        """All txs in [start, end]. Pages by block window (10k results per query)."""
        out, cursor, limit = [], start, 10000
        while True:
            batch = self._get({"module": "account", "action": action, "address": addr,
                               "startblock": cursor, "endblock": end,
                               "page": 1, "offset": limit, "sort": "asc"})
            if len(batch) < limit:
                return out + batch
            last = int(batch[-1]["blockNumber"])
            done = [t for t in batch if int(t["blockNumber"]) < last]
            if not done:
                raise CheckError(f"Too many transactions in block {last} to page through.")
            out += done
            cursor = last


# --------------------------------------------------------------------------
# Reward maths
# --------------------------------------------------------------------------

def explained_change(addr, normal, internal):
    """Balance change that the address's own transactions explain."""
    tin = tout = fees = 0
    for tx in normal:
        failed = tx.get("isError") == "1"
        value = int(tx["value"])
        if not failed and (tx.get("to") or "").lower() == addr:
            tin += value
        if tx["from"].lower() == addr:
            if not failed:
                tout += value
            fees += int(tx["gasUsed"]) * int(tx["gasPrice"])
    for tx in internal:
        if tx.get("isError") == "1":
            continue
        value = int(tx["value"])
        if (tx.get("to") or "").lower() == addr:
            tin += value
        if (tx.get("from") or "").lower() == addr:
            tout += value
    return tin, tout, fees


def check_address(addr, api_key, rpc, prev, now=None):
    """
    Returns (result, new_state). result is a dict describing the period.
    prev is the saved state for this address, or None on the first run.
    """
    now = now or time.time()
    head = rpc.block_number()
    end = head - SAFETY_LAG_BLOCKS
    end_ts = rpc.block_time(end)
    end_bal = rpc.balance(addr, end)
    history = list(prev.get("history", [])) if prev else []

    if prev and prev.get("block") is not None:
        start = prev["block"] + 1
        start_bal = int(prev["balance_wei"])
        start_ts = prev["timestamp"]
        first_run = False
    else:
        # First run: try to look back 24 h. Needs an RPC that keeps old state
        # (archive). If it doesn't, start tracking from now.
        try:
            start = rpc.block_at_or_after(now - 86400, end)
            start_bal = rpc.balance(addr, start - 1)
            start_ts = rpc.block_time(start - 1)
            first_run = True
        except CheckError:
            state = {"block": end, "balance_wei": str(end_bal), "timestamp": end_ts, "history": []}
            return {"status": "baseline", "addr": addr, "balance": end_bal, "end_ts": end_ts}, state

    if end < start:   # nothing new since last run
        return {"status": "nothing_new", "addr": addr, "balance": end_bal}, prev

    eth = Etherscan(api_key)
    normal = eth.txs("txlist", addr, start, end)
    internal = eth.txs("txlistinternal", addr, start, end)
    tin, tout, fees = explained_change(addr, normal, internal)
    rewards = (end_bal - start_bal) - (tin - tout - fees)
    epochs = end // EPOCH_BLOCKS - (start - 1) // EPOCH_BLOCKS

    history.append({"end_ts": end_ts, "secs": end_ts - start_ts, "wei": str(rewards)})
    history = history[-HISTORY_KEEP:]
    state = {"block": end, "balance_wei": str(end_bal), "timestamp": end_ts, "history": history}

    week = [h for h in history if h["end_ts"] > end_ts - 7 * 86400 + 3600]
    return {
        "status": "ok", "addr": addr, "first_run": first_run,
        "rewards": rewards, "epochs": epochs, "balance": end_bal,
        "start_ts": start_ts, "end_ts": end_ts, "start_block": start, "end_block": end,
        "transfers_in": tin, "transfers_out": tout, "fees": fees,
        "tx_count": len(normal) + len(internal),
        "week_wei": sum(int(h["wei"]) for h in week),
        "week_secs": sum(h["secs"] for h in week),
    }, state


# --------------------------------------------------------------------------
# Notification text
# --------------------------------------------------------------------------

def describe(r, tz):
    a = short_addr(r["addr"])
    if r["status"] == "error":
        return (f"⚠️ {a}: couldn't check\n   {r['error']}\n"
                f"   Nothing is lost: the next report covers the gap.")
    if r["status"] == "baseline":
        return (f"🟢 {a}: tracking started\n"
                f"   Balance {fmt_xdc(r['balance'], 2)} XDC. First rewards report on the next run.")
    if r["status"] == "nothing_new":
        return f"• {a}: no new blocks since the last check"

    rw, ep = r["rewards"], r["epochs"]
    lines = []
    if rw > 0:
        lines.append(f"✅ {a}: {fmt_xdc(rw, sign=True)} XDC")
        if ep:
            lines.append(f"   {ep} epochs · {fmt_xdc(rw // ep)} XDC per epoch")
    elif rw == 0:
        lines.append(f"⚠️ {a}: NO rewards in {ep} epochs")
        lines.append("   Check the node is online and still a masternode.")
    else:
        lines.append(f"⚠️ {a}: {fmt_xdc(rw)} XDC unexplained balance drop")
        lines.append("   Your transactions don't account for it. Worth a look.")
    lines.append(f"   {local_stamp(r['start_ts'], tz)} → {local_stamp(r['end_ts'], tz)} "
                 f"{tz_abbrev(r['end_ts'], tz)} ({duration_text(r['end_ts'] - r['start_ts'])})")
    if r["week_secs"] > 1.5 * 86400:
        days = r["week_secs"] / 86400
        label = "7-day total" if days > 6.5 else f"last {days:.0f} days"
        lines.append(f"   {label}: {fmt_xdc(r['week_wei'], 2)} XDC "
                     f"(avg {fmt_xdc(int(r['week_wei'] / days), 2)}/day)")
    if r["tx_count"]:
        moved = r["transfers_in"] - r["transfers_out"] - r["fees"]
        lines.append(f"   {r['tx_count']} of your own txs adjusted for ({fmt_xdc(moved, 2, sign=True)} XDC)")
    lines.append(f"   Balance: {fmt_xdc(r['balance'], 2)} XDC")
    return "\n".join(lines)


def build_notification(topic, results, tz):
    ok = [r for r in results if r["status"] == "ok"]
    total = sum(r["rewards"] for r in ok)
    problem = any(r["status"] == "error" or (r["status"] == "ok" and r["rewards"] <= 0)
                  for r in results)

    if ok and len(results) == 1 and total == 0:
        title = "XDC rewards: none received ⚠️"
    elif ok:
        title = f"XDC rewards: {fmt_xdc(total, 2, sign=True)} XDC"
        if len(ok) > 1:
            title += f" ({len(ok)} nodes)"
        if problem:
            title += " ⚠️"
    elif any(r["status"] == "error" for r in results):
        title = "XDC rewards: check failed ⚠️"
    elif any(r["status"] == "baseline" for r in results):
        title = "XDC rewards: tracking started"
    else:
        title = "XDC rewards: nothing new"

    body = "\n\n".join(describe(r, tz) for r in results)
    if len(ok) > 1:
        body += f"\n\nTotal: {fmt_xdc(total, sign=True)} XDC"

    msg = {
        "topic": topic,
        "title": title,
        "message": body,
        "priority": 4 if problem else 3,   # quiet daily report, loud when something's off
        "tags": ["warning"] if problem else ["moneybag"],
    }
    if len(results) == 1:
        url = f"https://xdcscan.com/address/{results[0]['addr']}"
        msg["click"] = url
        msg["actions"] = [{"action": "view", "label": "Open in XDCScan", "url": url}]
    return msg


def send(msg, server, token, dry_run):
    if dry_run:
        print(f"--- DRY RUN: would send to {msg['topic']} ---")
        print(json.dumps(msg, indent=2, ensure_ascii=False))
        return
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    http(server.rstrip("/") + "/", json.dumps(msg).encode(), headers)


# --------------------------------------------------------------------------
# Config + state
# --------------------------------------------------------------------------

def parse_csv(text, default_key):
    rows, problems = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().replace("\r", "")
        if not line or line.startswith("#") or line.lower().startswith(("address", "owner")):
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        addr = parts[0].lower()
        if addr.startswith("xdc"):
            addr = "0x" + addr[3:]
        topic = parts[1] if len(parts) > 1 else ""
        key = (parts[2] if len(parts) > 2 else "") or default_key
        if not re.fullmatch(r"0x[0-9a-f]{40}", addr):
            problems.append(f"line {n}: '{parts[0]}' isn't a valid XDC address")
        elif not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            problems.append(f"line {n}: ntfy topic missing or has characters other than letters, digits, - and _")
        elif not key:
            problems.append(f"line {n}: no Etherscan API key (add a 3rd column or an ETHERSCAN_API_KEY secret)")
        else:
            rows.append((addr, topic, key))
    return rows, problems


def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 2:
        return {}   # old format (no block numbers) can't be used safely
    return data.get("addresses", {})


def save_state(path, addresses):
    with open(path + ".tmp", "w") as f:
        json.dump({"version": 2, "addresses": addresses}, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(path + ".tmp", path)


def main():
    csv_text = os.environ.get("XDC_REWARDS_CSV") or os.environ.get("REWARDS_CSV") or ""
    if not csv_text.strip():
        print("::warning::XDC_REWARDS_CSV secret is not set, so there is nothing to check. "
              "See the README's Quick Setup.")
        return 0

    tz = ZoneInfo(os.environ.get("REPORT_TZ") or "Australia/Sydney")
    rpcs = [u.strip() for u in (os.environ.get("XDC_RPC_URLS") or "").split(",") if u.strip()] or DEFAULT_RPCS
    state_path = os.environ.get("STATE_FILE", "last_balances.json")
    dry = os.environ.get("DRY_RUN") == "1"
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    token = os.environ.get("NTFY_TOKEN", "")

    rows, problems = parse_csv(csv_text, os.environ.get("ETHERSCAN_API_KEY", ""))
    for p in problems:
        print(f"::error::XDC_REWARDS_CSV {p}")

    rpc = Rpc(rpcs)
    state = load_state(state_path)
    by_topic, pending, failed = {}, {}, False

    for addr, topic, key in rows:
        k = addr_key(addr)
        try:
            result, new_state = check_address(addr, key, rpc, state.get(k))
            pending.setdefault(topic, []).append((k, new_state))
        except Exception as e:
            failed = True
            result = {"status": "error", "addr": addr, "error": str(e)[:300]}
            print(f"::error::{short_addr(addr)}: {e}")
        print(f"{short_addr(addr)} -> {result['status']}"
              + (f" {fmt_xdc(result['rewards'])} XDC" if result["status"] == "ok" else ""))
        by_topic.setdefault(topic, []).append(result)

    for topic, results in by_topic.items():
        try:
            send(build_notification(topic, results, tz), server, token, dry)
            print(f"Notification sent to topic {topic[:3]}…")
        except Exception as e:
            failed = True
            print(f"::error::Couldn't send ntfy notification: {e}")
            continue
        # Only move the bookmark forward once the report was delivered, so an
        # ntfy outage means the next report covers the gap instead of losing it.
        for k, s in pending.get(topic, []):
            state[k] = s
        if not dry:
            save_state(state_path, state)

    return 1 if (failed or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
