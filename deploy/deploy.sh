#!/usr/bin/env bash
# Deploy the API with Docker on EC2.
#   ./deploy/deploy.sh          interactive (asks before removing the old API)
#   ./deploy/deploy.sh --yes    no questions
# Run it from the project folder (the one with the Dockerfile and .env). Safe to run again.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

YES=0; [[ "${1:-}" == "--yes" ]] && YES=1
say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { [[ $YES -eq 1 ]] && return 0; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy]$ ]]; }
IMG="bcacctvwala-api:latest"
NAME="bcacctvwala-api"
NET="bcnet"
SUBNET="172.28.0.0/24"   # fixed on purpose: nginx on the host reaches the container from this network's gateway (172.28.0.1), so TRUSTED_PROXIES in .env must cover it
# One-off container from the image (migrations, pg_dump, ...). Extra args go before the image name's command.
run_once() { docker run --rm -i --env-file .env -e PORT=4000 "$@"; }
TS=$(date +%Y%m%d-%H%M%S)

# ---------------------------------------------------------------- 1. preflight
say "1/7 Preflight"
command -v docker >/dev/null || die "Docker is not installed (see the install steps)."
[[ -f .env ]] || die ".env not found. Copy .env.production.example to .env and fill it in."
chmod 600 .env
for k in DB_HOST DB_NAME DB_USER DB_PASSWORD JWT_SECRET R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET; do
  v=$(grep -E "^${k}=" .env | head -1 | cut -d= -f2- || true)
  [[ -n "$v" ]] || die "$k is empty in .env"
done
grep -qE '^DB_HOST=.*(your-db|your-rds)' .env && die "DB_HOST is still the placeholder."
# Server clock in IST (v1.34.0): the host's own date / logs / the pre-deploy file name below. The container sets its own TZ.
if command -v timedatectl >/dev/null 2>&1 && [[ "$(timedatectl show -p Timezone --value 2>/dev/null)" != "Asia/Kolkata" ]]; then
  sudo timedatectl set-timezone Asia/Kolkata && echo "server time zone set to Asia/Kolkata" \
    || warn "Could not set the server time zone. Run: sudo timedatectl set-timezone Asia/Kolkata"
fi
echo "ok"

# ---------------------------------------------------------------- 2. build (old API keeps running meanwhile)
say "2/7 Building the image (the old API is still running)"
PG_MAJOR="${PG_MAJOR:-$(grep -E '^PG_MAJOR=' .env | head -1 | cut -d= -f2- || true)}"
docker build --build-arg PG_MAJOR="${PG_MAJOR:-17}" -t "$IMG" .

# ---------------------------------------------------------------- 3. database safety copy
say "3/7 Safety copy of the database (pg_dump)"
mkdir -p "$HOME/db-backups"; chmod 700 "$HOME/db-backups"
OUT="$HOME/db-backups/pre-deploy-$TS.dump"
run_once --entrypoint sh "$IMG" -c \
  'PGPASSWORD="$DB_PASSWORD" PGSSLMODE=verify-full PGSSLROOTCERT=/etc/ssl/certs/rds-global-bundle.pem \
   pg_dump -h "$DB_HOST" -p "${DB_PORT:-5432}" -U "$DB_USER" -d "$DB_NAME" --format=custom --schema=public --no-owner --no-privileges' \
  > "$OUT" || { rm -f "$OUT"; die "pg_dump failed. Check DB_HOST/credentials, the RDS security group (port 5432 from this EC2) and PG_MAJOR (must be >= the RDS major version)."; }
chmod 600 "$OUT"
[[ -s "$OUT" ]] || die "The backup file is empty."
echo "saved $OUT ($(du -h "$OUT" | cut -f1)). It contains password hashes: keep it private."

# ---------------------------------------------------------------- 4. remove the current API
say "4/7 Removing the currently running API"
FOUND=0
# systemd services (the README names it gst-api)
for u in gst-api bcacctvwala-api bcacctvwala; do
  if systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^${u}\.service"; then
    FOUND=1; warn "systemd service '$u' found"
    if ask "Stop, disable and remove service $u?"; then
      sudo systemctl stop "$u" || true
      sudo systemctl disable "$u" || true
      f=$(systemctl show -p FragmentPath --value "$u" 2>/dev/null || true)
      if [[ -n "$f" && -f "$f" ]]; then sudo mkdir -p /root/old-api-archive; sudo mv "$f" "/root/old-api-archive/${u}.service.$TS"; fi
      sudo systemctl daemon-reload; sudo systemctl reset-failed "$u" 2>/dev/null || true
    fi
  fi
done
# pm2
if command -v pm2 >/dev/null 2>&1; then
  for n in $(pm2 jlist 2>/dev/null | python3 -c 'import sys,json
try:
  [print(p["name"]) for p in json.load(sys.stdin) if any(s in p["name"].lower() for s in ("gst","bcacctv","api"))]
except Exception: pass' 2>/dev/null); do
    FOUND=1; warn "pm2 process '$n' found"
    ask "Delete pm2 process $n?" && { pm2 delete "$n"; pm2 save >/dev/null; }
  done
fi
# the previous run of this same container (replaced on every deploy)
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  FOUND=1; echo "replacing the existing container '$NAME'"; docker rm -f "$NAME" >/dev/null
fi
# network left behind by the old docker-compose setup (it uses the same subnet and would block creating $NET)
docker network inspect bcacctvwala_bcnet >/dev/null 2>&1 && { docker network rm bcacctvwala_bcnet >/dev/null && echo "removed the old compose network"; } || true
# other containers that publish port 4000
for c in $(docker ps -a --format '{{.Names}}|{{.Ports}}' | grep -E ':4000->' | cut -d'|' -f1 | grep -vx "$NAME" || true); do
  FOUND=1; warn "container '$c' publishes port 4000"
  ask "Stop and remove container $c?" && docker rm -f "$c"
done
[[ $FOUND -eq 0 ]] && echo "no old API found by name"
# whatever still holds port 4000?
if ss -ltnH 'sport = :4000' | grep -q .; then
  ss -ltnp 'sport = :4000' || true
  die "Something is still listening on port 4000 (above). Stop that process (kill <pid>) and run this script again."
fi
echo "port 4000 is free"

# ---------------------------------------------------------------- 5. migrations
say "5/7 Database migrations (alembic)"
PSQL='PGPASSWORD="$DB_PASSWORD" PGSSLMODE=verify-full PGSSLROOTCERT=/etc/ssl/certs/rds-global-bundle.pem psql -h "$DB_HOST" -p "${DB_PORT:-5432}" -U "$DB_USER" -d "$DB_NAME" -tA -c'
q() { run_once --entrypoint sh "$IMG" -c "$PSQL \"$1\"" | tr -d '[:space:]'; }
HAS_TABLES=$(q "select to_regclass('public.invoices') is not null")
HAS_ALEMBIC=$(q "select to_regclass('public.alembic_version') is not null")
if [[ "$HAS_TABLES" == "t" && "$HAS_ALEMBIC" != "t" ]]; then
  warn "The database has tables but alembic has no record of them (built from schema.sql). Working out which version it is..."
  if   [[ $(q "select exists(select 1 from information_schema.columns where table_name='invoice_items' and column_name='discount_percent')") == t ]]; then REV=0014
  elif [[ $(q "select exists(select 1 from information_schema.columns where table_name='invoice_items' and column_name='hsn_code')") == t ]]; then REV=0013
  elif [[ $(q "select to_regclass('public.expenses') is not null") == t ]]; then REV=0012
  elif [[ $(q "select exists(select 1 from information_schema.columns where table_name='store_profile' and column_name='signature_key')") == t ]]; then REV=0011
  else die "Cannot tell which version this database is. Find the matching revision in alembic/versions, run: docker run --rm --env-file .env $IMG alembic stamp <rev>  and run this script again."; fi
  echo "looks like revision $REV"
  ask "Tell alembic the database is at $REV (alembic stamp $REV)?" || die "Stopped."
  run_once "$IMG" alembic stamp "$REV"
fi
run_once "$IMG" alembic upgrade head
run_once "$IMG" alembic current

# ---------------------------------------------------------------- 6. start
say "6/7 Starting the API"
docker network inspect "$NET" >/dev/null 2>&1 || docker network create --subnet "$SUBNET" "$NET" >/dev/null
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  --env-file .env -e PORT=4000 \
  --network "$NET" \
  -p 127.0.0.1:4000:4000 \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=5 \
  "$IMG" >/dev/null
# 127.0.0.1 only: never publish 4000 on 0.0.0.0, and keep it closed in the security group too.
for i in $(seq 1 30); do
  s=$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo starting)
  [[ "$s" == "healthy" ]] && break
  sleep 3
done
[[ "$s" == "healthy" ]] || { docker logs --tail 60 "$NAME"; die "The API did not become healthy. The log is above."; }
curl -fsS http://127.0.0.1:4000/health; echo
curl -fsS http://127.0.0.1:4000/api/v1/version; echo

# ---------------------------------------------------------------- 7. R2
say "7/7 Checking Cloudflare R2"
docker exec "$NAME" python scripts/r2_check.py || warn "R2 check failed: fix the R2_* values in .env, then run this script again (a plain docker restart does not re-read .env)"

say "Done. Next: nginx + HTTPS (deploy/nginx-bcacctvwala.conf), then the first-run setup if the database is new."
