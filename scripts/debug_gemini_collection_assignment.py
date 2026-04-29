#!/usr/bin/env python
"""
Standalone debug runner for collection auto-assignment Gemini calls.

This mirrors the logic in quickstart.utils.services.CollectionAutoAssigner
but adds verbose diagnostics for 429/RESOURCE_EXHAUSTED responses.
"""
import argparse
import json
import os
import sys
from typing import Any

import django
import httpx
from django.conf import settings
from django.db import transaction

from google import genai
from google.genai import errors as genai_errors
from google.genai import types


def _setup_django() -> None:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "CEBackend.settings")
    django.setup()


def _extract_retry_delay(details: dict[str, Any]) -> str | None:
    for item in details.get("error", {}).get("details", []):
        if item.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
            return item.get("retryDelay")
    return None


def _extract_quota_violations(details: dict[str, Any]) -> list[dict[str, Any]]:
    for item in details.get("error", {}).get("details", []):
        if item.get("@type") == "type.googleapis.com/google.rpc.QuotaFailure":
            return item.get("violations", [])
    return []


def _build_prompt(class_obj: Any, collections_list: list[dict[str, Any]]) -> str:
    return f"""
            Act as a content curator for a class / experience booking platform.
            Your goal is to categorize the class below into specific collections.

            --- CLASS PROFILE ---
            Title: {class_obj.title}
            Description: {class_obj.description}

            --- CANDIDATE COLLECTIONS ---
            {json.dumps(collections_list, indent=2)}

            --- YOUR TASK ---
            1. Analyze the Class Profile against the criteria for EACH Candidate Collection.
            2. Look for specific keywords (e.g., "couples", "wine" for Date Night; "kids", "family" for Family Friendly).
            3. Briefly justify your decision for each match.

            --- OUTPUT FORMAT ---
            Return a single JSON object. Do not use Markdown formatting.
            {{
                "thought_process": {{
                    "Collection Name 1": "Reason why it fits or does not fit...",
                    "Collection Name 2": "Reason why it fits or does not fit..."
                }},
                "matched_ids": [12, 45]
            }}
            """


def _parse_matched_ids(raw_text: str) -> list[int]:
    from quickstart.utils.services import CollectionAutoAssigner

    return CollectionAutoAssigner()._parse_llm_json_response(raw_text)


def _raw_http_debug_call(api_key: str, prompt: str, model_name: str) -> None:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    print("\n[DEBUG] Sending raw HTTP request for full error payload...")
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, headers=headers, json=payload)

    print(f"[DEBUG] raw_status={response.status_code}")
    print("[DEBUG] raw_headers_subset:")
    interesting_headers = [
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-request-id",
    ]
    for header_name in interesting_headers:
        value = response.headers.get(header_name)
        if value:
            print(f"- {header_name}: {value}")

    try:
        body = response.json()
        print("[DEBUG] raw_json_body:")
        print(json.dumps(body, indent=2))
    except Exception:
        print("[DEBUG] raw_text_body:")
        print(response.text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Debug Gemini collection auto-assignment with full 429 diagnostics."
    )
    parser.add_argument("--class-id", type=int, required=True, help="ClassesMain.classId")
    parser.add_argument(
        "--apply-db-updates",
        action="store_true",
        help="Actually remove/add automated collections on the class (default: no writes).",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="Print the full prompt sent to Gemini.",
    )
    parser.add_argument(
        "--skip-raw-http-debug",
        action="store_true",
        help="Skip fallback raw HTTP call on ClientError.",
    )
    parser.add_argument(
        "--models",
        type=str,
        default="gemini-2.5-flash,gemini-2.0-flash",
        help="Comma-separated model preference order.",
    )
    args = parser.parse_args()

    _setup_django()

    from quickstart.models import ClassCollection, ClassesMain

    class_obj = ClassesMain.objects.filter(classId=args.class_id).first()
    if not class_obj:
        print(f"[ERROR] Class not found: classId={args.class_id}")
        return 1

    automated_collections = (
        ClassCollection.objects.filter(type="automated", is_active=True)
        .exclude(automation_rules__exact={})
        .order_by("sort_order")
    )
    collections_info = []
    for col in automated_collections:
        if "ai_criteria" in col.automation_rules:
            collections_info.append(
                {
                    "id": col.id,
                    "name": col.name,
                    "criteria": col.automation_rules.get("ai_criteria"),
                }
            )

    print(f"[INFO] classId={class_obj.classId} title={class_obj.title!r} status={class_obj.status}")
    print(f"[INFO] automated_collections={automated_collections.count()} ai_candidates={len(collections_info)}")

    if not collections_info:
        print("[INFO] No AI-criteria collections found. Nothing to send to Gemini.")
        return 0

    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        print("[ERROR] GEMINI_API_KEY missing in Django settings.")
        return 1

    prompt = _build_prompt(class_obj, collections_info)
    if args.print_prompt:
        print("\n=== PROMPT START ===")
        print(prompt)
        print("=== PROMPT END ===\n")

    client = genai.Client(api_key=api_key)
    model_candidates = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"[INFO] Model candidates: {model_candidates}")

    response = None
    last_error = None
    for idx, model_name in enumerate(model_candidates, start=1):
        print(f"[INFO] Calling Gemini model {model_name} ({idx}/{len(model_candidates)}) ...")
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            break
        except genai_errors.ClientError as exc:
            last_error = exc
            print(f"\n[ERROR] Gemini ClientError on model {model_name}")
            print(f"code={getattr(exc, 'code', None)}")
            print(f"message={str(exc)}")

            details = getattr(exc, "details", None)
            if isinstance(details, dict):
                print("\n[ERROR] details JSON:")
                print(json.dumps(details, indent=2))

                retry_delay = _extract_retry_delay(details)
                if retry_delay:
                    print(f"\n[ERROR] retryDelay from API: {retry_delay}")

                violations = _extract_quota_violations(details)
                if violations:
                    print("\n[ERROR] Quota violations:")
                    for violation in violations:
                        metric = violation.get("quotaMetric")
                        quota_id = violation.get("quotaId")
                        quota_value = violation.get("quotaValue")
                        dims = violation.get("quotaDimensions")
                        print(
                            f"- metric={metric} quotaId={quota_id} "
                            f"quotaValue={quota_value} dimensions={dims}"
                        )
            else:
                print(f"[ERROR] details={details!r}")
            if not args.skip_raw_http_debug:
                _raw_http_debug_call(api_key=api_key, prompt=prompt, model_name=model_name)
        except Exception as exc:
            print(f"\n[ERROR] Unexpected exception from Gemini call on model {model_name}")
            print(repr(exc))
            return 3

    if response is None:
        print("\n[ERROR] All model attempts failed.")
        if last_error:
            print(f"[ERROR] Last error: {last_error}")
        return 2

    raw = response.text or ""
    print("\n[INFO] Raw Gemini response:")
    print(raw if raw else "<EMPTY RESPONSE>")

    matched_ids = _parse_matched_ids(raw) if raw else []
    print(f"\n[INFO] Parsed matched_ids={matched_ids}")

    local_matches = list(
        automated_collections.filter(id__in=matched_ids).values_list("id", "name")
    )
    print(
        "[INFO] Local matched collections="
        + json.dumps(local_matches, ensure_ascii=True)
    )

    if args.apply_db_updates:
        print("[INFO] Applying DB updates (remove old automated, add matched)...")
        with transaction.atomic():
            class_obj.collections.remove(*automated_collections)
            if local_matches:
                match_objs = automated_collections.filter(id__in=matched_ids)
                class_obj.collections.add(*match_objs)
        print("[INFO] DB updates applied.")
    else:
        print("[INFO] Dry run complete. No DB writes were made.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
