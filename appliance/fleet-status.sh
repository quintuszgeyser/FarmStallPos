#!/bin/bash
# fleet-status.sh — show version + backup freshness across the fleet.
#
#   ./fleet-status.sh            # read stores-inventory.csv, SSH each box, print a table
#
# Reads stores-inventory.csv (store_id,ssh_host,wave,expected_version). For each store it
# SSHes (via Tailscale) and reads the live /__version and the last backup heartbeat, then
# prints expected-vs-actual so you can spot drift before/after a rollout. Read-only.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INV="${1:-$HERE/stores-inventory.csv}"
[ -f "$INV" ] || { echo "no inventory at $INV (see stores-inventory.csv.example)"; exit 1; }

printf '%-22s %-16s %-10s %-10s %-22s\n' STORE HOST EXPECTED ACTUAL LAST_BACKUP
printf '%.0s-' {1..82}; echo
tail -n +2 "$INV" | while IFS=, read -r sid host wave expected _; do
  [ -z "$sid" ] && continue
  actual="$(ssh -o ConnectTimeout=6 -o BatchMode=yes "$host" \
            "curl -s http://localhost:5000/__version 2>/dev/null" 2>/dev/null || echo "OFFLINE")"
  bk="$(ssh -o ConnectTimeout=6 -o BatchMode=yes "$host" \
        "cat /opt/farmpos/data/backups/.heartbeat 2>/dev/null | cut -d' ' -f1" 2>/dev/null || echo "?")"
  flag=""; [ "$actual" != "$expected" ] && [ "$actual" != "OFFLINE" ] && flag=" <-DRIFT"
  printf '%-22s %-16s %-10s %-10s %-22s%s\n' "$sid" "$host" "$expected" "$actual" "$bk" "$flag"
done
