"""One-off: merge Typesense keys into classeasily/prod/env (does not print secret contents)."""
import json
import secrets
import string

import boto3

REGION = "us-east-2"
SECRET_ID = "classeasily/prod/env"


def main() -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    r = sm.get_secret_value(SecretId=SECRET_ID)
    d = json.loads(r["SecretString"])
    alphabet = string.ascii_letters + string.digits
    if not (d.get("TYPESENSE_API_KEY") or "").strip():
        d["TYPESENSE_API_KEY"] = "".join(secrets.choice(alphabet) for _ in range(48))
    d["TYPESENSE_HOST"] = "typesense.classeasily.local"
    d["TYPESENSE_PORT"] = "8108"
    d["TYPESENSE_PROTOCOL"] = "http"
    d["TYPESENSE_COLLECTION_ALIAS"] = "classes_live"
    d["TYPESENSE_AUTO_BOOTSTRAP"] = "true"
    d["TYPESENSE_FULL_REINDEX_EACH_DEPLOY"] = "true"
    sm.put_secret_value(SecretId=SECRET_ID, SecretString=json.dumps(d, sort_keys=True))
    print("Updated", SECRET_ID, "(TYPESENSE_* keys merged; API key generated only if missing)")


if __name__ == "__main__":
    main()
