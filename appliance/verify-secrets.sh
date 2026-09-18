#!/bin/bash
# verify-secrets.sh - verify every fleet box has a unique, non-default SECRET_KEY.
#
#   ./verify-secrets.sh            # read stores-inventory.csv, SSH each box, print a table
#
# Rev 5 P1-2 gate: the STORE_ID-gated boot guard in app.py (create_app(), ~line
# 2431) can only be safely made unconditional once every provisioned box is
# confirmed to have a real, unique SECRET_KEY. This script is that
# evidence-gathering step and must report clean for the whole fleet before the
# STORE_ID gate is removed.
#
# It NEVER transmits, prints, or logs a raw secret value. The SHA-256 hash is
# computed on the REMOTE box, inside the ssh command - only the 12-char
# truncated hex fingerprint crosses the network, purely so a human can eyeball
# presence/uniqueness without the key itself ever leaving the box.
#
# Exit non-zero if any box: is unreachable, has no SECRET_KEY set, has a
# SECRET_KEY matching the known dev fallback ('dev-secret-key'), or has a
# fingerprint colliding with another box (two boxes sharing a signing key is a
# cross-store session-forgery risk).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INV="${1:-$HERE/stores-inventory.csv}"
[ -f "$INV" ] || { echo "no inventory at $INV (see stores-inventory.csv.example)"; exit 1; }

DEV_FALLBACK_HASH="$(printf '%s' 'dev-secret-key' | sha256sum | cut -c1-12)"

printf '%-22s %-9s %-14s %-28s\n' STORE PRESENT FINGERPRINT FLAG
printf '%.0s-' {1..75}; echo

declare -A seen_fp
fail=0

while IFS=, read -r sid host wave expected _; do
  [ -z "$sid" ] && continue

  remote_out="$(ssh -o ConnectTimeout=6 -o BatchMode=yes "$host" '
    val="$(grep -oP "^SECRET_KEY=\K.*" /opt/farmpos/.env 2>/dev/null)"
    if [ -z "$val" ]; then
      echo MISSING
    else
      printf "%s" "$val" | sha256sum | cut -c1-12
    fi
  ' 2>/dev/null)"

  if [ -z "$remote_out" ]; then
    printf '%-22s %-9s %-14s %-28s\n' "$sid" "NO" "-" "UNREACHABLE"
    fail=1
    continue
  fi
  if [ "$remote_out" = "MISSING" ]; then
    printf '%-22s %-9s %-14s %-28s\n' "$sid" "NO" "-" "SECRET_KEY NOT SET"
    fail=1
    continue
  fi

  flag="-"
  if [ "$remote_out" = "$DEV_FALLBACK_HASH" ]; then
    flag="DEV FALLBACK IN USE"
    fail=1
  elif [ -n "${seen_fp[$remote_out]:-}" ]; then
    flag="DUPLICATE (matches ${seen_fp[$remote_out]})"
    fail=1
  fi
  seen_fp[$remote_out]="$sid"

  printf '%-22s %-9s %-14s %-28s\n' "$sid" "YES" "$remote_out" "$flag"
done < <(tail -n +2 "$INV")

echo
if [ "$fail" = "1" ]; then
  echo "FAIL - one or more boxes missing a real, unique SECRET_KEY."
  echo "Do NOT remove the STORE_ID gate in app.py until this reports clean for every box."
  exit 1
fi
echo "PASS - every box has a present, non-default, unique SECRET_KEY."
