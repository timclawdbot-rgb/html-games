#!/usr/bin/env python3
"""Send top Hacker News items to Telegram.

Fetches the HN Top Stories list via the official Firebase API (no key required)
then sends a formatted summary via OpenClaw messaging.

Cron example (07:00 daily):
  0 7 * * * /usr/bin/python3 /home/tnu/clawd/scripts/hn_top10.py --channel telegram --target 476265210 >> /home/tnu/clawd/logs/hn_top10.log 2>&1
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.request
from typing import Any

HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_ITEM_PAGE = "https://news.ycombinator.com/item?id={id}"

DEFAULT_CHANNEL = "telegram"
DEFAULT_TARGET = "476265210"  # Tim


def run_cmd(args: list[str], *, timeout: int = 60) -> str:
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def send_message(channel: str, target: str, message: str) -> None:
    # Telegram hard limit is 4096; keep margin.
    if len(message) > 3500:
        message = message[:3480].rstrip() + "\n…(truncated)"
    run_cmd(["openclaw", "message", "send", "--channel", channel, "--target", target, "--message", message], timeout=60)


def http_json(url: str, *, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "openclaw-hn-top10/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read().decode("utf-8", errors="replace")
    return json.loads(data)


def fmt_item(n: int, item: dict[str, Any]) -> str:
    item_id = item.get("id")
    title = (item.get("title") or "(no title)").strip()
    score = item.get("score")
    comments = item.get("descendants")
    url = item.get("url")

    meta_bits = []
    if isinstance(score, int):
        meta_bits.append(f"{score} pts")
    if isinstance(comments, int):
        meta_bits.append(f"{comments} comments")
    meta = (" — " + ", ".join(meta_bits)) if meta_bits else ""

    lines = [f"{n}. {title}{meta}"]
    if item_id:
        lines.append(HN_ITEM_PAGE.format(id=item_id))
    if url and isinstance(url, str) and url.strip():
        lines.append(url.strip())
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=120, help="Overall timeout seconds")
    args = ap.parse_args()

    t0 = dt.datetime.now(dt.timezone.utc)

    top_ids = http_json(HN_TOP_URL)
    if not isinstance(top_ids, list):
        raise RuntimeError("Unexpected topstories payload")

    items: list[dict[str, Any]] = []
    # Fetch a few extra to compensate for deleted/invalid items
    want = max(1, int(args.count))
    for item_id in top_ids[: max(50, want * 3)]:
        if (dt.datetime.now(dt.timezone.utc) - t0).total_seconds() > float(args.timeout):
            break
        try:
            obj = http_json(HN_ITEM_URL.format(id=int(item_id)))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("deleted") or obj.get("dead"):
            continue
        if obj.get("type") not in ("story", None):
            continue
        items.append(obj)
        if len(items) >= want:
            break

    # If we didn't get enough, still send what we have.
    local_date = dt.datetime.now().strftime("%Y-%m-%d")
    lines = [f"HN Top {min(want, len(items))} — {local_date}", ""]
    for i, it in enumerate(items[:want], start=1):
        lines.append(fmt_item(i, it))
        lines.append("")

    msg = "\n".join(lines).strip()
    send_message(args.channel, args.target, msg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        # Best-effort: try to notify Telegram if messaging args are present.
        try:
            ch = DEFAULT_CHANNEL
            tg = DEFAULT_TARGET
            if "--channel" in sys.argv:
                ch = sys.argv[sys.argv.index("--channel") + 1]
            if "--target" in sys.argv:
                tg = sys.argv[sys.argv.index("--target") + 1]
            send_message(ch, tg, f"HN top10 cron failed: {type(e).__name__}: {e}")
        except Exception:
            pass
        raise
