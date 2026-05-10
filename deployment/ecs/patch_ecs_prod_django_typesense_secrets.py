"""
Register a new ECS task definition revision with TYPESENSE_* secrets from classeasily/prod/env.

Usage (from repo root, with AWS creds):
  python deployment/ecs/patch_ecs_prod_django_typesense_secrets.py web
  python deployment/ecs/patch_ecs_prod_django_typesense_secrets.py worker
"""
from __future__ import annotations

import json
import sys

import boto3

REGION = "us-east-2"
CLUSTER = "classeasily-prod-cluster"
# ASM JSON secret; suffix matches existing task definition valueFrom ARNs.
ENV_SECRET_ARN_PREFIX = (
    "arn:aws:secretsmanager:us-east-2:459929160817:secret:classeasily/prod/env-BsWyGd"
)

TASK_FAMILIES = {
    "web": "classeasily-prod-backend-web-task",
    "worker": "classeasily-prod-backend-task-worker",
}

TYPESENSE_KEYS = [
    "TYPESENSE_HOST",
    "TYPESENSE_PORT",
    "TYPESENSE_PROTOCOL",
    "TYPESENSE_API_KEY",
    "TYPESENSE_COLLECTION_ALIAS",
    "TYPESENSE_AUTO_BOOTSTRAP",
    "TYPESENSE_FULL_REINDEX_EACH_DEPLOY",
]


def _strip_register_noise(td: dict) -> dict:
    for k in (
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
    ):
        td.pop(k, None)
    return td


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in TASK_FAMILIES:
        print("Usage: patch_ecs_prod_django_typesense_secrets.py web|worker", file=sys.stderr)
        sys.exit(2)
    which = sys.argv[1]
    family = TASK_FAMILIES[which]

    ecs = boto3.client("ecs", region_name=REGION)
    r = ecs.describe_task_definition(taskDefinition=family)
    td = r["taskDefinition"]
    _strip_register_noise(td)

    c0 = td["containerDefinitions"][0]
    secrets = list(c0.get("secrets") or [])
    by_name = {s["name"]: s for s in secrets}
    for name in TYPESENSE_KEYS:
        by_name[name] = {
            "name": name,
            "valueFrom": f"{ENV_SECRET_ARN_PREFIX}:{name}::",
        }
    c0["secrets"] = sorted(by_name.values(), key=lambda x: x["name"])

    out = ecs.register_task_definition(**td)
    arn = out["taskDefinition"]["taskDefinitionArn"]
    print("Registered", arn)

    svc = (
        "classeasily-prod-web-service"
        if which == "web"
        else "classeasily-prod-worker-service"
    )
    ecs.update_service(
        cluster=CLUSTER,
        service=svc,
        taskDefinition=arn,
        forceNewDeployment=True,
    )
    print("Updated service", svc, "to new revision")


if __name__ == "__main__":
    main()
