import logging
import json
import re
from django.conf import settings
from quickstart.models import ClassCollection
from quickstart.utils.gemini_retry import generate_content_with_retry

# NEW SDK IMPORTS
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

class CollectionAutoAssigner:
    def process_class(self, class_instance):
        """
        Sends class details + all automation rules to Google Gemini 2.0
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
        Calls Google Gemini 2.0 Flash using the new `google.genai` SDK.
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

            response = generate_content_with_retry(
                client,
                model='gemini-2.0-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json'
                )
            )

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