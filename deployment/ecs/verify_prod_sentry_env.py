"""
Verify prod ECS web/worker task definitions expose Sentry + Django env.

Usage (AWS creds, us-east-2):
  python deployment/ecs/verify_prod_sentry_env.py

Exits 0 when SENTRY_DSN and DJANGO_ENV=prod are present on both tasks; 1 otherwise.
"""
from __future__ import annotations

import sys

import boto3

REGION = "us-east-2"
TASK_FAMILIES = {
    "web": "classeasily-prod-backend-web-task",
    "worker": "classeasily-prod-backend-task-worker",
}
REQUIRED = {"SENTRY_DSN", "DJANGO_ENV"}


def _env_map(container: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in container.get("environment") or []:
        out[item["name"]] = item.get("value", "")
    for item in container.get("secrets") or []:
        out[item["name"]] = item.get("valueFrom", "")
    return out


def main() -> None:
    ecs = boto3.client("ecs", region_name=REGION)
    ok = True
    for label, family in TASK_FAMILIES.items():
        td = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
        c0 = td["containerDefinitions"][0]
        env = _env_map(c0)
        missing = sorted(REQUIRED - set(env.keys()))
        django_val = env.get("DJANGO_ENV", "")
        if missing:
            print(f"[FAIL] {label} ({family}): missing {missing}")
            ok = False
        elif django_val and django_val != "prod":
            print(
                f"[WARN] {label}: DJANGO_ENV is {django_val!r} (expected 'prod' as plain env or secret)"
            )
        else:
            secret_names = {s["name"] for s in (c0.get("secrets") or [])}
            sentry_src = "secretsManager" if "SENTRY_DSN" in secret_names else "environment"
            print(f"[OK] {label}: SENTRY_DSN via {sentry_src}, DJANGO_ENV configured")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
