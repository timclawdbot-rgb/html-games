#!/usr/bin/env python3
"""Product price finder (UK) using OpenClaw browser automation.

Goal: given a product URL or query, find alternative retailers and best-effort prices.

Notes / limitations:
- This is a best-effort heuristic tool. Product matching is hard.
- Uses DuckDuckGo HTML results to avoid paid APIs.
- Then visits a handful of candidate links and extracts a GBP price via regex.

Usage:
  ./scripts/product_price_finder.py --url "https://www.amazon.co.uk/dp/B0F3WLFCPL"
  ./scripts/product_price_finder.py --query "Samsung 990 EVO Plus 4TB"

Send results on Telegram:
  ./scripts/product_price_finder.py --url ... --channel telegram --target 476265210
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

DEFAULT_CHANNEL = "telegram"
DEFAULT_TARGET = "476265210"  # Tim

GBP_RE = re.compile(r"£\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{2})?)")


def run_cmd(args: list[str], *, timeout: int = 120) -> str:
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def openclaw_browser_start():
    run_cmd(["openclaw", "browser", "start"], timeout=60)


def openclaw_browser_open(url: str) -> str:
    out = run_cmd(["openclaw", "browser", "open", "--json", "--expect-final", "--timeout", "60000", url], timeout=120)
    obj = json.loads(out)
    tid = obj.get("targetId")
    if not tid:
        raise RuntimeError(f"No targetId from open: {out[:200]}")
    return tid


def openclaw_browser_close(target_id: str):
    try:
        run_cmd(["openclaw", "browser", "close", target_id], timeout=30)
    except Exception:
        pass


def openclaw_browser_eval(target_id: str, fn: str) -> dict[str, Any]:
    out = run_cmd(
        [
            "openclaw",
            "browser",
            "evaluate",
            "--json",
            "--expect-final",
            "--timeout",
            "60000",
            "--target-id",
            target_id,
            "--fn",
            fn,
        ],
        timeout=120,
    )
    obj = json.loads(out)
    return obj.get("result") or {}


def send_message(channel: str, target: str, message: str):
    run_cmd(["openclaw", "message", "send", "--channel", channel, "--target", target, "--message", message], timeout=60)


def parse_gbp(text: str | None) -> float | None:
    if not text:
        return None
    m = GBP_RE.search(text.replace("\u00a0", " "))
    if not m:
        return None
    s = m.group(1).replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


@dataclass
class Candidate:
    title: str
    url: str
    price_gbp: float | None
    price_raw: str | None


def ddg_search(query: str, *, max_results: int = 8) -> list[tuple[str, str]]:
    """Return [(title,url)] from DuckDuckGo HTML results."""
    q = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/html/?q={q}"
    tid = openclaw_browser_open(url)
    try:
        fn = r'''() => {
          const out = [];
          for (const a of document.querySelectorAll('a.result__a')) {
            const href = a.href;
            const title = (a.innerText || '').trim();
            if (!href || !title) continue;
            out.push({title, url: href});
          }
          return {results: out.slice(0, %d)};
        }''' % (max_results)
        res = openclaw_browser_eval(tid, fn)
        items = res.get("results") or []
        out: list[tuple[str, str]] = []
        for it in items:
            u = (it.get("url") or "").strip()
            t = (it.get("title") or "").strip()
            if u and t:
                out.append((t, u))
        return out
    finally:
        openclaw_browser_close(tid)


def extract_title_and_price(url: str) -> Candidate:
    tid = openclaw_browser_open(url)
    try:
        fn = r'''() => {
          const title = (document.querySelector('meta[property="og:title"]')?.content
            || document.title
            || '').trim();
          const bodyText = (document.body?.innerText || '');
          return {title, bodyText: bodyText.slice(0, 200000)};
        }'''
        res = openclaw_browser_eval(tid, fn)
        title = (res.get("title") or url).strip()
        body = res.get("bodyText") or ""
        # best-effort first GBP-looking token
        m = GBP_RE.search(body.replace("\u00a0", " "))
        raw = m.group(0) if m else None
        price = parse_gbp(raw)
        return Candidate(title=title, url=url, price_gbp=price, price_raw=raw)
    finally:
        openclaw_browser_close(tid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, help="Product URL")
    ap.add_argument("--query", default=None, help="Search query")
    ap.add_argument("--channel", default=None)
    ap.add_argument("--target", default=None)
    ap.add_argument("--max-results", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.url and not args.query:
        ap.error("Provide --url or --query")

    openclaw_browser_start()

    if args.query:
        query = args.query
        base_title = args.query
    else:
        # If URL provided, use it as a starting point and also use its title as query
        base = extract_title_and_price(args.url)
        base_title = base.title
        query = base_title

    # Add a UK bias keyword
    search_query = f"{query} price UK"

    # Search
    hits = ddg_search(search_query, max_results=args.max_results * 2)

    # Filter obvious junk domains
    bad = ("youtube.com", "facebook.com", "reddit.com", "wikipedia.org")
    filtered = [(t, u) for (t, u) in hits if not any(b in u for b in bad)]
    filtered = filtered[: args.max_results]

    # Visit candidates and attempt to extract a GBP price
    cands: list[Candidate] = []
    for t, u in filtered:
        try:
            c = extract_title_and_price(u)
            # keep original SERP title if page title is empty
            if not c.title or c.title == u:
                c.title = t
            cands.append(c)
        except Exception:
            continue

    priced = [c for c in cands if c.price_gbp is not None]
    priced.sort(key=lambda c: c.price_gbp)

    lines: list[str] = []
    lines.append(f"Price finder (best effort) — {base_title}")
    lines.append(f"Query: {search_query}")
    lines.append("")

    if priced:
        best = priced[0]
        lines.append(f"Best found: {best.price_raw or ''} — {best.title}")
        lines.append(best.url)
        lines.append("")

    lines.append("Candidates:")
    for c in (priced + [c for c in cands if c.price_gbp is None])[:10]:
        price = c.price_raw or "(no price found)"
        lines.append(f"- {price} — {c.title}")
        lines.append(f"  {c.url}")

    msg = "\n".join(lines).strip()

    if args.dry_run or not (args.channel or args.target):
        print(msg)
    else:
        send_message(args.channel or DEFAULT_CHANNEL, args.target or DEFAULT_TARGET, msg)


if __name__ == "__main__":
    main()
