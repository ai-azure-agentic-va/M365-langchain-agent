"""Content safety evaluations using Azure AI Evaluation SDK.

Provides:
- Groundedness evaluation (how well the answer is grounded in the retrieved context)
- Harmful content detection (violence, sexual, self-harm, hate/unfairness)

All features are toggleable via CONTENT_SAFETY_ENABLED env var.
Results are returned as structured dicts for logging to the metrics DB
and displayed in the Chainlit Content Safety debug accordion.
"""

import asyncio
import logging
import os
from typing import Dict

from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

# Toggle content safety features on/off
CONTENT_SAFETY_ENABLED = os.environ.get("CONTENT_SAFETY_ENABLED", "false").lower() == "true"

_evaluators = {}


def _get_credential():
    return DefaultAzureCredential()


def _get_azure_ai_project() -> Dict:
    """Build the Azure AI project config from env vars."""
    return {
        "subscription_id": os.environ.get("AZURE_FOUNDRY_SUBSCRIPTION_ID", ""),
        "resource_group_name": os.environ.get("AZURE_FOUNDRY_RESOURCE_GROUP", ""),
        "project_name": os.environ.get("AZURE_FOUNDRY_WORKSPACE", ""),
    }


def _get_groundedness_evaluator():
    """Lazy-init groundedness evaluator using Azure AI project."""
    if "groundedness" not in _evaluators:
        try:
            from azure.ai.evaluation import GroundednessEvaluator
            credential = _get_credential()
            project = _get_azure_ai_project()
            model_config = {
                "azure_endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
                "azure_deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4.1"),
                "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-01-preview"),
            }
            _evaluators["groundedness"] = GroundednessEvaluator(
                credential=credential,
                azure_ai_project=project,
                model_config=model_config,
            )
            logger.info("[ContentSafety] GroundednessEvaluator initialized")
        except Exception as e:
            logger.warning(f"[ContentSafety] Failed to init GroundednessEvaluator: {e}")
            _evaluators["groundedness"] = None
    return _evaluators.get("groundedness")


def _get_content_safety_evaluator():
    """Lazy-init content safety evaluator (harmful content detection)."""
    if "content_safety" not in _evaluators:
        try:
            from azure.ai.evaluation import ContentSafetyEvaluator
            project = _get_azure_ai_project()
            credential = _get_credential()
            _evaluators["content_safety"] = ContentSafetyEvaluator(
                credential=credential,
                azure_ai_project=project,
            )
            logger.info("[ContentSafety] ContentSafetyEvaluator initialized")
        except Exception as e:
            logger.warning(f"[ContentSafety] Failed to init ContentSafetyEvaluator: {e}")
            _evaluators["content_safety"] = None
    return _evaluators.get("content_safety")


async def evaluate_groundedness(
    query: str,
    answer: str,
    context: str,
) -> Dict:
    """Evaluate how grounded the answer is in the retrieved context.

    Returns:
        {"groundedness_score": float (0-5), "groundedness_reason": str}
        or empty dict if disabled/failed.
    """
    if not CONTENT_SAFETY_ENABLED:
        return {}

    evaluator = _get_groundedness_evaluator()
    if not evaluator:
        return {}

    try:
        # The evaluator is synchronous — run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: evaluator(
                query=query,
                response=answer,
                context=context,
            ),
        )
        score = result.get("groundedness", 0)
        reason = result.get("groundedness_reason", "")
        logger.info(f"[ContentSafety] Groundedness score={score}, reason={reason[:100]}")
        return {
            "groundedness_score": float(score) if score is not None else 0.0,
            "groundedness_reason": reason or "",
        }
    except Exception as e:
        logger.error(f"[ContentSafety] Groundedness evaluation failed: {e}")
        return {"groundedness_score": "Error", "groundedness_reason": str(e)[:200]}


async def evaluate_content_safety(
    query: str,
    answer: str,
) -> Dict:
    """Evaluate for harmful content (violence, sexual, self-harm, hate/unfairness).

    Returns:
        {
            "violence": str (severity),
            "sexual": str (severity),
            "self_harm": str (severity),
            "hate_unfairness": str (severity),
        }
        or empty dict if disabled/failed.
    """
    if not CONTENT_SAFETY_ENABLED:
        return {}

    evaluator = _get_content_safety_evaluator()
    if not evaluator:
        return {}

    try:
        # The evaluator is synchronous — run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: evaluator(
                query=query,
                response=answer,
            ),
        )
        logger.info(f"[ContentSafety] Content safety result: {result}")
        return {
            "violence": result.get("violence", "N/A"),
            "sexual": result.get("sexual", "N/A"),
            "self_harm": result.get("self_harm", "N/A"),
            "hate_unfairness": result.get("hate_unfairness", "N/A"),
        }
    except Exception as e:
        logger.error(f"[ContentSafety] Content safety evaluation failed: {e}")
        return {
            "violence": "Error",
            "sexual": "Error",
            "self_harm": "Error",
            "hate_unfairness": "Error",
        }


async def run_all_evaluations(
    query: str,
    answer: str,
    context: str,
) -> Dict:
    """Run all enabled content safety evaluations.

    Returns combined results dict with all scores and flags.
    """
    if not CONTENT_SAFETY_ENABLED:
        return {"enabled": False}

    results = {"enabled": True}

    # Run both evaluations concurrently
    groundedness_task = evaluate_groundedness(query, answer, context)
    safety_task = evaluate_content_safety(query, answer)

    groundedness, safety = await asyncio.gather(
        groundedness_task, safety_task, return_exceptions=True
    )

    if isinstance(groundedness, dict):
        results.update(groundedness)
    else:
        logger.error(f"[ContentSafety] Groundedness task failed: {groundedness}")
        results["groundedness_score"] = "Error"
        results["groundedness_reason"] = str(groundedness)[:200]

    if isinstance(safety, dict):
        results.update(safety)
    else:
        logger.error(f"[ContentSafety] Safety task failed: {safety}")

    return results
