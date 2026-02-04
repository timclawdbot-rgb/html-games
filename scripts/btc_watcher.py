#!/usr/bin/env python3
"""BTC daily-move watcher (free, no paid APIs).

- Fetches BTC price + 24h % change from CoinGecko (no API key).
- If abs(24h change) >= threshold, sends Telegram alert via OpenClaw.
- Uses a small local state file to avoid repeated alerts.

Intended to run via cron.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_CHANNEL = "telegram"
DEFAULT_TARGET = "476265210"  # Tim
DEFAULT_THRESHOLD_PCT = 10.0
DEFAULT_COOLDOWN_HOURS = 12
DEFAULT_STATE_PATH = "/home/tnu/clawd/data/btc_watch_state.json"


def now_ts() -> int:
    return int(time.time())


def run_cmd(args: list[str], *, timeout: int = 60) -> str:
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def send_message(channel: str, target: str, message: str):
    # Use OpenClaw messaging (no direct Telegram API usage)
    run_cmd(["openclaw", "message", "send", "--channel", channel, "--target", target, "--message", message], timeout=60)


def http_get_json(url: str, *, timeout: int = 20, retries: int = 3) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "openclaw-btc-watcher/1.0 (+https://openclaw.ai)",
            "Accept": "application/json",
        },
    )

    last_err: Exception | None = None
    for i in range(max(1, retries)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data)
        except Exception as e:
            last_err = e
            # Graceful handling for rate limiting / transient failures
            # If CoinGecko rate-limits us, just skip this run.
            msg = str(e)
            if "HTTP Error 429" in msg:
                raise RuntimeError("rate_limited")
            # small linear backoff
            time.sleep(1.5 * (i + 1))

    if last_err:
        raise last_err
    raise RuntimeError("http_get_json failed")


@dataclass
class BtcSnapshot:
    usd: float | None
    gbp: float | None
    change_24h_pct_usd: float | None
    last_updated_at: int | None


def fetch_btc() -> BtcSnapshot:
    # CoinGecko simple endpoint (free, no key)
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin&vs_currencies=usd,gbp"
        "&include_24hr_change=true"
        "&include_last_updated_at=true"
    )
    obj = http_get_json(url)
    b = obj.get("bitcoin") or {}
    return BtcSnapshot(
        usd=float(b["usd"]) if b.get("usd") is not None else None,
        gbp=float(b["gbp"]) if b.get("gbp") is not None else None,
        change_24h_pct_usd=float(b["usd_24h_change"]) if b.get("usd_24h_change") is not None else None,
        last_updated_at=int(b["last_updated_at"]) if b.get("last_updated_at") is not None else None,
    )


def load_state(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_state(path: str, state: dict[str, Any]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fmt_money(x: float | None, *, cur: str) -> str:
    if x is None:
        return "—"
    if cur == "USD":
        return f"${x:,.0f}"
    if cur == "GBP":
        return f"£{x:,.0f}"
    return f"{x:,.2f} {cur}"


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT, help="Alert threshold for abs(24h %% change)")
    ap.add_argument("--cooldown-hours", type=float, default=DEFAULT_COOLDOWN_HOURS, help="Min hours between alerts")
    ap.add_argument("--state", default=DEFAULT_STATE_PATH)
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        snap = fetch_btc()
    except Exception as e:
        # Rate limit or transient network error: skip silently (cron will retry next run)
        state = load_state(args.state)
        state["lastFetchError"] = str(e)[:200]
        state["lastFetchErrorTs"] = now_ts()
        save_state(args.state, state)
        return

    change = snap.change_24h_pct_usd
    if change is None:
        return

    state = load_state(args.state)
    last_alert_ts = int(state.get("lastAlertTs") or 0)
    last_alert_sign = state.get("lastAlertSign")  # -1 / +1

    cooldown_s = int(args.cooldown_hours * 3600)
    eligible = (now_ts() - last_alert_ts) >= cooldown_s

    # Only alert when above threshold AND (cooldown ok) AND (sign changed or never alerted)
    if abs(change) < float(args.threshold):
        # Reset sign memory when move no longer significant
        state["lastNonSignificantTs"] = now_ts()
        save_state(args.state, state)
        return

    sign = 1 if change > 0 else -1
    if not eligible and last_alert_sign == sign:
        return

    msg = (
        "BTC alert: significant 24h move\n"
        f"24h change: {fmt_pct(change)} (threshold {args.threshold:.1f}%)\n"
        f"Price: {fmt_money(snap.usd, cur='USD')} / {fmt_money(snap.gbp, cur='GBP')}\n"
        "Source: CoinGecko"
    )

    if args.dry_run:
        print(msg)
    else:
        send_message(args.channel, args.target, msg)

    state["lastAlertTs"] = now_ts()
    state["lastAlertSign"] = sign
    state["lastAlertChangePct"] = change
    state["lastAlertPriceUsd"] = snap.usd
    state["lastAlertPriceGbp"] = snap.gbp
    save_state(args.state, state)


if __name__ == "__main__":
    main()
