#!/bin/bash
# Nightly logical dump of the on-box Postgres to S3. No secret values printed.
set -euo pipefail

STAMP="$(date -u +%Y-%m-%d)"
BUCKET="${BACKUP_BUCKET:-classeasily-prod-bucket}"
KEY="backups/postgres/classeasily-prod-${STAMP}.dump"
COMPOSE_DIR="${COMPOSE_DIR:-/home/ec2-user/single-box}"

python3 - <<PY
from pathlib import Path
vals = {}
for line in Path("/home/ec2-user/.env.prod").read_text().splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        vals[k] = v
Path("/tmp/pgpass.env").write_text("PGPASSWORD=" + vals["DB_PASSWORD"] + "\n")
Path("/tmp/pgpass.env").chmod(0o600)
Path("/tmp/dbmeta").write_text(vals["DB_USER"] + "\n" + vals["DB_NAME"] + "\n")
PY

USER="$(sed -n '1p' /tmp/dbmeta)"
DB="$(sed -n '2p' /tmp/dbmeta)"
DUMP_DIR="/home/ec2-user/dumps"
mkdir -p "$DUMP_DIR"
DUMP="${DUMP_DIR}/classeasily-prod-${STAMP}.dump"

docker run --rm --network classeasily-prod-net --env-file /tmp/pgpass.env \
  -v "${DUMP_DIR}:/dumps" \
  -u "$(id -u):$(id -g)" \
  postgis/postgis:16-3.4 \
  pg_dump -h postgres -U "$USER" -d "$DB" -Fc --no-owner --no-privileges -f "/dumps/classeasily-prod-${STAMP}.dump"

aws s3 cp "$DUMP" "s3://${BUCKET}/${KEY}" --region us-east-2 --only-show-errors
rm -f "$DUMP"
echo "uploaded s3://${BUCKET}/${KEY}"
