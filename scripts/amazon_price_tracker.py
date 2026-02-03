#!/usr/bin/env python3
"""Generic Amazon ASIN price tracker using OpenClaw Browser + Telegram delivery.

- Starts OpenClaw browser if needed.
- Visits each ASIN with small random delays.
- Extracts title + buybox price.
- Stores history in SQLite.
- Sends a daily summary message.

This avoids AI usage/credits: it uses only local browser automation + messaging.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


DEFAULT_DB = "/home/tnu/clawd/data/amazon_price_history.sqlite3"
DEFAULT_CHANNEL = "telegram"
DEFAULT_TARGET = "476265210"  # Tim


PRICE_RE = re.compile(r"([0-9]+(?:\.[0-9]{2})?)")


def run_cmd(args: list[str], *, timeout: int = 120) -> str:
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def rand_sleep(min_s: float, max_s: float):
    time.sleep(random.uniform(min_s, max_s))


def parse_price_gbp(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = PRICE_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def now_ts() -> int:
    return int(time.time())


def local_day(ts: int) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
          asin TEXT PRIMARY KEY,
          label TEXT,
          created_ts INTEGER
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_checks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT,
          ts INTEGER,
          day TEXT,
          asin TEXT,
          label TEXT,
          title TEXT,
          url TEXT,
          price_raw TEXT,
          price_gbp REAL,
          ok INTEGER,
          error TEXT,
          FOREIGN KEY(asin) REFERENCES products(asin)
        );
        """
    )

    # Lightweight migrations / extra fields
    _ensure_column(conn, "price_checks", "buybox_price_raw", "TEXT")
    _ensure_column(conn, "price_checks", "buybox_price_gbp", "REAL")
    _ensure_column(conn, "price_checks", "lowest_new_price_raw", "TEXT")
    _ensure_column(conn, "price_checks", "lowest_new_price_gbp", "REAL")
    _ensure_column(conn, "price_checks", "price_source", "TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_day_asin ON price_checks(day, asin);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_run ON price_checks(run_id);")
    conn.commit()
    return conn


def upsert_product(conn: sqlite3.Connection, asin: str, label: str):
    conn.execute(
        "INSERT INTO products(asin,label,created_ts) VALUES(?,?,?) ON CONFLICT(asin) DO UPDATE SET label=excluded.label",
        (asin, label, now_ts()),
    )


def store_check(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ts: int,
    asin: str,
    label: str,
    title: str | None,
    url: str | None,
    price_raw: str | None,
    price_gbp: float | None,
    buybox_price_raw: str | None,
    buybox_price_gbp: float | None,
    lowest_new_price_raw: str | None,
    lowest_new_price_gbp: float | None,
    price_source: str | None,
    ok: bool,
    error: str | None,
):
    conn.execute(
        """
        INSERT INTO price_checks(
          run_id,ts,day,asin,label,title,url,
          price_raw,price_gbp,
          buybox_price_raw,buybox_price_gbp,
          lowest_new_price_raw,lowest_new_price_gbp,
          price_source,
          ok,error
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            ts,
            local_day(ts),
            asin,
            label,
            title,
            url,
            price_raw,
            price_gbp,
            buybox_price_raw,
            buybox_price_gbp,
            lowest_new_price_raw,
            lowest_new_price_gbp,
            price_source,
            1 if ok else 0,
            error,
        ),
    )


def openclaw_browser_start():
    run_cmd(["openclaw", "browser", "start"], timeout=60)


def openclaw_browser_open(url: str) -> str:
    out = run_cmd(["openclaw", "browser", "open", "--json", "--expect-final", "--timeout", "60000", url], timeout=90)
    obj = json.loads(out)
    tid = obj.get("targetId")
    if not tid:
        raise RuntimeError(f"No targetId from open: {out[:200]}")
    return tid


def openclaw_browser_navigate(target_id: str, url: str):
    # Navigate within an existing tab
    run_cmd(["openclaw", "browser", "navigate", "--target-id", target_id, url], timeout=90)


def openclaw_browser_close(target_id: str):
    try:
        run_cmd(["openclaw", "browser", "close", target_id], timeout=30)
    except Exception:
        pass


def openclaw_browser_eval_product(target_id: str) -> dict[str, Any]:
    # Extract title + buy-box price + best-effort offers link (best-effort)
    fn = r'''() => {
      const buyboxPrice = document.querySelector("#corePriceDisplay_desktop_feature_div .a-price .a-offscreen")?.innerText
        || document.querySelector("#corePriceDisplay_desktop_feature_div .a-offscreen")?.innerText
        || document.querySelector(".a-price .a-offscreen")?.innerText
        || null;

      // Link to offers / buying options (varies a lot). Try a few patterns.
      const offersHref = (
        document.querySelector('#buybox-see-all-buying-choices a')?.href
        || document.querySelector("a[href*='/gp/offer-listing/']")?.href
        || document.querySelector("a[href*='offer-listing']")?.href
        || null
      );

      return {
        title: (document.getElementById("productTitle")?.innerText||"").trim(),
        buyboxPrice,
        offersUrl: offersHref,
        url: location.href
      };
    }'''

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
        timeout=90,
    )
    obj = json.loads(out)
    return obj.get("result") or {}


def openclaw_browser_eval_lowest_new_offer(target_id: str) -> dict[str, Any]:
    # On the All Offers Display (AOD) view, pick the lowest "New" offer currently loaded.
    fn = r'''() => {
      const offers = [...document.querySelectorAll('#aod-offer-list #aod-offer')];
      function priceToNum(s){
        if(!s) return null;
        const m = (s.replace(/,/g,'').match(/([0-9]+(?:\.[0-9]{2})?)/));
        return m ? parseFloat(m[1]) : null;
      }
      let best = null;
      let bestNum = null;
      let newCount = 0;
      for(const offer of offers){
        const txt = (offer.innerText||'').trim();
        const first = (txt.split(/\n/)[0]||'').trim();
        if(!/^New$/i.test(first)) continue;
        newCount++;
        const priceText = offer.querySelector('span[id^="aod-price-"]')?.innerText || null;
        const n = priceToNum(priceText);
        if(n == null) continue;
        if(bestNum == null || n < bestNum){
          bestNum = n;
          best = priceText;
        }
      }
      return {loadedOfferCount: offers.length, newOfferCount: newCount, lowestNewPrice: best};
    }'''

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
        timeout=90,
    )
    obj = json.loads(out)
    return obj.get("result") or {}


def openclaw_browser_scroll_more(target_id: str, px: int = 1800) -> dict[str, Any]:
    fn = f"() => {{ const before = window.scrollY; window.scrollBy(0, {px}); const after = window.scrollY; const atEnd = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 5); return {{before, after, atEnd, scrollHeight: document.body.scrollHeight}}; }}"
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
        timeout=90,
    )
    obj = json.loads(out)
    return obj.get("result") or {}


def send_message(channel: str, target: str, message: str):
    run_cmd(["openclaw", "message", "send", "--channel", channel, "--target", target, "--message", message], timeout=60)


@dataclass
class WatchItem:
    asin: str
    label: str


def load_watchlist(path: str) -> tuple[str, list[WatchItem]]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    name = obj.get("name") or os.path.basename(path)
    items = []
    for it in obj.get("items", []):
        asin = (it.get("asin") or "").strip()
        label = (it.get("label") or asin).strip()
        if asin:
            items.append(WatchItem(asin=asin, label=label))
    if not items:
        raise ValueError(f"No items in watchlist: {path}")
    return name, items


def daily_min_prices(conn: sqlite3.Connection, asin: str, limit_days: int = 7) -> list[tuple[str, float]]:
    rows = conn.execute(
        """
        SELECT day, MIN(price_gbp) AS p
        FROM price_checks
        WHERE asin = ? AND ok = 1 AND price_gbp IS NOT NULL
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
        """,
        (asin, limit_days),
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def yesterday_min(conn: sqlite3.Connection, asin: str, today: str) -> float | None:
    row = conn.execute(
        """
        SELECT MIN(price_gbp)
        FROM price_checks
        WHERE asin = ? AND ok = 1 AND price_gbp IS NOT NULL AND day < ?
        ORDER BY day DESC
        LIMIT 1
        """,
        (asin, today),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def fmt_money(x: float | None) -> str:
    return "—" if x is None else f"£{x:.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", required=True, help="Path to watchlist JSON")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite db path (default: {DEFAULT_DB})")
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--min-delay", type=float, default=2.0)
    ap.add_argument("--max-delay", type=float, default=6.0)
    ap.add_argument("--history-days", type=int, default=5, help="Days of history to include per item")
    ap.add_argument("--dry-run", action="store_true", help="Do not send a message; print it instead")
    args = ap.parse_args()

    watch_name, items = load_watchlist(args.watchlist)

    conn = init_db(args.db)
    run_id = str(uuid.uuid4())
    ts = now_ts()
    today = local_day(ts)

    # Ensure browser is up
    openclaw_browser_start()

    results: list[dict[str, Any]] = []

    for item in items:
        upsert_product(conn, item.asin, item.label)
        conn.commit()

        # Visit product page
        try:
            target_id = openclaw_browser_open(f"https://www.amazon.co.uk/dp/{item.asin}")
            rand_sleep(args.min_delay, args.max_delay)
            data = openclaw_browser_eval_product(target_id)
            rand_sleep(args.min_delay, args.max_delay)

            title = (data.get("title") or "").strip()
            url = data.get("url")

            buybox_raw = data.get("buyboxPrice")
            buybox_gbp = parse_price_gbp(buybox_raw)

            lowest_new_raw = None
            lowest_new_gbp = None

            offers_url = data.get("offersUrl")
            if offers_url:
                try:
                    # Force NEW-only if possible
                    if "condition=ALL" in offers_url:
                        offers_url = offers_url.replace("condition=ALL", "condition=NEW")
                    elif "condition=" not in offers_url:
                        offers_url += ("&" if "?" in offers_url else "?") + "condition=NEW"

                    # Load offers (AOD) in the same tab
                    openclaw_browser_navigate(target_id, offers_url)
                    rand_sleep(args.min_delay, args.max_delay)

                    # AOD often lazy-loads; sample a few scroll positions and keep the lowest NEW found
                    best_raw = None
                    best_gbp = None
                    for _ in range(4):
                        od = openclaw_browser_eval_lowest_new_offer(target_id)
                        cand_raw = od.get("lowestNewPrice")
                        cand_gbp = parse_price_gbp(cand_raw)
                        if cand_gbp is not None and (best_gbp is None or cand_gbp < best_gbp):
                            best_gbp = cand_gbp
                            best_raw = cand_raw

                        sc = openclaw_browser_scroll_more(target_id)
                        rand_sleep(args.min_delay, args.max_delay)
                        if sc.get("atEnd"):
                            break

                    lowest_new_raw = best_raw
                    lowest_new_gbp = best_gbp
                except Exception:
                    # best-effort: ignore and fall back to buybox
                    lowest_new_raw = None
                    lowest_new_gbp = None

            # Choose the price we track as primary
            if lowest_new_gbp is not None:
                price_raw = lowest_new_raw
                price_gbp = lowest_new_gbp
                price_source = "lowest_new_offer"
            elif buybox_gbp is not None:
                price_raw = buybox_raw
                price_gbp = buybox_gbp
                price_source = "buybox"
            else:
                price_raw = None
                price_gbp = None
                price_source = "none"

            ok = bool(title)
            store_check(
                conn,
                run_id=run_id,
                ts=ts,
                asin=item.asin,
                label=item.label,
                title=title or None,
                url=url,
                price_raw=price_raw,
                price_gbp=price_gbp,
                buybox_price_raw=buybox_raw,
                buybox_price_gbp=buybox_gbp,
                lowest_new_price_raw=lowest_new_raw,
                lowest_new_price_gbp=lowest_new_gbp,
                price_source=price_source,
                ok=ok,
                error=None if ok else "missing-title",
            )
            conn.commit()

            results.append(
                {
                    "asin": item.asin,
                    "label": item.label,
                    "title": title or item.label,
                    "price_gbp": price_gbp,
                    "price_raw": price_raw,
                    "price_source": price_source,
                    "url": url or f"https://www.amazon.co.uk/dp/{item.asin}",
                    "ccc": f"https://uk.camelcamelcamel.com/product/{item.asin}",
                    "buybox_gbp": buybox_gbp,
                    "lowest_new_gbp": lowest_new_gbp,
                }
            )
        except Exception as e:
            store_check(
                conn,
                run_id=run_id,
                ts=ts,
                asin=item.asin,
                label=item.label,
                title=None,
                url=f"https://www.amazon.co.uk/dp/{item.asin}",
                price_raw=None,
                price_gbp=None,
                buybox_price_raw=None,
                buybox_price_gbp=None,
                lowest_new_price_raw=None,
                lowest_new_price_gbp=None,
                price_source="error",
                ok=False,
                error=str(e)[:300],
            )
            conn.commit()
            results.append(
                {
                    "asin": item.asin,
                    "label": item.label,
                    "title": item.label,
                    "price_gbp": None,
                    "price_raw": None,
                    "price_source": "error",
                    "url": f"https://www.amazon.co.uk/dp/{item.asin}",
                    "ccc": f"https://uk.camelcamelcamel.com/product/{item.asin}",
                    "error": str(e)[:140],
                }
            )
        finally:
            try:
                if 'target_id' in locals() and target_id:
                    openclaw_browser_close(target_id)
            except Exception:
                pass

    # Best deal of this run
    priced = [r for r in results if r.get("price_gbp") is not None]
    priced.sort(key=lambda r: r["price_gbp"])
    best = priced[0] if priced else None

    lines: list[str] = []
    lines.append(f"{watch_name} — {today}")

    if best:
        lines.append(f"Best right now (lowest NEW offer): {best['label']} — {fmt_money(best['price_gbp'])}")
        lines.append(best["url"])
        lines.append(best["ccc"])
    else:
        lines.append("ERROR: No prices found (possible captcha / layout change).")

    lines.append("")
    lines.append("Current prices:")
    for r in priced[:10]:
        lines.append(f"- {r['label']}: {fmt_money(r['price_gbp'])}")

    # History per item (daily min)
    lines.append("")
    lines.append(f"History (daily min, last {args.history_days} days):")
    for r in results:
        hist = daily_min_prices(conn, r["asin"], limit_days=args.history_days)
        if not hist:
            lines.append(f"- {r['label']}: (no history yet)")
            continue
        hist_str = ", ".join([f"{day} {fmt_money(p)}" for day, p in reversed(hist)])
        # reversed => oldest->newest for readability
        lines.append(f"- {r['label']}: {hist_str}")

    lines.append("")
    lines.append(f"DB: {args.db}")

    msg = "\n".join(lines).strip()
    if args.dry_run:
        print(msg)
    else:
        send_message(args.channel, args.target, msg)


if __name__ == "__main__":
    main()
