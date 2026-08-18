"""LiteLLM custom guardrail — prompt-injection / jailbreak heuristic (Seam S2).

Always-on, model-free regex detector. It runs
as a `pre_call` guardrail inside the LiteLLM proxy, so every chat frontend
(Open WebUI, Onyx, or any other OpenAI-`/v1` client) gets the same protection
without any changes on the frontend side.

Blocking is fail-closed: a match raises an exception, which LiteLLM surfaces
as a 4xx/5xx error to the caller instead of forwarding the prompt to the model.

Mounted into the litellm container at /app/guardrails/injection_guardrail.py
and referenced from config.yaml as:
    guardrail: guardrails.injection_guardrail.PromptInjectionGuardrail
"""
import re
from typing import List, Literal, Optional

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.types.utils import GenericGuardrailAPIInputs

INJECTION_PATTERNS = [
    r"ignore\s+(all|any|the|your|previous|prior|above)",
    r"disregard\s+(all|any|the|your|previous|prior|above)",
    r"forget\s+(all|any|the|your|previous|prior)\s+instructions",
    r"(reveal|show|print|repeat|output)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions)",
    r"system\s+prompt",
    r"developer\s+mode",
    r"\bDAN\b",
    r"do\s+anything\s+now",
    r"jailbreak",
    r"you\s+are\s+now\b",
    r"pretend\s+to\s+be\b",
    r"act\s+as\s+(if|an?)\b",
    r"override\s+(your|the)\s+(rules|instructions|guardrails|policy)",
    r"exfiltrat",
    r"begin\s+by\s+ignoring",
    r"no\s+longer\s+bound\s+by",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def detect_injection(text: str) -> List[str]:
    """Return the human-readable patterns that matched (empty = clean)."""
    return [p.pattern for p in _COMPILED if p.search(text)]


class PromptInjectionGuardrail(CustomGuardrail):
    """Blocks requests whose text matches a known injection/jailbreak pattern."""

    async def apply_guardrail(
        self,
        inputs: GenericGuardrailAPIInputs,
        request_data: dict,
        input_type: Literal["request", "response"],
        logging_obj: Optional[object] = None,
    ) -> GenericGuardrailAPIInputs:
        texts = inputs.get("texts", []) or []
        for text in texts:
            hits = detect_injection(text)
            if hits:
                verbose_proxy_logger.warning(
                    "injection_guardrail blocked request (pattern=%s)", hits[0]
                )
                raise ValueError(
                    f"Request blocked by injection guardrail: matched pattern '{hits[0]}'"
                )
        return inputs

