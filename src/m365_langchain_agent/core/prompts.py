"""Prompt templates — loaded from .txt files, overridable via environment variables."""

import importlib.resources
import logging

from m365_langchain_agent.config import settings

logger = logging.getLogger(__name__)


def _load_prompt(filename: str, env_override: str = "") -> str:
    if env_override:
        logger.debug("Prompt '%s' overridden via environment", filename)
        return env_override.strip()

    try:
        ref = importlib.resources.files("m365_langchain_agent.prompts").joinpath(filename)
        text = ref.read_text(encoding="utf-8")
        return text.strip()
    except Exception as e:
        logger.error("Failed to load prompt file '%s': %s", filename, e)
        raise


SYSTEM_PROMPT: str = _load_prompt("system.txt", env_override=settings.system_prompt_override)
STTM_SYSTEM_PROMPT: str = _load_prompt("sttm_system.txt", env_override=settings.sttm_system_prompt_override)
SUGGESTED_PROMPTS_PROMPT: str = _load_prompt("suggested_prompts.txt", env_override=settings.suggested_prompts_prompt_override)
QUERY_REWRITE_PROMPT: str = _load_prompt("query_rewrite.txt", env_override=settings.query_rewrite_prompt_override)
QUERY_REFINE_PROMPT: str = _load_prompt("query_refine.txt", env_override=settings.query_refine_prompt_override)
OUT_OF_SCOPE_ANSWER: str = _load_prompt("out_of_scope.txt", env_override=settings.out_of_scope_answer_override)
