"""Chainlit UI — browser-based chat interface for the RAG agent.

Activated when USER_INTERFACE=CHAINLIT_UI. Provides a web chat UI
at http://localhost:8080 that calls the same invoke_agent() function
as the Bot Framework path.

Features:
- Clickable source links to SharePoint/Wiki pages
- In-line debug accordions: retrieved chunks, full LLM prompt, active settings
- Dynamic settings: Top K, Temperature, System Prompt, Model selection
- Conversation history via CosmosDB
"""

import json
import logging
import os
import uuid
from html import escape
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

import chainlit as cl
from chainlit.config import config as chainlit_config
from chainlit.input_widget import Select, Slider, TextInput
from chainlit.types import ThreadDict
from chainlit.user import User

# UI configuration

chainlit_config.ui.name = "ETS VA Assistant"
chainlit_config.ui.default_theme = "light"
# Use the exact uploaded JPG asset for both header branding and assistant avatars.
chainlit_config.ui.logo_file_url = "/chat/public/ai-circle-logo.jpg"
chainlit_config.ui.default_avatar_file_url = "/chat/public/ai-circle-logo.jpg"
chainlit_config.ui.avatar_size = 40
chainlit_config.features.spontaneous_file_upload = None
chainlit_config.features.unsafe_allow_html = True
chainlit_config.features.edit_message = False

chainlit_config.ui.custom_css = "/public/custom.css"
chainlit_config.ui.custom_js = "/public/debug-accordion.js"

# Set SHOW_CHAT_SETTINGS=false to hide the gear icon / settings panel (Model, Top K, Temperature, System Prompt)
# Set SHOW_CHAT_SETTINGS=true  (default) to show it
SHOW_CHAT_SETTINGS = os.environ.get("SHOW_CHAT_SETTINGS", "true").lower().strip() == "true"

# Debug panels toggle (Retrieved Chunks, Full LLM Prompt)
# Set SHOW_DEBUG_PANELS=true  in dev to show chunk details after each response
# Set SHOW_DEBUG_PANELS=false (default) for demo/prod — no debug output shown
SHOW_DEBUG_PANELS = os.environ.get("SHOW_DEBUG_PANELS", "false").lower().strip() == "true"

# Suggested follow-up prompts after each response
# Set SHOW_SUGGESTED_PROMPTS=true (default) to show 3 clickable follow-up suggestions
# Set SHOW_SUGGESTED_PROMPTS=false to disable
SHOW_SUGGESTED_PROMPTS = os.environ.get("SHOW_SUGGESTED_PROMPTS", "true").lower().strip() == "true"

# Greeting detection — respond directly without running the RAG pipeline
_GREETING_WORDS = {"hello", "hi", "hey", "greetings", "good morning", "good afternoon", "good evening", "howdy", "hola"}
_GREETING_RESPONSE = "Hello! I'm the **ETS Virtual Assistant**. How can I help you today?"
_THANKS_WORDS = {"thank you", "thanks", "thankyou", "ty", "thx"}
_THANKS_RESPONSE = "You're welcome! If you have any other questions, feel free to ask."

from m365_langchain_agent.agent import (
    invoke_agent_stream,
    generate_suggested_prompts,
    get_available_models,
    SYSTEM_PROMPT,
    DEFAULT_TOP_K,
    DEFAULT_TEMPERATURE,
    DEFAULT_MODEL,
)
from m365_langchain_agent.cosmos_store import get_cosmos_store
from m365_langchain_agent.chainlit_data_layer import CosmosDataLayer


# --- Data layer (conversation history sidebar) ---
@cl.data_layer
def get_data_layer():
    return CosmosDataLayer()


# --- Auth: auto-authenticate all users so the sidebar is accessible ---
@cl.header_auth_callback
def header_auth_callback(headers: dict) -> User:
    return User(identifier="default-user", metadata={"role": "user"})


@cl.author_rename
async def rename_author(author: str) -> str:
    """Display a friendly name while keeping stable assistant author id."""
    if author == "assistant":
        return "ETS VA Assistant"
    return author


@cl.set_starters
async def set_starters():
    """Card-style starter prompts shown in the empty chat state.

    Reads from STARTER_PROMPTS env var (JSON array of {label, message} objects).
    Returns None (no starters) if the env var is missing or empty.
    """
    raw = os.environ.get("STARTER_PROMPTS", "").strip()
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[Chainlit] STARTER_PROMPTS is not valid JSON — skipping starters")
        return None
    if not items:
        return None
    return [
        cl.Starter(label=item["label"], message=item["message"])
        for item in items
        if item.get("label") and item.get("message")
    ]


@cl.action_callback("suggested_prompt")
async def on_suggested_prompt(action: cl.Action):
    """Handle clicks on suggested follow-up prompt chips.

    Sends the suggestion as a new user message and processes it
    through the normal RAG pipeline via on_message().
    """
    # Create a Message object that mimics a real user message
    prompt = action.payload.get("prompt", "")
    if not prompt:
        return
    user_msg = cl.Message(content=prompt, author="user")
    await user_msg.send()
    # Process through the normal handler
    await on_message(user_msg)


@cl.on_chat_start
async def on_chat_start():
    """Initialize a new conversation session with configurable settings."""
    conversation_id = f"chainlit-{uuid.uuid4().hex[:12]}"
    cl.user_session.set("conversation_id", conversation_id)
    logger.info(f"[Chainlit] New session: conversation_id={conversation_id}")

    if SHOW_CHAT_SETTINGS:
        available_models = get_available_models()
        settings = await cl.ChatSettings(
            [
                Select(
                    id="model",
                    label="Model",
                    values=available_models,
                    initial_value=DEFAULT_MODEL if DEFAULT_MODEL in available_models else available_models[0],
                    description="Azure OpenAI deployment to use for generation.",
                ),
                Slider(
                    id="top_k",
                    label="Top K (Retrieved Chunks)",
                    initial=DEFAULT_TOP_K,
                    min=1,
                    max=20,
                    step=1,
                    description="Number of chunks to retrieve from AI Search.",
                ),
                Slider(
                    id="temperature",
                    label="Temperature",
                    initial=DEFAULT_TEMPERATURE,
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
        cl.user_session.set("settings", settings)
    else:
        cl.user_session.set("settings", {})


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    """Resume a previous conversation thread from the sidebar."""
    conversation_id = thread["id"]
    cl.user_session.set("conversation_id", conversation_id)
    logger.info(f"[Chainlit] Resumed thread: conversation_id={conversation_id}")

    if SHOW_CHAT_SETTINGS:
        available_models = get_available_models()
        settings = await cl.ChatSettings(
            [
                Select(
                    id="model",
                    label="Model",
                    values=available_models,
                    initial_value=DEFAULT_MODEL if DEFAULT_MODEL in available_models else available_models[0],
                    description="Azure OpenAI deployment to use for generation.",
                ),
                Slider(
                    id="top_k",
                    label="Top K (Retrieved Chunks)",
                    initial=DEFAULT_TOP_K,
                    min=1,
                    max=20,
                    step=1,
                    description="Number of chunks to retrieve from AI Search.",
                ),
                Slider(
                    id="temperature",
                    label="Temperature",
                    initial=DEFAULT_TEMPERATURE,
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
        cl.user_session.set("settings", settings)
    else:
        cl.user_session.set("settings", {})


@cl.on_settings_update
async def on_settings_update(settings):
    """Handle settings changes from the UI."""
    cl.user_session.set("settings", settings)
    model = settings.get("model", DEFAULT_MODEL)
    top_k = int(settings.get("top_k", DEFAULT_TOP_K))
    temp = settings.get("temperature", DEFAULT_TEMPERATURE)
    logger.info(f"[Chainlit] Settings updated: model={model}, top_k={top_k}, temperature={temp}")
    await cl.Message(
        content=f"Settings updated: **Model:** `{model}` | **Top K:** `{top_k}` | **Temperature:** `{temp}`",
        author="assistant",
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle incoming user messages with rich citation rendering and debug accordions."""
    conversation_id = cl.user_session.get("conversation_id")
    user_text = message.content

    if not user_text or not user_text.strip():
        await cl.Message(content="I didn't receive a message. Please try again.", author="assistant").send()
        return

    # Handle simple greetings/thanks without running the RAG pipeline
    normalized = user_text.strip().lower().rstrip("!.,?")
    if normalized in _GREETING_WORDS:
        await cl.Message(content=_GREETING_RESPONSE, author="assistant").send()
        return
    if normalized in _THANKS_WORDS:
        await cl.Message(content=_THANKS_RESPONSE, author="assistant").send()
        return

    # Read current settings from session
    settings = cl.user_session.get("settings") or {}
    model = settings.get("model", DEFAULT_MODEL)
    top_k = int(settings.get("top_k", DEFAULT_TOP_K))
    temperature = float(settings.get("temperature", DEFAULT_TEMPERATURE))
    system_prompt = settings.get("system_prompt", SYSTEM_PROMPT)

    logger.info(
        f"[Chainlit] Message: conversation={conversation_id}, "
        f"model={model}, top_k={top_k}, temp={temperature}, "
        f"text={user_text[:100]}"
    )

    # Load conversation history from CosmosDB
    try:
        cosmos = get_cosmos_store()
        history = cosmos.get_history(conversation_id)
    except Exception as e:
        logger.error(f"[Chainlit] CosmosDB read failed: {e}")
        history = []

    # Stream the RAG agent response token by token
    msg = cl.Message(content="", author="assistant")
    await msg.send()

    answer = ""
    sources = []
    raw_chunks = []
    full_prompt = ""
    search_query = ""
    original_query = ""
    query_rewritten = False

    async for chunk in invoke_agent_stream(
        query=user_text,
        conversation_history=history,
        top_k=top_k,
        temperature=temperature,
        system_prompt=system_prompt if system_prompt != SYSTEM_PROMPT else None,
        model_name=model if model != DEFAULT_MODEL else None,
    ):
        if isinstance(chunk, dict) and chunk.get("type") == "metadata":
            answer = chunk["answer"]
            sources = chunk["sources"]
            raw_chunks = chunk.get("raw_chunks", [])
            full_prompt = chunk.get("full_prompt", "")
            search_query = chunk.get("search_query", "")
            original_query = chunk.get("original_query", "")
            query_rewritten = chunk.get("query_rewritten", False)
        else:
            await msg.stream_token(chunk)

    # Append source links after streaming completes
    source_lines = []
    for s in sources:
        title = s.get("title", "Untitled")
        url = s.get("url", "")
        idx = s.get("index", 0)
        if url:
            safe_url = quote(url, safe="/:@?&#=")
            source_lines.append(f"[{idx}] [{title}]({safe_url})")
        else:
            source_lines.append(f"[{idx}] {title}")

    # Only append Sources block if the LLM answer doesn't already list citations
    answer_lower = answer.lower()
    has_citation_section = "citations:" in answer_lower or "sources:" in answer_lower
    if source_lines and not has_citation_section:
        await msg.stream_token("\n\n---\n**Sources:**\n" + "\n".join(source_lines))

    await msg.update()

    # --- Debug Panels (dev only: SHOW_DEBUG_PANELS=true) ---
    # Renders native <details> accordions directly in one stacked group.
    # Collapsed by default — click to expand. In prod: SHOW_DEBUG_PANELS=false — hidden.
    if SHOW_DEBUG_PANELS and raw_chunks:
        index_name = os.environ.get("AZURE_SEARCH_INDEX_NAME", "N/A")
        search_endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "N/A")
        semantic_config = os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIG_NAME", "")
        embedding_model = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
        prompt_type = "Custom" if system_prompt != SYSTEM_PROMPT else "Default"

        def _to_multiline_html(text: str, limit: int = None) -> str:
            safe = escape(text or "")
            if limit is not None:
                safe = safe[:limit]
            return safe.replace("\n", "<br>")

        # Build search query content
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
        search_parts.append(f"<b>Chunk Size:</b> 1024 tokens (200 overlap)<br>")
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

        # Build chunks content
        chunk_parts = []
        chunk_parts.append(f"Query: {escape(user_text)}<br>")
        chunk_parts.append(f"Index: {index_name} | Top K: {top_k} | Model: {model} | Temp: {temperature}<br>")
        chunk_parts.append(f"Chunks retrieved: {len(raw_chunks)}<br><hr>")
        for i, chunk_data in enumerate(raw_chunks):
            title = escape(chunk_data.get("document_title") or chunk_data.get("file_name") or "Untitled")
            search_score = chunk_data.get("score", 0)
            reranker_score = chunk_data.get("reranker_score")
            source_url = escape(chunk_data.get("source_url", ""))
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

        # Build prompt content
        prompt_html = (
            f'<div class="debug-prompt-pre">{escape(full_prompt)}</div>'
            if full_prompt and full_prompt.strip()
            else "No prompt captured."
        )

        # Build settings content
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

    # Save to CosmosDB for conversation history
    try:
        cosmos = get_cosmos_store()
        cosmos.save_turn(
            conversation_id=conversation_id,
            user_message=user_text,
            bot_response=answer,
        )
    except Exception as e:
        logger.error(f"[Chainlit] CosmosDB write failed: {e}")

    logger.info(
        f"[Chainlit] Response sent: conversation={conversation_id}, "
        f"model={model}, sources={len(sources)}, raw_chunks={len(raw_chunks)}"
    )

    # --- Suggested follow-up prompts ---
    if SHOW_SUGGESTED_PROMPTS and answer and not answer.startswith("Sorry,"):
        try:
            suggestions = await generate_suggested_prompts(
                query=user_text,
                answer=answer,
                conversation_history=history,
                model_name=model if model != DEFAULT_MODEL else None,
            )
            if suggestions:
                actions = [
                    cl.Action(
                        name="suggested_prompt",
                        label=s,
                        payload={"prompt": s},
                        tooltip=f"Click to ask: {s}",
                    )
                    for s in suggestions[:3]
                ]
                suggestion_msg = cl.Message(
                    content="**Want to explore further?** Click a suggestion below:",
                    author="assistant",
                    actions=actions,
                )
                await suggestion_msg.send()
                logger.info(f"[Chainlit] Showed {len(suggestions)} suggested prompts")
        except Exception as e:
            logger.warning(f"[Chainlit] Suggested prompts failed: {e}")
