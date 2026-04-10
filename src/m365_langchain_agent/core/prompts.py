"""Prompt templates for the RAG agent — loaded from .txt files.

Prompts live as plain text files in the ``prompts/`` package directory so they
can be reviewed, versioned, and edited independently of Python code.

Override priority:
  1. Environment variable (e.g. SYSTEM_PROMPT_OVERRIDE) — for deployment-time tuning
  2. Prompt file (e.g. prompts/system.txt) — version-controlled default

Prompt files are loaded once at import time via ``importlib.resources``.
"""

import importlib.resources
import logging
import os

from m365_langchain_agent.config import settings

logger = logging.getLogger(__name__)


def _load_prompt(filename: str, env_override: str = "") -> str:
    """Load a prompt from the prompts/ package directory.

    Args:
        filename: Name of the .txt file in the prompts/ directory.
        env_override: If non-empty, this value takes precedence over the file.

    Returns:
        The prompt text (stripped of leading/trailing whitespace).
    """
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


# ---------------------------------------------------------------------------
# Loaded prompts — available as module-level constants
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: str = _load_prompt(
    "system.txt",
    env_override=os.environ.get("SYSTEM_PROMPT_OVERRIDE", ""),
)

STTM_SYSTEM_PROMPT: str = _load_prompt(
    "sttm_system.txt",
    env_override=settings.sttm_system_prompt_override,
)

SUGGESTED_PROMPTS_PROMPT: str = _load_prompt(
    "suggested_prompts.txt",
    env_override=os.environ.get("SUGGESTED_PROMPTS_PROMPT_OVERRIDE", ""),
)

QUERY_REWRITE_PROMPT: str = _load_prompt(
    "query_rewrite.txt",
    env_override=os.environ.get("QUERY_REWRITE_PROMPT_OVERRIDE", ""),
)

QUERY_REFINE_PROMPT: str = _load_prompt(
    "query_refine.txt",
    env_override=os.environ.get("QUERY_REFINE_PROMPT_OVERRIDE", ""),
)

OUT_OF_SCOPE_ANSWER: str = _load_prompt(
    "out_of_scope.txt",
    env_override=os.environ.get("OUT_OF_SCOPE_ANSWER_OVERRIDE", ""),
)
