"""
Phase 7: Parse LLM response for ANSWER and optional WORKING_STATE_PATCH (JSON).
"""
import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Marker the LLM is instructed to output before the JSON patch
PATCH_MARKER = "WORKING_STATE_PATCH:"


def parse_llm_response_for_working_state(response_text: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Parse LLM response that may contain:
      <answer text>
      WORKING_STATE_PATCH: {"investigation": {...}, ...}

    Returns (answer_text, patch_dict or None).
    If WORKING_STATE_PATCH is missing or invalid JSON, patch_dict is None; answer_text is the full response or text before the marker.
    """
    if not response_text or not response_text.strip():
        return (response_text or "", None)

    text = response_text.strip()
    if PATCH_MARKER not in text:
        return (text, None)

    parts = text.split(PATCH_MARKER, 1)
    answer_part = parts[0].strip()
    rest = parts[1].strip()

    # Rest may be JSON only, or JSON followed by more text; take the first JSON object
    patch = _extract_first_json_object(rest)
    if patch is None:
        logger.debug("WORKING_STATE_PATCH marker found but no valid JSON")
        return (text, None)
    return (answer_part, patch)


def _extract_first_json_object(s: str) -> Optional[Dict[str, Any]]:
    """
    Find the first {...} in s by brace matching and parse it as JSON.
    Returns the parsed dict or None if no valid object or parse failure.
    """
    s = s.strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
