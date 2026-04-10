"""FastAPI route handlers — health, readiness, test, auth, SSO status."""

import json
import logging

from fastapi import APIRouter, Request, Response

from m365_langchain_agent.config import settings
from m365_langchain_agent.models import TestQueryRequest
from m365_langchain_agent.core.agent import invoke_agent
from m365_langchain_agent.cosmos import get_cosmos_store
from m365_langchain_agent.bot.adapter import create_adapter
from m365_langchain_agent.bot.handler import DocAgentBot

from botbuilder.schema import Activity

logger = logging.getLogger(__name__)

router = APIRouter()

_adapter = None
_bot = None


def _get_adapter():
    global _adapter
    if _adapter is None:
        _adapter = create_adapter()
    return _adapter


def _get_bot():
    global _bot
    if _bot is None:
        _bot = DocAgentBot()
    return _bot


@router.post("/api/messages")
async def messages(request: Request) -> Response:
    """Bot Framework messaging endpoint."""
    content_type = request.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        return Response(status_code=415)

    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    adapter = _get_adapter()
    bot = _get_bot()

    try:
        response = await adapter.process_activity(activity, auth_header, bot.on_turn)
        if response:
            return Response(
                content=response.body,
                status_code=response.status,
                headers=response.headers,
            )
        return Response(status_code=201)
    except Exception as e:
        logger.error("Failed to process activity: %s", e, exc_info=True)
        return Response(status_code=500, content="Internal server error")


@router.get("/health")
async def health():
    return {"status": "healthy", "service": "m365-langchain-agent"}


@router.get("/readiness")
async def readiness():
    return {"status": "ready", "service": "m365-langchain-agent"}


@router.get("/sso-status")
async def sso_status():
    return {"enabled": settings.enable_sso}


@router.get("/starter-prompts")
async def starter_prompts():
    if not settings.show_starter_prompts:
        return {"prompts": []}
    raw = settings.starter_prompts.strip()
    if not raw:
        return {"prompts": []}
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return {"prompts": []}
    prompts = []
    for item in items:
        if isinstance(item, dict) and item.get("message", "").strip():
            prompts.append({
                "label": item.get("label", item["message"]).strip(),
                "message": item["message"].strip(),
            })
    return {"prompts": prompts}


@router.post("/test/query")
async def test_query(request: Request):
    """Test endpoint — bypasses Bot Framework auth for RAG pipeline verification."""
    body = await request.json()
    query = body.get("query", "")
    conversation_id = body.get("conversation_id", "test-session")
    model_name = body.get("model")
    top_k = body.get("top_k")
    temperature = body.get("temperature")
    filter_expr = body.get("filter")

    if not query:
        return {"error": "Missing 'query' field"}

    results = {"query": query, "conversation_id": conversation_id, "steps": {}}
    if model_name:
        results["model"] = model_name

    try:
        cosmos = await get_cosmos_store()
        history = await cosmos.get_history(conversation_id)
        results["steps"]["cosmos_read"] = {"status": "ok", "history_length": len(history)}
    except Exception as e:
        results["steps"]["cosmos_read"] = {"status": "error", "error": str(e)}
        history = []

    try:
        agent_result = await invoke_agent(
            query=query,
            conversation_history=history,
            model_name=model_name,
            top_k=int(top_k) if top_k else None,
            temperature=float(temperature) if temperature is not None else None,
            filter_expr=filter_expr,
        )
        answer = agent_result["answer"]
        sources = agent_result["sources"]
        results["steps"]["agent"] = {
            "status": "ok",
            "answer_length": len(answer),
            "source_count": len(sources),
        }
        results["answer"] = answer
        results["sources"] = sources
        results["raw_chunks"] = agent_result.get("raw_chunks", [])
    except Exception as e:
        results["steps"]["agent"] = {"status": "error", "error": str(e)}
        results["answer"] = None
        results["sources"] = []
        return results

    try:
        cosmos = await get_cosmos_store()
        await cosmos.save_turn(conversation_id=conversation_id, user_message=query, bot_response=answer)
        results["steps"]["cosmos_write"] = {"status": "ok"}
    except Exception as e:
        results["steps"]["cosmos_write"] = {"status": "error", "error": str(e)}

    return results


@router.get("/chat/auth/login")
async def auth_login(request: Request):
    from m365_langchain_agent.web.auth import login_route
    return login_route(request)


@router.get("/chat/auth/callback")
async def auth_callback(request: Request):
    from m365_langchain_agent.web.auth import callback_route
    return callback_route(request)


@router.get("/chat/auth/logout")
async def auth_logout(request: Request):
    from m365_langchain_agent.web.auth import logout_route
    return logout_route(request)


@router.get("/chat/auth/error")
async def auth_error(request: Request):
    message = request.query_params.get("message", "Authentication failed")
    return Response(
        content=f"<html><body><h1>Authentication Error</h1><p>{message}</p><p><a href='/chat/auth/login'>Try again</a></p></body></html>",
        media_type="text/html",
    )


@router.get("/chat/auth/signed-out")
async def auth_signed_out():
    return Response(
        content=(
            "<html><body style='font-family: Inter, Arial, sans-serif; padding: 32px;'>"
            "<h1>Signed out</h1>"
            f"<p>You have been signed out of the {settings.app_display_name}.</p>"
            "<p style='color: #667085;'>Redirecting to sign in page in <span id='countdown'>2</span> seconds...</p>"
            "<script>"
            "try { localStorage.clear(); } catch (e) {}"
            "try { sessionStorage.clear(); } catch (e) {}"
            "let seconds = 2;"
            "const countdownEl = document.getElementById('countdown');"
            "const interval = setInterval(() => {"
            "  seconds--;"
            "  if (countdownEl) countdownEl.textContent = seconds;"
            "  if (seconds <= 0) {"
            "    clearInterval(interval);"
            "    window.location.href = '/chat/auth/login?prompt=login';"
            "  }"
            "}, 1000);"
            "</script>"
            "<p><a href='/chat/auth/login?prompt=login' "
            "style='display:inline-block;padding:10px 16px;border:1px solid #d0d5dd;"
            "border-radius:10px;text-decoration:none;color:#344054;font-weight:600;'>"
            "Sign in now</a></p>"
            "</body></html>"
        ),
        media_type="text/html",
    )
