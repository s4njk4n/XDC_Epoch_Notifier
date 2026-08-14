#!/bin/bash
set -euo pipefail

# =============================================
# XDC Epoch Rewards Notifier — DEBUG VERSION
# =============================================

while IFS=',' read -r address ntfy_topic api_key || [ -n "$address" ]; do
  [[ -z "$address" || "$address" == "address"* ]] && continue

  address=$(echo "$address" | xargs | tr '[:upper:]' '[:lower:]')
  ntfy_topic=$(echo "$ntfy_topic" | xargs)

  [[ -z "$ntfy_topic" ]] && continue

  echo "DEBUG: last_balances.json exists = $([[ -f last_balances.json ]] && echo YES || echo NO)"

  previous_wei=0
  if [[ -f last_balances.json ]]; then
    previous_wei=$(jq -r --arg a "$address" '.[$a] // "0"' last_balances.json 2>/dev/null || echo 0)
  fi
  echo "DEBUG: previous_wei = $previous_wei"

  current_wei=$(curl -s -m 10 -X POST -H "Content-Type: application/json" \
    --data '{"jsonrpc":"2.0","method":"eth_getBalance","params":["'"$address"'","latest"],"id":1}' \
    "https://erpc.xinfin.network" | jq -r '.result // "0x0"' | python3 -c "print(int(input(), 16))" 2>/dev/null || echo 0)
  echo "DEBUG: current_wei = $current_wei"

  rewards_wei=$(echo "$current_wei - $previous_wei" | bc 2>/dev/null || echo 0)
  [[ "$rewards_wei" -lt 0 ]] && rewards_wei=0
  rewards_xdc=$(echo "scale=4; $rewards_wei / 1000000000000000000" | bc -l 2>/dev/null || echo "0.0000")

  echo "   → $rewards_xdc XDC received in last 24h"

  # Save for tomorrow
  jq --arg a "$address" --arg v "$current_wei" '. + {($a): $v}' last_balances.json 2>/dev/null > /tmp/last_balances.json || echo "{\"$address\": \"$current_wei\"}" > /tmp/last_balances.json
  mv /tmp/last_balances.json last_balances.json

  # ALWAYS send notification
  today=$(date -u +"%Y-%m-%d")
  message="🪙 XDC Masternode Daily Rewards Check

Date: ${today} (08:00 AEST)
Address: ${address}
Epoch rewards received: ${rewards_xdc} XDC

✅ Daily monitoring check completed successfully"

  curl -s -m 10 \
    -H "Title: XDC Daily Rewards" \
    -H "Tags: moneybag,clock" \
    -H "Priority: high" \
    -d "$message" \
    "https://ntfy.sh/${ntfy_topic}"

  echo "   ✅ Notification sent to ${ntfy_topic} (rewards: ${rewards_xdc} XDC)"

done <<< "${REWARDS_CSV}"
