#!/usr/bin/env bash
set -euo pipefail

ASINS_FILE="/home/tnu/clawd/config/ssd4tb_asins.txt"
STATE_JSON="/home/tnu/clawd/memory/ssd-4tb-daily.json"
LOCKFILE="/tmp/ssd4tb_check.lock"

TG_TARGET="476265210"
TG_CHANNEL="telegram"

rand_sleep() {
  # random sleep 2-6 seconds
  python3 - <<'PY'
import random, time
s=random.uniform(2.0,6.0)
time.sleep(s)
PY
}

# prevent overlap
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  exit 0
fi

if [[ ! -f "$ASINS_FILE" ]]; then
  echo "Missing ASIN list: $ASINS_FILE" >&2
  exit 1
fi

# Ensure browser is running (no-op if already running)
openclaw browser start >/dev/null

items_json='[]'

while IFS= read -r line; do
  line="${line%%#*}"
  line="$(echo "$line" | xargs || true)"
  [[ -z "$line" ]] && continue
  asin="$line"

  # open product page
  tab_json=$(openclaw browser open --json --expect-final --timeout 60000 "https://www.amazon.co.uk/dp/${asin}")
  target_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("targetId",""))' <<<"$tab_json")

  # small delay like a user
  rand_sleep

  # extract title + price
  data=$(openclaw browser evaluate --json --expect-final --timeout 60000 --target-id "$target_id" --fn '() => ({
    title: (document.getElementById("productTitle")?.innerText||"").trim(),
    price: document.querySelector("#corePriceDisplay_desktop_feature_div .a-price .a-offscreen")?.innerText
      || document.querySelector("#corePriceDisplay_desktop_feature_div .a-offscreen")?.innerText
      || document.querySelector(".a-price .a-offscreen")?.innerText
      || null,
    url: location.href
  })')

  # close tab
  openclaw browser close "$target_id" >/dev/null 2>&1 || true

  # append to items
  items_json=$(python3 -c 'import json,sys; items=json.loads(sys.argv[1]); obj=json.load(sys.stdin); obj=obj.get("result", obj); obj["asin"]=sys.argv[2]; items.append(obj); print(json.dumps(items))' "$items_json" "$asin" <<<"$data")

  rand_sleep

done < "$ASINS_FILE"

# Filter to 4TB NVMe-ish by title
filtered=$(python3 -c 'import json,re,sys; items=json.loads(sys.stdin.read()); out=[]
for it in items:
  t=(it.get("title") or "").strip()
  if not t: continue
  if not re.search(r"\b4\s*TB\b", t, re.I): continue
  if not re.search(r"(nvme|m\.2|pcie)", t, re.I): continue
  if re.search(r"(enclosure|heatsink|case|adapter|kit|external)", t, re.I): continue
  out.append(it)
print(json.dumps(out))' <<<"$items_json")

# Format message and persist state
msg=$(python3 /home/tnu/clawd/scripts/ssd4tb_check.py <<<"$filtered")

# Send to Telegram
action_out=$(openclaw message send --channel "$TG_CHANNEL" --target "$TG_TARGET" --message "$msg" 2>&1) || {
  echo "Failed to send message: $action_out" >&2
  exit 1
}

echo "$msg"
