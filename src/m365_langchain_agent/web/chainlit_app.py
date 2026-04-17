"""Chainlit UI chat interface for the RAG agent."""

import json
import logging
import os
import uuid
from html import escape
from urllib.parse import quote

import chainlit as cl
from chainlit.config import config as chainlit_config
from chainlit.input_widget import Select, Slider, TextInput
from chainlit.types import ThreadDict
from chainlit.user import User

from m365_langchain_agent.config import settings
from m365_langchain_agent.core.agent import invoke_agent_stream, generate_suggested_prompts
from m365_langchain_agent.core.prompts import SYSTEM_PROMPT
from m365_langchain_agent.cosmos import get_cosmos_store
from m365_langchain_agent.web.data_layer import CosmosDataLayer

logger = logging.getLogger(__name__)

_PUBLIC_PREFIX = settings.chainlit_public_prefix.rstrip("/") or "public"

chainlit_config.ui.name = settings.app_display_name
chainlit_config.ui.default_theme = "light"
chainlit_config.ui.logo_file_url = f"{_PUBLIC_PREFIX}/avatars/ai-circle-logo.jpg?v=6"
chainlit_config.ui.default_avatar_file_url = f"{_PUBLIC_PREFIX}/avatars/ai-circle-logo.jpg?v=6"
chainlit_config.ui.avatar_size = 40
chainlit_config.features.spontaneous_file_upload = None
chainlit_config.features.unsafe_allow_html = True
chainlit_config.features.edit_message = False
chainlit_config.ui.custom_css = f"{_PUBLIC_PREFIX}/custom.css?v=11"
chainlit_config.ui.custom_js = f"{_PUBLIC_PREFIX}/debug-accordion.js?v=11"

_GREETING_WORDS = {w.strip().lower() for w in settings.greeting_words.split(",") if w.strip()}
_THANKS_WORDS = {w.strip().lower() for w in settings.thanks_words.split(",") if w.strip()}

_REASONING_TEMPLATE: dict[str, str] = {
    "intent":       "Understanding your question",
    "strategy":     "Determining approach — searching knowledge base",
    "retrieval":    "Reviewing {n} retrieved documents",
    "reasoning":    "Identifying key insights from retrieved information",
    "answer_prep":  "Preparing a comprehensive answer",
}


def _render_event(evt: dict, template: dict) -> list[tuple[str, str]] | None:
    name = evt.get("event", "")

    if name == "rewriting_query":
        return [("intent", f"… {template['intent']}")]
    if name == "query_rewritten":
        return [("intent", f"✔ {template['intent']}")]
    if name == "search_start":
        return [
            ("intent", f"✔ {template['intent']}"),
            ("strategy", f"… {template['strategy']}"),
        ]
    if name == "search_complete":
        n = evt.get("sources", 0)
        retrieval_text = template["retrieval"].format(n=n)
        return [
            ("strategy", f"✔ {template['strategy']}"),
            ("retrieval", f"✔ {retrieval_text}"),
            ("reasoning", f"… {template['reasoning']}"),
        ]
    if name == "refining_search":
        return [("strategy", "… Refining search for better results")]
    if name == "retry_search_complete":
        n = evt.get("sources", 0)
        retrieval_text = template["retrieval"].format(n=n)
        return [
            ("strategy", "✔ Refined search strategy"),
            ("retrieval", f"✔ {retrieval_text}"),
            ("reasoning", f"… {template['reasoning']}"),
        ]
    if name == "generating":
        return [
            ("reasoning", f"✔ {template['reasoning']}"),
            ("answer_prep", f"… {template['answer_prep']}"),
        ]
    return None


if not settings.disable_data_layer:
    @cl.data_layer
    def get_data_layer():
        return CosmosDataLayer()


@cl.header_auth_callback
def header_auth_callback(headers: dict) -> User:
    user_oid = headers.get("x-user-oid")
    user_name = headers.get("x-user-name", "Unknown User")
    user_email = headers.get("x-user-email", "")
    user_role = headers.get("x-user-role", "user")

    if not user_oid:
        return User(identifier="default-user", metadata={"role": "user"})

    return User(
        identifier=user_oid,
        metadata={"name": user_name, "email": user_email, "role": user_role},
    )


@cl.author_rename
async def rename_author(author: str) -> str:
    if author == "assistant":
        return settings.app_display_name
    return author


_STARTER_ITEMS: list[dict] = []
if settings.show_starter_prompts:
    _raw = settings.starter_prompts.strip()
    if _raw:
        try:
            _STARTER_ITEMS = [item for item in json.loads(_raw) if item.get("message")]
        except json.JSONDecodeError:
            logger.warning("STARTER_PROMPTS is not valid JSON — skipping starters")

from chainlit.server import app as _chainlit_app
from starlette.responses import JSONResponse
from starlette.routing import Route


async def _starter_prompts_endpoint(request):
    return JSONResponse({
        "prompts": [
            {"label": item.get("label", item["message"]), "message": item["message"]}
            for item in _STARTER_ITEMS
        ]
    })


# Insert before the catch-all /{full_path:path} route
_catchall_idx = next(
    (i for i, r in enumerate(_chainlit_app.routes) if getattr(r, 'path', '') == '/{full_path:path}'),
    len(_chainlit_app.routes)
)
_chainlit_app.routes.insert(_catchall_idx, Route("/starter-prompts", _starter_prompts_endpoint))


@cl.set_starters
async def set_starters():
    if not _STARTER_ITEMS:
        return None
    return [
        cl.Starter(label=item.get("label", item["message"]), message=item["message"])
        for item in _STARTER_ITEMS
    ]


def _build_suggestion_chips_html(suggestions: list[str]) -> str:
    chips = []
    for s in suggestions:
        safe = escape(s)
        chips.append(
            f'<div class="suggestion-chip" data-prompt="{safe}">'
            f'<span class="suggestion-chip-text">{safe}</span>'
            f'</div>'
        )
    return (
        '<div class="suggestion-chips-container">'
        '<div class="suggestion-chips-label">Want to explore further?</div>'
        + "".join(chips)
        + '</div>'
    )


async def _init_chat_settings():
    if settings.show_chat_settings:
        available_models = settings.available_models_list
        default_model = settings.azure_openai_deployment_name
        chat_settings = await cl.ChatSettings(
            [
                Select(
                    id="model",
                    label="Model",
                    values=available_models,
                    initial_value=default_model if default_model in available_models else available_models[0],
                    description="Azure OpenAI deployment to use for generation.",
                ),
                Slider(
                    id="top_k",
                    label="Top K (Retrieved Chunks)",
                    initial=settings.default_top_k,
                    min=1,
                    max=20,
                    step=1,
                    description="Number of chunks to retrieve from AI Search.",
                ),
                Slider(
                    id="temperature",
                    label="Temperature",
                    initial=settings.default_temperature,
                    min=0.0,
                    max=1.0,
                    step=0.05,
                    description="LLM randomness. Lower = more deterministic, higher = more creative.",
                ),
                TextInput(
                    id="system_prompt",
                    label="System Prompt",
                    initial=SYSTEM_PROMPT,
                    description="Instructions for the LLM. Edit to change behavior.",
                ),
            ]
        ).send()
        cl.user_session.set("settings", chat_settings)
    else:
        cl.user_session.set("settings", {})


@cl.on_chat_start
async def on_chat_start():
    conversation_id = f"chainlit-{uuid.uuid4().hex[:12]}"
    cl.user_session.set("conversation_id", conversation_id)
    logger.info("New session: conversation_id=%s", conversation_id)
    await _init_chat_settings()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    conversation_id = thread["id"]
    cl.user_session.set("conversation_id", conversation_id)
    logger.info("Resumed thread: conversation_id=%s", conversation_id)
    await _init_chat_settings()


@cl.on_settings_update
async def on_settings_update(chat_settings):
    cl.user_session.set("settings", chat_settings)
    model = chat_settings.get("model", settings.azure_openai_deployment_name)
    top_k = int(chat_settings.get("top_k", settings.default_top_k))
    temp = chat_settings.get("temperature", settings.default_temperature)
    await cl.Message(
        content=f"Settings updated: **Model:** `{model}` | **Top K:** `{top_k}` | **Temperature:** `{temp}`",
        author="assistant",
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    conversation_id = cl.user_session.get("conversation_id")
    user_text = message.content

    if not user_text or not user_text.strip():
        await cl.Message(content="I didn't receive a message. Please try again.", author="assistant").send()
        return

    normalized = user_text.strip().lower().rstrip("!.,?")
    if normalized in _GREETING_WORDS:
        await cl.Message(content=settings.greeting_response, author="assistant").send()
        return
    if normalized in _THANKS_WORDS:
        await cl.Message(content=settings.thanks_response, author="assistant").send()
        return

    chat_settings = cl.user_session.get("settings") or {}
    default_model = settings.azure_openai_deployment_name
    model = chat_settings.get("model", default_model)
    top_k = int(chat_settings.get("top_k", settings.default_top_k))
    temperature = float(chat_settings.get("temperature", settings.default_temperature))
    system_prompt = chat_settings.get("system_prompt", SYSTEM_PROMPT)

    try:
        user = cl.user_session.get("user")
        user_oid = user.identifier if user and user.identifier != "default-user" else None

        cosmos = await get_cosmos_store()
        history = await cosmos.get_history(conversation_id, user_id=user_oid)
    except Exception as e:
        logger.error("CosmosDB read failed: %s", e)
        history = []

    msg = cl.Message(content="", author="assistant")
    await msg.send()

    answer = ""
    sources = []
    raw_chunks = []
    full_prompt = ""
    search_query = ""
    original_query = ""
    query_rewritten = False

    trace_lines: list[tuple[str, str]] = []
    reasoning_template = _REASONING_TEMPLATE
    streaming_started = False

    trace_lines.append(("intent", f"… {reasoning_template['intent']}"))
    msg.content = trace_lines[0][1]
    await msg.update()

    async for chunk in invoke_agent_stream(
        query=user_text,
        conversation_history=history,
        top_k=top_k,
        temperature=temperature,
        system_prompt=system_prompt if system_prompt != SYSTEM_PROMPT else None,
        model_name=model if model != default_model else None,
    ):
        if isinstance(chunk, dict):
            if chunk.get("type") == "event":
                rendered = _render_event(chunk, reasoning_template)
                if rendered:
                    for key, text in rendered:
                        replaced = False
                        for i, (k, _) in enumerate(trace_lines):
                            if k == key:
                                trace_lines[i] = (key, text)
                                replaced = True
                                break
                        if not replaced:
                            trace_lines.append((key, text))
                    msg.content = "<br>".join(t for _, t in trace_lines)
                    await msg.update()
            elif chunk.get("type") == "metadata":
                answer = chunk["answer"]
                sources = chunk["sources"]
                raw_chunks = chunk.get("raw_chunks", [])
                full_prompt = chunk.get("full_prompt", "")
                search_query = chunk.get("search_query", "")
                original_query = chunk.get("original_query", "")
                query_rewritten = chunk.get("query_rewritten", False)
        else:
            if not streaming_started:
                streaming_started = True
                for i, (k, t) in enumerate(trace_lines):
                    if t.startswith("…"):
                        trace_lines[i] = (k, t.replace("…", "✔", 1))
                if trace_lines:
                    collapsed = "<br>".join(t for _, t in trace_lines)
                    msg.content = (
                        '<details class="thinking-accordion">'
                        '<summary class="thinking-summary">Thinking</summary>'
                        f'<div class="thinking-body">{collapsed}</div>'
                        "</details>\n\n"
                    )
                else:
                    msg.content = ""
                await msg.update()
            await msg.stream_token(chunk)

    if not streaming_started and answer:
        for i, (k, t) in enumerate(trace_lines):
            if t.startswith("…"):
                trace_lines[i] = (k, t.replace("…", "✔", 1))
        if trace_lines:
            collapsed = "<br>".join(t for _, t in trace_lines)
            msg.content = (
                '<details class="thinking-accordion">'
                '<summary class="thinking-summary">Thinking</summary>'
                f'<div class="thinking-body">{collapsed}</div>'
                "</details>\n\n"
            )
        else:
            msg.content = ""
        msg.content += answer
        await msg.update()

    source_lines = []
    seen_names: set[str] = set()
    for s in sources:
        name = s.get("file_name") or s.get("title", "Untitled")
        if name in seen_names:
            continue
        seen_names.add(name)
        url = s.get("url", "")
        idx = s.get("index", 0)
        if url:
            safe_url = quote(url, safe="/:@?&#=")
            source_lines.append(f"[{idx}] [{name}]({safe_url})")
        else:
            source_lines.append(f"[{idx}] {name}")

    answer_lower = answer.lower()
    has_citation_section = "citations:" in answer_lower or "sources:" in answer_lower or "cited sources:" in answer_lower
    if source_lines and not has_citation_section:
        await msg.stream_token("\n\n---\n**Cited Sources:**\n" + "\n".join(source_lines))

    await msg.update()

    if settings.show_debug_panels and raw_chunks:
        index_name = settings.azure_search_index_name
        search_endpoint = settings.azure_search_endpoint
        semantic_config = settings.azure_search_semantic_config_name
        embedding_model = settings.azure_openai_embedding_deployment
        prompt_type = "Custom" if system_prompt != SYSTEM_PROMPT else "Default"

        def _to_multiline_html(text: str, limit: int | None = None) -> str:
            safe = escape(text or "")
            if limit is not None:
                safe = safe[:limit]
            return safe.replace("\n", "<br>")

        search_parts = []
        search_parts.append(f"<b>Original Query:</b> {escape(original_query or user_text)}<br>")
        if query_rewritten:
            search_parts.append(f"<b>Rewritten Query:</b> {escape(search_query)}<br>")
            search_parts.append("<b>Query Rewritten:</b> Yes (conversation context used)<br>")
        else:
            search_parts.append("<b>Query Rewritten:</b> No (standalone query)<br>")
        search_parts.append("<hr>")
        search_parts.append(f"<b>Search Endpoint:</b> {escape(search_endpoint)}<br>")
        search_parts.append(f"<b>Index:</b> {escape(index_name)}<br>")
        search_parts.append(f"<b>Top K:</b> {top_k}<br>")
        search_parts.append("<b>Chunk Size:</b> 1024 tokens (200 overlap)<br>")
        search_parts.append("<hr>")
        search_parts.append("<b>Hybrid Search Components:</b><br>")
        search_parts.append(f'&nbsp;&nbsp;1. <b>Keyword (BM25):</b> search_text = "{escape(search_query or user_text)}"<br>')
        search_parts.append(f"&nbsp;&nbsp;2. <b>Vector (HNSW Cosine):</b> 3072d embedding via {escape(embedding_model)}<br>")
        if semantic_config:
            search_parts.append(f'&nbsp;&nbsp;3. <b>Semantic Reranker:</b> config = "{escape(semantic_config)}"<br>')
        else:
            search_parts.append("&nbsp;&nbsp;3. <b>Semantic Reranker:</b> DISABLED (no config set)<br>")
        search_parts.append("<hr>")
        search_parts.append(f"<b>Results Returned:</b> {len(raw_chunks)} chunks<br>")
        if raw_chunks:
            hybrid_scores = [c.get("score", 0) for c in raw_chunks]
            search_parts.append(f"<b>Hybrid RRF Score Range:</b> {min(hybrid_scores):.4f} — {max(hybrid_scores):.4f}<br>")
            rr_scores = [c.get("reranker_score") for c in raw_chunks if c.get("reranker_score")]
            if rr_scores:
                search_parts.append(f"<b>Semantic Relevance Range:</b> {min(rr_scores):.4f} — {max(rr_scores):.4f} (out of 4.0)<br>")
        search_html = "".join(search_parts)

        chunk_parts = []
        chunk_parts.append(f"Query: {escape(user_text)}<br>")
        chunk_parts.append(f"Index: {index_name} | Top K: {top_k} | Model: {model} | Temp: {temperature}<br>")
        chunk_parts.append(f"Chunks retrieved: {len(raw_chunks)}<br><hr>")
        for i, chunk_data in enumerate(raw_chunks):
            title = escape(chunk_data.get("document_title") or chunk_data.get("file_name") or "Untitled")
            search_score = chunk_data.get("score", 0)
            reranker_score = chunk_data.get("reranker_score")
            raw_source_url = chunk_data.get("source_url", "")
            source_url = escape(quote(raw_source_url, safe="/:@?&#=") if raw_source_url else "")
            source_type = escape(chunk_data.get("source_type", ""))
            chunk_idx = chunk_data.get("chunk_index", "?")
            total_chunks_val = chunk_data.get("total_chunks", "?")
            content_text = chunk_data.get("content", "")
            pii_redacted = chunk_data.get("pii_redacted", False)

            chunk_parts.append(f"<b>Chunk {i+1}: {title}</b><br>")
            chunk_parts.append(f"Hybrid RRF Score: {search_score:.4f}<br>")
            if reranker_score:
                chunk_parts.append(f"Semantic Relevance: {reranker_score:.4f} / 4.0<br>")
            chunk_parts.append(f"Source Type: {source_type}<br>")
            chunk_parts.append(f"Chunk: {chunk_idx} of {total_chunks_val}<br>")
            chunk_parts.append(f"PII Redacted: {pii_redacted}<br>")
            chunk_parts.append(f"Source URL: {source_url}<br>")
            chunk_parts.append(f'<div class="debug-text-block">{_to_multiline_html(content_text, 500)}</div><br>')
        chunks_html = "".join(chunk_parts)

        prompt_html = (
            f'<div class="debug-prompt-pre">{escape(full_prompt)}</div>'
            if full_prompt and full_prompt.strip()
            else "No prompt captured."
        )

        settings_html = (
            f"Model: {model}<br>Top K: {top_k}<br>Temperature: {temperature}<br>"
            f"System Prompt: {prompt_type}<br>Index: {index_name}<br>"
            f"Sources: {len(sources)}<br>Chunks: {len(raw_chunks)}"
        )

        accordion_msg = (
            '<div class="debug-accordion-group">'
            f'<details class="debug-accordion"><summary class="debug-accordion-summary">Search Query (AI Search)</summary><div class="debug-accordion-body">{search_html}</div></details>'
            f'<details class="debug-accordion"><summary class="debug-accordion-summary">Retrieved Chunks ({len(raw_chunks)})</summary><div class="debug-accordion-body">{chunks_html}</div></details>'
            f'<details class="debug-accordion"><summary class="debug-accordion-summary">Full LLM Prompt</summary><div class="debug-accordion-body">{prompt_html}</div></details>'
            f'<details class="debug-accordion"><summary class="debug-accordion-summary">Settings</summary><div class="debug-accordion-body">{settings_html}</div></details>'
            "</div>"
        )

        await cl.Message(content=accordion_msg, author="assistant").send()

    try:
        user = cl.user_session.get("user")
        user_oid = user.identifier if user and user.identifier != "default-user" else None
        user_name = user.metadata.get("name") if user else None
        user_email = user.metadata.get("email") if user else None

        cosmos = await get_cosmos_store()
        await cosmos.save_turn(
            conversation_id=conversation_id,
            user_message=user_text,
            bot_response=answer,
            user_id=user_oid,
            user_email=user_email,
            user_display_name=user_name,
        )
    except Exception as e:
        logger.error("CosmosDB write failed: %s", e)

    if settings.show_suggested_prompts and answer and not answer.startswith("Sorry,"):
        try:
            suggestions = await generate_suggested_prompts(
                query=user_text,
                answer=answer,
                conversation_history=history,
                model_name=model if model != default_model else None,
            )
            if suggestions:
                chips_html = _build_suggestion_chips_html(suggestions[:3])
                await cl.Message(content=chips_html, author="assistant").send()
        except Exception as e:
            logger.warning("Suggested prompts failed: %s", e)
