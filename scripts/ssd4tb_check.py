#!/usr/bin/env python3
import json
import os
import re
import sys
from datetime import datetime, timezone

STATE_PATH = "/home/tnu/clawd/memory/ssd-4tb-daily.json"

PRICE_RE = re.compile(r"([0-9]+(?:\.[0-9]{2})?)")


def parse_price_gbp(s: str | None):
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


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(obj):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def money(x: float | None):
    if x is None:
        return "—"
    return f"£{x:.2f}"


def main():
    items = json.load(sys.stdin)
    # normalize
    norm = []
    for it in items:
        asin = it.get("asin")
        title = (it.get("title") or "").strip()
        price_raw = it.get("price")
        price = parse_price_gbp(price_raw)
        url = it.get("url") or (f"https://www.amazon.co.uk/dp/{asin}" if asin else None)
        if not asin:
            continue
        norm.append(
            {
                "asin": asin,
                "title": title,
                "priceGBP": price,
                "priceRaw": price_raw,
                "url": url,
                "ccc": f"https://uk.camelcamelcamel.com/product/{asin}",
            }
        )

    # sort by price (missing prices go last)
    norm.sort(key=lambda x: (x["priceGBP"] is None, x["priceGBP"] or 1e18))

    today = datetime.now(timezone.utc).astimezone().date().isoformat()

    lowest = next((x for x in norm if x["priceGBP"] is not None), None)

    prev = load_state()
    prev_low = None
    if prev and prev.get("date"):
        prev_low = prev.get("lowestPriceGBP")

    # save state
    state = {
        "date": today,
        "lowestPriceGBP": lowest["priceGBP"] if lowest else None,
        "lowestAsin": lowest["asin"] if lowest else None,
        "items": norm,
    }
    save_state(state)

    # build message
    lines = []

    if lowest:
        lines.append(f"Today’s best 4TB NVMe: {lowest['title'][:80]} — {money(lowest['priceGBP'])}")
        lines.append(lowest["url"])
        lines.append(lowest["ccc"])
    else:
        lines.append("ERROR: No priced 4TB NVMe items found.")

    # compare
    if prev_low is None or lowest is None or lowest["priceGBP"] is None:
        lines.append("Cheaper than yesterday? NO (no prior data)")
    else:
        diff = lowest["priceGBP"] - float(prev_low)
        if abs(diff) < 0.005:
            lines.append("Cheaper than yesterday? NO (£0.00)")
        elif diff < 0:
            lines.append(f"Cheaper than yesterday? YES (-{money(-diff)})")
        else:
            lines.append(f"Cheaper than yesterday? NO (+{money(diff)})")

    lines.append("Top deals:")
    for it in norm[:5]:
        p = money(it["priceGBP"])
        t = it["title"][:70] or it["asin"]
        lines.append(f"- {t} — {p}")
        lines.append(f"  {it['url']}")
        lines.append(f"  {it['ccc']}")

    print("\n".join(lines).strip())


if __name__ == "__main__":
    main()
