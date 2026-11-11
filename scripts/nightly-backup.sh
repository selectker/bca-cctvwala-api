#!/usr/bin/env bash
# NOT NEEDED since v1.34.0: the API takes a backup itself every day at BACKUP_SCHEDULE_TIME (00:00 IST by default).
# Keep this only if you set BACKUP_SCHEDULE_ENABLED=false and prefer cron -- running both makes two backups a day.
# Nightly database backup into R2 via the API (POST /api/v1/admin/backup/cloud).
# Needs the platform admin's phone + password in /etc/bcacctvwala/backup.env (chmod 600, owned by root):
#   ADMIN_PHONE=...
#   ADMIN_PASSWORD=...
# Cron (as root):  15 2 * * *  /home/ubuntu/BCAcctvWala-api-python/scripts/nightly-backup.sh >> /var/log/bcacctvwala-backup.log 2>&1
set -Eeuo pipefail
set -a; source /etc/bcacctvwala/backup.env; set +a
BASE="${API_BASE:-http://127.0.0.1:4000/api/v1}"

json() { python3 -c 'import sys,json; print(json.load(sys.stdin)["'"$1"'"])'; }

TOKEN=$(curl -fsS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,os; print(json.dumps({"phone":os.environ["ADMIN_PHONE"],"password":os.environ["ADMIN_PASSWORD"]}))')" | json token)

RESP=$(curl -fsS --max-time 3600 -X POST "$BASE/admin/backup/cloud" -H "Authorization: Bearer $TOKEN") \
  || { curl -fsS -X POST "$BASE/auth/logout" -H "Authorization: Bearer $TOKEN" >/dev/null || true; echo "$(date -Is) BACKUP FAILED"; exit 1; }

curl -fsS -X POST "$BASE/auth/logout" -H "Authorization: Bearer $TOKEN" >/dev/null || true
echo "$(date -Is) backup ok: $RESP"
