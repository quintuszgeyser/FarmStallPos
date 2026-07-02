#!/bin/bash
# common.sh — shared helpers for the appliance scripts. Source it: . lib/common.sh

FARMPOS_HOME="${FARMPOS_HOME:-/opt/farmpos}"
SECRETS_DIR="$FARMPOS_HOME/secrets"

c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_bold()  { printf '\033[1m%s\033[0m\n'  "$*"; }
die()     { c_red "ERROR: $*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# Minimal YAML scalar reader (flat keys + one level of nesting via dotted path).
# Good enough for store.yml; avoids a hard dependency on yq.
#   yaml_get store.yml scale.ip
yaml_get() {
  local file="$1" path="$2"
  python3 - "$file" "$path" <<'PY'
import sys, yaml
f, path = sys.argv[1], sys.argv[2]
with open(f) as fh:
    data = yaml.safe_load(fh) or {}
cur = data
for part in path.split('.'):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        cur = None
        break
print('' if cur is None else cur)
PY
}

# URL-encode a Postgres password for the DATABASE_URL (handles @ : / ! etc.)
urlencode() {
  python3 - "$1" <<'PY'
import sys, urllib.parse
print(urllib.parse.quote(sys.argv[1], safe=''))
PY
}

gen_secret() { openssl rand -base64 32 | tr -d '\n/+=' | cut -c1-40; }

# Read a store.yml value with a fallback if empty/missing.
yaml_or() { local v; v="$(yaml_get "$1" "$2")"; [ -n "$v" ] && printf '%s' "$v" || printf '%s' "$3"; }
