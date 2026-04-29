import logging
import json
import re
from django.conf import settings
from quickstart.models import ClassCollection

# NEW SDK IMPORTS
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

logger = logging.getLogger(__name__)

class CollectionAutoAssigner:
    @staticmethod
    def _candidate_models():
        configured_models = getattr(settings, "GEMINI_COLLECTION_MODELS", None)
        if isinstance(configured_models, str) and configured_models.strip():
            models = [m.strip() for m in configured_models.split(",") if m.strip()]
            if models:
                return models

        single_model = (
            getattr(settings, "GEMINI_COLLECTION_MODEL", None)
            or getattr(settings, "GEMINI_MODEL", None)
        )
        if isinstance(single_model, str) and single_model.strip():
            return [single_model.strip()]

        # Prefer 2.5 for stability; fallback to 2.0 for compatibility.
        return ["gemini-2.5-flash", "gemini-2.0-flash"]

    def process_class(self, class_instance):
        """
        Sends class details + all automation rules to Gemini
        to determine collection membership in one go.
        """
        if class_instance.status != 'active':
            return

        # Fetch Automated Collections (order by sort_order for consistency)
        automated_collections = ClassCollection.objects.filter(
            type="automated",
            is_active=True,
        ).exclude(automation_rules__exact={}).order_by("sort_order")

        if not automated_collections.exists():
            return

        # Safety: Clear PREVIOUS automated assignments
        class_instance.collections.remove(*automated_collections)

        # Prepare Payload
        collections_info = []
        for col in automated_collections:
            if "ai_criteria" in col.automation_rules:
                collections_info.append({
                    "id": col.id,
                    "name": col.name,
                    "criteria": col.automation_rules.get("ai_criteria")
                })

        if not collections_info:
            return

        # Call the API
        matched_collection_ids = self._call_llm_curator(class_instance, collections_info)

        # Save Results
        if matched_collection_ids:
            matches = automated_collections.filter(id__in=matched_collection_ids)
            if matches.exists():
                class_instance.collections.add(*matches)
                match_names = list(matches.values_list('name', flat=True))
                # We log the final DB action here
                logger.info(f"✅ ACTION: Assigned '{class_instance.title}' to {match_names}")
            else:
                 logger.info(f"🚫 ACTION: LLM suggested IDs {matched_collection_ids} but no local collections matched.")
        else:
            logger.info(f"🤷 ACTION: No collections assigned for '{class_instance.title}'")

    def _call_llm_curator(self, cls, collections_list):
        """
        Calls Gemini using the `google.genai` SDK.
        """
        try:
            api_key = getattr(settings, "GEMINI_API_KEY", None)
            if not api_key:
                logger.error("GEMINI_API_KEY is missing in settings.")
                return []

            client = genai.Client(api_key=api_key)

            # --- LOG INPUT ---
            logger.info(f"\n🔮 --- ASKING LLM ---")
            logger.info(f"Class: {cls.title}")
            
            prompt = f"""
            Act as a content curator for a class / experience booking platform.
            Your goal is to categorize the class below into specific collections.
            
            --- CLASS PROFILE ---
            Title: {cls.title}
            Description: {cls.description}

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

            response = None
            candidate_models = self._candidate_models()
            for idx, model_name in enumerate(candidate_models, start=1):
                try:
                    logger.info(
                        "Calling Gemini curator model %s (%s/%s)",
                        model_name,
                        idx,
                        len(candidate_models),
                    )
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type='application/json'
                        )
                    )
                    break
                except genai_errors.ClientError as model_error:
                    status_code = getattr(model_error, "code", None)
                    is_retryable = status_code in {404, 429, 500, 502, 503, 504}
                    logger.warning(
                        "Gemini model %s failed with code=%s; retryable=%s; error=%s",
                        model_name,
                        status_code,
                        is_retryable,
                        model_error,
                    )
                    if not is_retryable or idx == len(candidate_models):
                        raise

            if response is None:
                logger.warning(
                    "Gemini returned no response object for class '%s'",
                    cls.title,
                )
                return []

            # --- LOG OUTPUT ---
            if response.text:
                logger.info(f"🤖 LLM RAW RESPONSE: {response.text}")
                matched_ids = self._parse_llm_json_response(response.text)
                return matched_ids

            logger.warning(f"LLM returned empty response for '{cls.title}'")
            return []

        except Exception as e:
            logger.error(f"LLM API failed for class {cls.classId}: {e}")
            return []

    def _parse_llm_json_response(self, raw_text):
        """
        Parse LLM JSON response robustly. Handles invalid JSON such as
        single-quoted keys, trailing commas, or markdown code fences.
        """
        if not raw_text or not raw_text.strip():
            return []

        text = raw_text.strip()

        # Remove markdown code block if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)

        # 1) Try standard json.loads first
        try:
            data = json.loads(text)
            ids = data.get("matched_ids", [])
            return [int(x) for x in ids if isinstance(x, (int, float)) or str(x).isdigit()]
        except json.JSONDecodeError:
            pass

        # 2) Try removing trailing commas (invalid in JSON but sometimes emitted by LLMs)
        try:
            fixed = re.sub(r",\s*([}\]])", r"\1", text)
            data = json.loads(fixed)
            ids = data.get("matched_ids", [])
            return [int(x) for x in ids if isinstance(x, (int, float)) or str(x).isdigit()]
        except (json.JSONDecodeError, TypeError):
            pass

        # 3) Fallback: extract matched_ids array with regex (handles single/double quotes, markdown)
        try:
            for pattern in [
                r'"matched_ids"\s*:\s*\[([^\]]*)\]',
                r"'matched_ids'\s*:\s*\[([^\]]*)\]",
                r"matched_ids\s*:\s*\[([^\]]*)\]",
            ]:
                match = re.search(pattern, text)
                if match:
                    inner = match.group(1)
                    ids = [int(part) for part in re.findall(r"\d+", inner)]
                    return ids
        except (TypeError, ValueError) as e:
            logger.warning(f"Regex fallback failed for LLM response: {e}")

        logger.warning("Could not parse matched_ids from LLM response; returning empty list.")
        return []