#!/bin/bash
# deploy_cron.sh — runs every minute via crontab
# Polls QA POS for pending scheduled deploys and executes them.
# Install: crontab -e → add:
#   * * * * * /home/quintusz/farmpos-docker/scripts/deploy_cron.sh >> /home/quintusz/farmpos-docker/logs/deploy_cron.log 2>&1

QA_URL="http://localhost:5100"
DEPLOY_SH="/home/quintusz/farmpos-docker/deploy.sh"
ROLLBACK_SH="/home/quintusz/farmpos-docker/rollback.sh"
LOCKFILE="/tmp/pos_deploy.lock"

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
    exit 0
fi
touch "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

# Poll QA for due schedule
POLL=$(curl -s -X POST "$QA_URL/api/deploy-schedule/poll" 2>/dev/null)
DEPLOY=$(echo "$POLL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('deploy','false'))" 2>/dev/null)

if [ "$DEPLOY" != "True" ] && [ "$DEPLOY" != "true" ]; then
    exit 0
fi

SCHEDULE_ID=$(echo "$POLL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null)
DESCRIPTION=$(echo "$POLL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('description',''))" 2>/dev/null)
ACTION=$(echo "$POLL" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('action','deploy'))" 2>/dev/null)

# Choose script by action: deploy (QA->prod promotion) or rollback (prev image)
if [ "$ACTION" = "rollback" ]; then
    RUN_SH="$ROLLBACK_SH"; RUN_ARG=""
else
    RUN_SH="$DEPLOY_SH"; RUN_ARG="prod"
fi

echo "[$(date)] Starting scheduled $ACTION #$SCHEDULE_ID: $DESCRIPTION"

# Run deploy or rollback
LOG=$(bash "$RUN_SH" $RUN_ARG 2>&1)
EXIT_CODE=$?

SUCCESS="false"
[ $EXIT_CODE -eq 0 ] && SUCCESS="true"

echo "[$(date)] $ACTION #$SCHEDULE_ID finished (exit=$EXIT_CODE)"

# Report result back to QA POS
curl -s -X POST "$QA_URL/api/deploy-schedule/complete" \
    -H "Content-Type: application/json" \
    -d "{\"id\": $SCHEDULE_ID, \"success\": $SUCCESS, \"log\": $(echo "$LOG" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")}" \
    > /dev/null 2>&1

echo "[$(date)] $ACTION #$SCHEDULE_ID complete (success=$SUCCESS)"
