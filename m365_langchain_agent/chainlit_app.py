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

import logging
import os
import uuid

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
chainlit_config.features.spontaneous_file_upload = None

from m365_langchain_agent.agent import (
    invoke_agent,
    invoke_agent_stream,
    format_sources_markdown,
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


@cl.on_chat_start
async def on_chat_start():
    """Initialize a new conversation session with configurable settings."""
    conversation_id = f"chainlit-{uuid.uuid4().hex[:12]}"
    cl.user_session.set("conversation_id", conversation_id)
    logger.info(f"[Chainlit] New session: conversation_id={conversation_id}")

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

    await cl.Message(
        content=(
            "Hello! I'm the **ETS VA Assistant**. Ask me questions about "
            "internal policies, procedures, and documentation.\n\n"
            "I'll search the knowledge base and provide answers with "
            "**clickable source links** and citations.\n\n"
            "Use the **Settings** panel (gear icon) to adjust model, "
            "temperature, top K, and system prompt."
        )
    ).send()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    """Resume a previous conversation thread from the sidebar."""
    conversation_id = thread["id"]
    cl.user_session.set("conversation_id", conversation_id)
    logger.info(f"[Chainlit] Resumed thread: conversation_id={conversation_id}")

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


@cl.on_settings_update
async def on_settings_update(settings):
    """Handle settings changes from the UI."""
    cl.user_session.set("settings", settings)
    model = settings.get("model", DEFAULT_MODEL)
    top_k = int(settings.get("top_k", DEFAULT_TOP_K))
    temp = settings.get("temperature", DEFAULT_TEMPERATURE)
    logger.info(f"[Chainlit] Settings updated: model={model}, top_k={top_k}, temperature={temp}")
    await cl.Message(
        content=f"Settings updated: **Model:** `{model}` | **Top K:** `{top_k}` | **Temperature:** `{temp}`"
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle incoming user messages with rich citation rendering and debug accordions."""
    conversation_id = cl.user_session.get("conversation_id")
    user_text = message.content

    if not user_text or not user_text.strip():
        await cl.Message(content="I didn't receive a message. Please try again.").send()
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
    msg = cl.Message(content="")
    await msg.send()

    answer = ""
    sources = []
    raw_chunks = []
    full_prompt = ""

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
        else:
            await msg.stream_token(chunk)

    # Append source links after streaming completes
    source_lines = []
    for s in sources:
        title = s.get("title", "Untitled")
        url = s.get("url", "")
        idx = s.get("index", 0)
        if url:
            source_lines.append(f"[{idx}] [{title}]({url})")
        else:
            source_lines.append(f"[{idx}] {title}")

    if source_lines:
        await msg.stream_token("\n\n---\n**Sources:**\n" + "\n".join(source_lines))

    await msg.update()

    # --- Debug Accordion: Retrieved Chunks (collapsible Step under the message) ---
    index_name = os.environ.get("AZURE_SEARCH_INDEX_NAME", "N/A")
    chunks_step = cl.Step(name=f"Retrieved Chunks ({len(raw_chunks)})", type="tool")
    chunks_step.parent_id = msg.id

    header = (
        f"**Query:** `{user_text}`\n"
        f"**Index:** `{index_name}`\n"
        f"**Chunks retrieved:** {len(raw_chunks)} (top_k={top_k})\n\n---\n"
    )

    if not raw_chunks:
        chunks_step.output = header + "\nNo chunks retrieved from AI Search."
    else:
        chunk_sections = []
        for i, chunk in enumerate(raw_chunks):
            title = chunk.get("document_title") or chunk.get("file_name") or "Untitled"
            search_score = chunk.get("score", 0)
            reranker_score = chunk.get("reranker_score")
            source_url = chunk.get("source_url", "")
            source_type = chunk.get("source_type", "")
            chunk_idx = chunk.get("chunk_index", "?")
            total_chunks_val = chunk.get("total_chunks", "?")
            content = chunk.get("content", "")
            pii_redacted = chunk.get("pii_redacted", False)

            lines = [f"### Chunk {i+1}: {title}", ""]
            lines.append("| Field | Value |")
            lines.append("|-------|-------|")
            lines.append(f"| **Search Score** | `{search_score:.4f}` |")
            if reranker_score:
                lines.append(f"| **Reranker Score** | `{reranker_score:.4f}` |")
            lines.append(f"| **Source Type** | `{source_type}` |")
            lines.append(f"| **Chunk** | `{chunk_idx}` of `{total_chunks_val}` |")
            lines.append(f"| **PII Redacted** | `{pii_redacted}` |")
            lines.append(f"| **Source URL** | `{source_url}` |")
            lines.append("")
            lines.append(f"```\n{content}\n```")
            lines.append("")

            chunk_sections.append("\n".join(lines))

        chunks_step.output = header + "\n".join(chunk_sections)
    await chunks_step.send()

    # --- Debug Accordion: Full LLM Prompt ---
    prompt_step = cl.Step(name="Full LLM Prompt", type="tool")
    prompt_step.parent_id = msg.id
    prompt_step.output = f"```\n{full_prompt}\n```"
    await prompt_step.send()

    # --- Debug Accordion: Active Settings ---
    prompt_type = "Custom" if system_prompt != SYSTEM_PROMPT else "Default"
    settings_step = cl.Step(name="Settings Used", type="tool")
    settings_step.parent_id = msg.id
    settings_step.output = (
        "| Setting | Value |\n"
        "|---------|-------|\n"
        f"| **Model** | `{model}` |\n"
        f"| **Top K** | `{top_k}` |\n"
        f"| **Temperature** | `{temperature}` |\n"
        f"| **System Prompt** | _{prompt_type}_ |\n"
        f"| **Index** | `{index_name}` |\n"
        f"| **Sources Found** | {len(sources)} |\n"
        f"| **Raw Chunks** | {len(raw_chunks)} |"
    )
    await settings_step.send()

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
