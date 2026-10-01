#!/bin/bash
# Fetch classeasily/prod/env and write /home/ec2-user/.env.prod with on-box overrides.
# Does not print secret values.
set -euo pipefail

OUT="${1:-/home/ec2-user/.env.prod}"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

aws secretsmanager get-secret-value \
  --secret-id classeasily/prod/env \
  --region us-east-2 \
  --query SecretString \
  --output text > "$TMP"

python3 - "$TMP" "$OUT" <<'PY'
import json, os, sys
from pathlib import Path
src, dest = sys.argv[1], sys.argv[2]
with open(src) as f:
    data = json.load(f)
if not isinstance(data, dict):
    raise SystemExit("secret is not a JSON object")

# On-box overrides. Keep user/password/name from the secret.
data["DB_HOST"] = "postgres"
data["DB_PORT"] = str(data.get("DB_PORT") or "5432")
data["CACHE_URL"] = "redis://redis:6379/0"
data["TYPESENSE_HOST"] = "typesense"
data["TYPESENSE_PORT"] = str(data.get("TYPESENSE_PORT") or "8108")
data["TYPESENSE_PROTOCOL"] = "http"
data["IS_DOCKER"] = "true"
data["DJANGO_ENV"] = "prod"
data["DEBUG"] = "False"

# Health checks from the host + Caddy.
hosts = [h.strip() for h in str(data.get("ALLOWED_HOSTS", "")).split(",") if h.strip()]
for extra in ("api.classeasily.com", "localhost", "127.0.0.1", "web"):
    if extra not in hosts:
        hosts.append(extra)
data["ALLOWED_HOSTS"] = ",".join(hosts)

lines = []
for k in sorted(data.keys()):
    v = data[k]
    if v is None:
        continue
    s = str(v).replace("\n", " ").replace("\r", "")
    lines.append(f"{k}={s}")
os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
with open(dest, "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(dest, 0o600)
print(f"wrote {dest} ({len(lines)} keys)", file=sys.stderr)

# File-based Postgres creds so Compose cannot interpolate $ in the password.
cred = Path("/home/ec2-user/single-box/pg-creds")
cred.mkdir(mode=0o700, exist_ok=True)
(cred / "user").write_text(data["DB_USER"])
(cred / "password").write_text(data["DB_PASSWORD"])
(cred / "db").write_text(data["DB_NAME"])
for p in cred.iterdir():
    p.chmod(0o600)

def escape(s: str) -> str:
    return str(s).replace("$", "$$")

ts = Path("/home/ec2-user/single-box/typesense.env")
ts.write_text("TYPESENSE_API_KEY=" + escape(data["TYPESENSE_API_KEY"]) + "\n")
ts.chmod(0o600)

compose_env = Path(str(dest) + ".compose")
compose_env.write_text("\n".join(f"{k}={escape(v)}" for k, v in data.items() if v is not None) + "\n")
compose_env.chmod(0o600)
print(f"wrote {compose_env}", file=sys.stderr)
PY
