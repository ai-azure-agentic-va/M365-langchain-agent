# Architecture — M365 LangChain Agent

C4 model diagrams from highest level (Context) down to lowest (Code).

---

## Level 1 — System Context

**What talks to what, from the outside.**

Every box is a separate system. Arrows show who initiates communication.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│                           SYSTEM CONTEXT                                │
│                                                                         │
│  ┌───────────┐                                                          │
│  │           │                                                          │
│  │           │
│  │  (User)   │                                                          │
│  │           │                                                          │
│  └─────┬─────┘                                                          │
│        │                                                                │
│        │ Asks questions in natural language                              │
│        │ via Teams / M365 Copilot / WebChat                             │
│        ▼                                                                │
│  ┌─────────────────────────────────────────────┐                        │
│  │                                             │                        │
│  │         Azure Bot Service                   │                        │
│  │         [Microsoft Managed]                 │                        │
│  │                                             │                        │
│  │  Routes messages between Teams and          │                        │
│  │  the agent container. Handles auth,         │                        │
│  │  channel fanout (Teams/DirectLine/Web).     │                        │
│  │                                             │                        │
│  └──────────────────┬──────────────────────────┘                        │
│                     │                                                   │
│                     │ HTTPS POST /api/messages                          │
│                     │ Bot Framework Activity JSON                       │
│                     ▼                                                   │
│  ┌─────────────────────────────────────────────┐                        │
│  │                                             │                        │
│  │    ★ M365 LangChain Agent                   │                        │
│  │    [This System]                            │                        │
│  │                                             │                        │
│  │  Receives user questions, searches the      │                        │
│  │  knowledge base, generates citation-backed  │                        │
│  │  answers, maintains conversation state.     │                        │
│  │                                             │                        │
│  └───┬──────────────┬──────────────┬───────────┘                        │
│      │              │              │                                     │
│      │              │              │                                     │
│      ▼              ▼              ▼                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                              │
│  │ Azure AI │  │ Azure    │  │ Azure    │                               │
│  │ Search   │  │ OpenAI   │  │ CosmosDB │                               │
│  │          │  │          │  │          │                               │
│  │ Stores   │  │ LLM      │  │ Stores   │                               │
│  │ indexed  │  │ generates│  │ conver-  │                               │
│  │ docs,    │  │ answers; │  │ sation   │                               │
│  │ serves   │  │ embedding│  │ history  │                               │
│  │ hybrid   │  │ model    │  │ per user │                               │
│  │ search   │  │ creates  │  │ session  │                               │
│  │          │  │ vectors  │  │          │                               │
│  └──────────┘  └──────────┘  └──────────┘                              │
│                                                                         │
│  ┌──────────┐                                                           │
│  │ LangSmith│  Traces every agent invocation                            │
│  │ (SaaS)   │  (optional, for observability)                            │
│  └──────────┘                                                           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key relationships:**

| From | To | Protocol | Purpose |
|------|----|----------|---------|
| Employee | Bot Service | Teams SDK / WebChat | User asks a question |
| Bot Service | LangChain Agent | HTTPS POST `/api/messages` | Forwards user message as Activity JSON |
| LangChain Agent | Azure AI Search | HTTPS (REST) | Hybrid search: keyword + vector + semantic |
| LangChain Agent | Azure OpenAI | HTTPS (REST) | Embedding (search) + generation (answer) |
| LangChain Agent | CosmosDB | HTTPS (REST) | Read/write conversation history |
| LangChain Agent | LangSmith | HTTPS (REST) | Send traces (automatic via SDK) |
| LangChain Agent | Bot Service | HTTPS POST (callback) | Send reply back to user via `serviceUrl` |

---

## Level 2 — Container Diagram

**What runs inside the system.** Each box is a deployable unit (container, service, database).

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│                           CONTAINER DIAGRAM                                  │
│                                                                              │
│                                                                              │
│    EXTERNAL                                                                  │
│    ────────                                                                  │
│                                                                              │
│    ┌────────────────────────┐       ┌──────────────────────────────┐         │
│    │ Azure Bot Service      │       │ Azure Application Gateway    │         │
│    │ [Microsoft Managed]    │──────►│ [Container Apps Ingress]     │         │
│    │                        │       │                              │         │
│    │ Configured with MSI    │       │ TLS termination (HTTPS→HTTP) │         │
│    │ or App Password auth   │       │ FQDN: <cluster-fqdn>        │         │
│    │ Channels: Teams,       │       │ Cert: <ssl-cert-name>        │         │
│    │   DirectLine, WebChat  │       │                              │         │
│    │                        │       │ Routes:                      │         │
│    │ Endpoint:              │       │  /api/messages → :8080       │         │
│    │  https://<fqdn>/       │       │  /health       → :8080       │         │
│    │    api/messages         │       │  /readiness    → :8080       │         │
│    └────────────────────────┘       └──────────────┬───────────────┘         │
│                                                    │                         │
│                                                    │ HTTP (plain, in-cluster)│
│                                                    ▼                         │
│    AZURE CONTAINER APPS                                                     │
│    ────────────────────                                                      │
│                                                                              │
│    ┌─────────────────────────────────────────────────────────────────┐       │
│    │                                                                 │       │
│    │  M365 LangChain Agent Container                                 │       │
│    │  [Python 3.10 · FastAPI · uvicorn · port 8080]                  │       │
│    │                                                                 │       │
│    │  Image: <acr-name>.azurecr.io/                                  │       │
│    │         m365-langchain-agent:<tag>                               │       │
│    │                                                                 │       │
│    │  ┌─────────────────────────────────────────────────────────┐    │       │
│    │  │ FastAPI App (web/app.py + web/routes.py)                 │    │       │
│    │  │                                                          │    │       │
│    │  │  POST /api/messages  ← Bot Framework Activity JSON       │    │       │
│    │  │  GET  /health        ← liveness probe                     │    │       │
│    │  │  GET  /readiness     ← readiness probe                    │    │       │
│    │  │  POST /test/query    ← RAG test (no Bot auth)             │    │       │
│    │  │  GET  /chat/*        ← Chainlit UI (when enabled)         │    │       │
│    │  │  GET  /chat/auth/*   ← Entra ID SSO endpoints             │    │       │
│    │  └──────────┬───────────────────────────────────────────────┘    │       │
│    │             │                                                   │       │
│    │             │ Deserializes Activity, invokes bot handler         │       │
│    │             ▼                                                   │       │
│    │  ┌──────────────────────┐    ┌──────────────────────────┐      │       │
│    │  │ Bot Framework        │    │ RAG Agent Orchestrator   │      │       │
│    │  │ Adapter + Handler    │───►│ (core/agent.py)          │      │       │
│    │  │ (bot/adapter.py +    │    │                          │      │       │
│    │  │  bot/handler.py)     │    │ 1. STTM detection        │      │       │
│    │  │                      │    │ 2. Query rewrite          │      │       │
│    │  │ MSI or App Password  │    │ 3. Search (AI Search)    │      │       │
│    │  │ Validates auth       │    │ 4. Quality gate + retry  │      │       │
│    │  │ Extracts user text   │    │ 5. Generate (LLM)        │      │       │
│    │  │ Sends reply back     │    │ 6. Return cited answer   │      │       │
│    │  └──────────┬───────────┘    └──────────────────────────┘      │       │
│    │             │                                                   │       │
│    │             │ Loads/saves conversation history                   │       │
│    │             ▼                                                   │       │
│    │  ┌──────────────────────┐                                      │       │
│    │  │ CosmosDB Client      │                                      │       │
│    │  │ (cosmos.py)          │                                      │       │
│    │  │                      │                                      │       │
│    │  │ get_history()        │                                      │       │
│    │  │ save_turn()          │                                      │       │
│    │  └──────────────────────┘                                      │       │
│    │                                                                 │       │
│    └─────────────────────────────────────────────────────────────────┘       │
│                                                                              │
│                                                                              │
│    AZURE MANAGED SERVICES                                                    │
│    ──────────────────────                                                    │
│                                                                              │
│    ┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐      │
│    │ Azure AI Search   │  │ Azure OpenAI      │  │ Azure CosmosDB    │      │
│    │                   │  │                   │  │                   │      │
│    │ Index:            │  │ Deployments:      │  │ Database:         │      │
│    │  (from .env)      │  │  (from .env)      │  │  (from .env)      │      │
│    │                   │  │                   │  │ Container:        │      │
│    │ Search types:     │  │ LLM model +       │  │  conversations    │      │
│    │  keyword          │  │ Embedding model   │  │                   │      │
│    │  + vector         │  │                   │  │ Partition key:    │      │
│    │  + semantic       │  │                   │  │  /conversation_id │      │
│    │    reranking      │  │                   │  │ TTL: 24 hours     │      │
│    └───────────────────┘  └───────────────────┘  └───────────────────┘      │
│                                                                              │
│    ┌───────────────────┐                                                     │
│    │ LangSmith (SaaS)  │  Automatic tracing via LANGCHAIN_TRACING_V2=true   │
│    │ smith.langchain.  │  Project name configured via env var                │
│    │ com               │                                                     │
│    └───────────────────┘                                                     │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Container inventory:**

| Container | Technology | Runs On | Purpose |
|-----------|-----------|---------|---------|
| M365 LangChain Agent | Python 3.10, FastAPI, uvicorn | Container Apps | Receives Bot messages, runs RAG, replies |
| Container Apps Ingress | Azure-managed | Container Apps Environment | TLS termination, path routing |
| Azure Bot Service | Microsoft-managed | Azure global | Channels (Teams/Web), Activity routing |
| Azure AI Search | Microsoft-managed | Azure | Document search index |
| Azure OpenAI | Microsoft-managed | Azure | LLM + embeddings |
| Azure CosmosDB | Microsoft-managed | Azure | Conversation state |
| LangSmith | SaaS | langchain.com | Observability/tracing |

---

## Level 3 — Component Diagram

**What's inside the container.** Each box is a Python module.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  M365 LangChain Agent Container                                             │
│  COMPONENT DIAGRAM                                                           │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐     │
│  │                                                                     │     │
│  │  web/app.py — FastAPI Application Factory                           │     │
│  │  ════════════════════════════════════════                           │     │
│  │                                                                     │     │
│  │  Responsibilities:                                                  │     │
│  │    • create_app() factory with async lifespan                       │     │
│  │    • Mounts Chainlit UI or Bot-only mode via USER_INTERFACE         │     │
│  │    • Adds SSOAuthMiddleware when ENABLE_SSO=true                    │     │
│  │    • Registers web/routes.py router (health, messages, test, auth)  │     │
│  │                                                                     │     │
│  │  Depends on: web/routes.py, web/chainlit_app.py, web/middleware.py  │     │
│  │                                                                     │     │
│  └──────────────────────────┬──────────────────────────────────────────┘     │
│                             │                                                │
│                             │ Routes POST /api/messages → adapter → bot      │
│                             ▼                                                │
│  ┌─────────────────────────────────────────────────────────────────────┐     │
│  │                                                                     │     │
│  │  bot/handler.py — DocAgentBot (ActivityHandler)                     │     │
│  │  ══════════════════════════════════════════════                     │     │
│  │                                                                     │     │
│  │  Responsibilities:                                                  │     │
│  │    • on_message_activity() — main message handler                   │     │
│  │    • on_members_added_activity() — welcome message                  │     │
│  │    • Sends typing indicator while processing                        │     │
│  │    • Orchestrates: load history → invoke agent → save turn → reply  │     │
│  │                                                                     │     │
│  │  Depends on: core/agent.py, cosmos.py, bot/adapter.py               │     │
│  │                                                                     │     │
│  └───────┬───────────────────────────────┬─────────────────────────────┘     │
│          │                               │                                   │
│          │ invoke_agent(query, history)   │ get_history() / save_turn()       │
│          ▼                               ▼                                   │
│  ┌─────────────────────────┐   ┌──────────────────────────────┐             │
│  │                         │   │                              │             │
│  │  core/agent.py          │   │  cosmos.py                   │             │
│  │  RAG Agent Orchestrator │   │  CosmosConversationStore     │             │
│  │  ═══════════════════    │   │  ════════════════════════    │             │
│  │                         │   │                              │             │
│  │  invoke_agent():        │   │  get_history():              │             │
│  │   1. Detect STTM query  │   │   Read conversation from     │             │
│  │   2. Rewrite query      │   │   CosmosDB by conversation_  │             │
│  │   3. Search docs        │   │   id (partition key)         │             │
│  │   4. Quality gate       │   │                              │             │
│  │   5. Build context      │   │  save_turn():                │             │
│  │   6. Call LLM           │   │   Append user+bot messages   │             │
│  │   7. Filter citations   │   │   to conversation doc        │             │
│  │   8. Return result      │   │   (max N msgs, TTL-based)    │             │
│  │                         │   │                              │             │
│  │  invoke_agent_stream(): │   │  Depends on:                 │             │
│  │   Streaming variant     │   │   azure-cosmos SDK           │             │
│  │   yields tokens + meta  │   │                              │             │
│  │                         │   │  Connects to:                │             │
│  └──────────┬──────────────┘   │   Azure CosmosDB             │             │
│             │                  │   (HTTPS, Managed Identity)   │             │
│             │                  │                              │             │
│             │                  └──────────────────────────────┘             │
│             │                                                               │
│             │ search(query, top_k, semantic_query)                           │
│             ▼                                                               │
│  ┌──────────────────────────────────────────────────┐                       │
│  │                                                  │                       │
│  │  core/search.py — AsyncSearchClient              │                       │
│  │  ════════════════════════════════════             │                       │
│  │                                                  │                       │
│  │  Async singleton via get_search_client()         │                       │
│  │                                                  │                       │
│  │  search(query, top_k, filter_expr,               │                       │
│  │         semantic_query):                          │                       │
│  │   1. Embed query → 3072d vector                  │                       │
│  │      (text-embedding-3-large via Azure OpenAI)   │                       │
│  │   2. Hybrid search on Azure AI Search:           │                       │
│  │      • search_text = query (keyword / BM25)      │                       │
│  │      • vector_queries = [VectorizedQuery]         │                       │
│  │      • query_type = "semantic" (reranker)         │                       │
│  │      • semantic_query = original intent           │                       │
│  │      • exhaustive_knn = optional exact KNN        │                       │
│  │   3. Parse results → list of dicts                │                       │
│  │                                                  │                       │
│  │  search_document_names(query):                   │                       │
│  │   Facet-based lightweight doc name discovery      │                       │
│  │                                                  │                       │
│  │  Connects to:                                    │                       │
│  │   Azure AI Search (HTTPS, Managed Identity)      │                       │
│  │   Azure OpenAI embeddings (HTTPS, MSI token)     │                       │
│  │                                                  │                       │
│  └──────────────────────────────────────────────────┘                       │
│                                                                              │
│  ┌──────────────────────────────────────────────────┐                       │
│  │                                                  │                       │
│  │  foundry_register.py — Foundry Agent Management  │                       │
│  │  ═════════════════════════════════════════════   │                       │
│  │                                                  │                       │
│  │  NOT part of the runtime container.              │                       │
│  │  One-shot CLI script for agent registration.     │                       │
│  │                                                  │                       │
│  │  register_agent() — POST to Foundry Agents API   │                       │
│  │  list_agents()    — GET registered agents         │                       │
│  │  delete_agent()   — DELETE an agent by ID         │                       │
│  │                                                  │                       │
│  │  Connects to:                                    │                       │
│  │   Azure AI Foundry REST API                      │                       │
│  │   (Bearer token via DefaultAzureCredential)      │                       │
│  │                                                  │                       │
│  └──────────────────────────────────────────────────┘                       │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Level 4 — Request Flow (Sequence)

![Sequence Diagram](images/sequence-diagram.png)

**What happens when a user sends a message, step by step.**

```
 Employee          Teams/           Bot              App            LangChain     Azure AI    Azure      CosmosDB
 (User)            M365             Service          Gateway        Agent         Search      OpenAI
   │                │                │                │              │              │           │           │
   │ "What is the   │                │                │              │              │           │           │
   │  VPN policy?"  │                │                │              │              │           │           │
   │───────────────►│                │                │              │              │           │           │
   │                │  Activity JSON │                │              │              │           │           │
   │                │───────────────►│                │              │              │           │           │
   │                │                │  HTTPS POST    │              │              │           │           │
   │                │                │  /api/messages │              │              │           │           │
   │                │                │───────────────►│              │              │           │           │
   │                │                │                │  HTTP POST   │              │           │           │
   │                │                │                │  /api/msg    │              │           │           │
   │                │                │                │─────────────►│              │           │           │
   │                │                │                │              │              │           │           │
   │                │                │                │      ┌───────┤              │           │           │
   │                │                │                │      │ 1. BotFrameworkAdapter            │           │
   │                │                │                │      │    validates auth header           │           │
   │                │                │                │      │    (JWT from Bot Service)          │           │
   │                │                │                │      │                                    │           │
   │                │                │                │      │ 2. DocAgentBot.on_message()        │           │
   │                │                │                │      │    extracts user text               │           │
   │                │                │                │      │                      │              │           │
   │                │                │                │      │ 3. Load history      │              │           │
   │                │                │                │      │─────────────────────────────────────────────►│
   │                │                │                │      │                      │              │        │
   │                │                │                │      │    history = []      │              │   read │
   │                │                │                │      │◄─────────────────────────────────────────────│
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 4. invoke_agent()    │              │        │
   │                │                │                │      │    query="What is    │              │        │
   │                │                │                │      │     the VPN policy?" │              │        │
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 5. Embed query       │              │        │
   │                │                │                │      │──────────────────────────────────►│        │
   │                │                │                │      │                      │     vector ◄┤        │
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 6. Hybrid search     │              │        │
   │                │                │                │      │─────────────────────►│              │        │
   │                │                │                │      │     5 doc chunks  ◄──┤              │        │
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 7. Build prompt:     │              │        │
   │                │                │                │      │    system + history  │              │        │
   │                │                │                │      │    + docs + query    │              │        │
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 8. Call LLM          │              │        │
   │                │                │                │      │──────────────────────────────────►│        │
   │                │                │                │      │           cited answer ◄───────────┤        │
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 9. Append sources    │              │        │
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 10. Save turn        │              │        │
   │                │                │                │      │──────────────────────────────────────────────►│
   │                │                │                │      │                      │              │  upsert│
   │                │                │                │      │                      │              │        │
   │                │                │                │      │ 11. Send reply       │              │        │
   │                │                │                │      │    via serviceUrl    │              │        │
   │                │                │                │      │    callback          │              │        │
   │                │                │                │      └───────┤              │              │        │
   │                │                │                │              │              │              │        │
   │                │                │  HTTPS POST to serviceUrl     │              │              │        │
   │                │                │◄─────────────────────────────┤              │              │        │
   │                │  Activity JSON │                │              │              │              │        │
   │                │◄───────────────│                │              │              │              │        │
   │  "Based on     │                │                │              │              │              │        │
   │   doc [1]..."  │                │                │              │              │              │        │
   │◄───────────────│                │                │              │              │              │        │
   │                │                │                │              │              │              │        │
```

**Step-by-step breakdown:**

| Step | Component | Action | Protocol | Details |
|------|-----------|--------|----------|---------|
| 1 | Employee | Types question | Teams UI | "What is the VPN policy?" |
| 2 | Teams | Sends to Bot Service | Internal | Activity JSON with type=message |
| 3 | Bot Service | Forwards to endpoint | HTTPS POST `/api/messages` | Adds JWT auth header |
| 4 | Container Apps Ingress | TLS termination | HTTPS → HTTP | Forwards to container port 8080 |
| 5 | web/routes.py | Deserializes Activity | In-process | adapter.process_activity() via bot/adapter.py |
| 6 | bot/handler.py | Validates, extracts text | In-process | DocAgentBot.on_message_activity() |
| 7 | cosmos.py | Loads conversation history | HTTPS → CosmosDB | get_history(conversation_id) |
| 8 | core/agent.py | Detects STTM, rewrites query | In-process | "sttm" in query check + _rewrite_query_with_history() |
| 9 | core/search.py | Embeds + hybrid search | HTTPS → Azure OpenAI + AI Search | keyword + 3072d vector + semantic reranking |
| 10 | core/agent.py | Quality gate + optional retry | In-process / HTTPS | Checks retrieval_score_threshold, refines query if needed |
| 11 | core/agent.py | Builds prompt + context | In-process | System prompt + history + formatted docs + question |
| 12 | core/agent.py | Generates answer | HTTPS → Azure OpenAI | LLM model, configurable temperature |
| 13 | core/agent.py | Filters cited sources | In-process | Matches [1], [2] citation indices to source list |
| 14 | cosmos.py | Saves turn | HTTPS → CosmosDB | save_turn(conversation_id, user_msg, bot_msg) |
| 15 | bot/handler.py | Sends reply | HTTPS → Bot Service | Via serviceUrl callback in Activity |
| 16 | Bot Service | Routes to Teams | Internal | Activity response rendered as chat message |
| 17 | Employee | Sees answer | Teams UI | Grounded answer with [1], [2] citations |

---

## Deployment Topology

**Physical view — what runs where.**

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  AZURE (<region>)                                                    │
│                                                                      │
│  Resource Group: <resource-group>                                     │
│  ┌────────────────────────────────────────────────────────────┐      │
│  │                                                            │      │
│  │  Container Apps Environment                                │      │
│  │                                                            │      │
│  │  ┌──────────────────────────────────────────────────┐     │      │
│  │  │ Container App: m365-langchain-agent               │     │      │
│  │  │ Image: <acr-name>.azurecr.io/                     │     │      │
│  │  │        m365-langchain-agent:<tag>                  │     │      │
│  │  │ Port: 8080                                        │     │      │
│  │  │ CPU: 0.5 | Memory: 1 GiB                         │     │      │
│  │  └──────────────────────────────────────────────────┘     │      │
│  │                                                            │      │
│  │  Ingress: External, HTTPS (auto TLS)                       │      │
│  │  FQDN: <app-name>.<region>.azurecontainerapps.io           │      │
│  │                                                            │      │
│  └────────────────────────────────────────────────────────────┘      │
│                                                                      │
│  Resource Group: <services-resource-group>                            │
│  ┌────────────────────────┐  ┌────────────────────────┐             │
│  │ Azure AI Search        │  │ Azure OpenAI           │             │
│  │ <search-service-name>  │  │ <openai-resource-name> │             │
│  │ Index: <index-name>    │  │ LLM + embedding models │             │
│  └────────────────────────┘  └────────────────────────┘             │
│                                                                      │
│  Resource Group: <cosmos-resource-group>                              │
│  ┌────────────────────────┐                                          │
│  │ Azure CosmosDB         │                                          │
│  │ DB: <database-name>    │                                          │
│  │ Container:             │                                          │
│  │  conversations         │                                          │
│  └────────────────────────┘                                          │
│                                                                      │
│  Global                                                              │
│  ┌────────────────────────┐                                          │
│  │ Azure Bot Service      │                                          │
│  │ <bot-name>             │                                          │
│  │ Endpoint: https://     │                                          │
│  │  <fqdn>/api/messages   │                                          │
│  └────────────────────────┘                                          │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘

  External SaaS
  ┌────────────────────────┐
  │ LangSmith              │
  │ smith.langchain.com    │
  │ Project: <project>     │
  └────────────────────────┘
```

---

## Authentication & Security

### Overview

The system has three distinct authentication boundaries:
1. **Inbound** — Bot Service authenticating TO our agent (JWT validation)
2. **Outbound Reply** — Our agent authenticating BACK to Bot Service (MSI or app secret)
3. **Backend Services** — Our agent authenticating to Azure services (API keys or MSI)

```
                                      ┌──────────────────────────────────────────┐
                                      │         TRUST BOUNDARIES                  │
                                      │                                          │
  ┌──────────────┐  ① JWT Bearer      │   ┌────────────────────────────────┐    │
  │              │  (Azure AD token)   │   │                                │    │
  │  Azure Bot   │─────────────────────┼──►│   M365 LangChain Agent         │    │
  │  Service     │                     │   │   (BotFrameworkAdapter)         │    │
  │              │◄────────────────────┼───│                                │    │
  │  (Microsoft  │  ② MSI Token        │   │   Validates ① inbound JWT      │    │
  │   Managed)   │  (reply via         │   │   Acquires ② MSI token to      │    │
  │              │   serviceUrl)       │   │     send reply back             │    │
  └──────────────┘                     │   │                                │    │
                                      │   └────┬──────────┬──────────┬─────┘    │
                                      │        │          │          │          │
                                      │   ③ API Key  ③ API Key  ③ API Key     │
                                      │        │          │          │          │
                                      │        ▼          ▼          ▼          │
                                      │   ┌────────┐ ┌────────┐ ┌────────┐    │
                                      │   │ Azure  │ │ Azure  │ │ Azure  │    │
                                      │   │ OpenAI │ │ AI     │ │ Cosmos │    │
                                      │   │        │ │ Search │ │ DB     │    │
                                      │   └────────┘ └────────┘ └────────┘    │
                                      │                                          │
                                      │   ┌────────┐                             │
                                      │   │ Lang   │  ④ API Key (SaaS)          │
                                      │   │ Smith  │                             │
                                      │   └────────┘                             │
                                      └──────────────────────────────────────────┘
```

### ① Inbound Authentication — Bot Service → Agent

When a user sends a message in Teams/WebChat, Bot Service forwards it to our `/api/messages`
endpoint. Bot Service authenticates the request by including a JWT (JSON Web Token) in the
`Authorization` header.

**Token flow:**
```
Bot Service                                          LangChain Agent
    │                                                      │
    │  1. Acquires JWT from Azure AD                       │
    │     Audience = BOT_APP_ID (MSI client ID)            │
    │     Issuer = login.botframework.com                  │
    │     or login.microsoftonline.com                     │
    │                                                      │
    │  2. POST /api/messages                               │
    │     Authorization: Bearer <jwt>                      │
    │     Content-Type: application/json                   │
    │     Body: { Activity JSON }                          │
    │─────────────────────────────────────────────────────►│
    │                                                      │
    │                     3. BotFrameworkAdapter validates: │
    │                        • JWT signature (public keys) │
    │                        • Audience = BOT_APP_ID       │
    │                        • Issuer is trusted            │
    │                        • Token not expired            │
    │                        • Channel endorsements         │
    │                                                      │
    │                     4. If valid → process Activity    │
    │                        If invalid → 401 Unauthorized  │
    │                                                      │
```

**Configuration required:**
| Variable | Purpose | Example |
|----------|---------|---------|
| `BOT_APP_ID` | Bot's identity (MSI client ID or App Registration ID) | `<guid>` |
| `BOT_AUTH_TENANT` | Azure AD tenant for single-tenant/MSI validation | `<guid>` |

### ② Outbound Authentication — Agent → Bot Service (Reply)

After processing the message, the agent sends the reply back to Bot Service via the `serviceUrl`
provided in the incoming Activity. This outbound call requires authentication.

**Supported auth modes (auto-detected at startup):**

| Mode | Condition | Credential Class | How It Works |
|------|-----------|-----------------|--------------|
| **UserAssignedMSI** | `BOT_APP_ID` set, `BOT_APP_PASSWORD` empty | `MsiAppCredentials` | Uses `ManagedIdentityCredential` from `azure-identity` to acquire tokens from the managed identity endpoint |
| **App Password** | `BOT_APP_ID` set, `BOT_APP_PASSWORD` set | `MicrosoftAppCredentials` | Uses client_credentials grant with app ID + secret |
| **No Auth** | `BOT_APP_ID` empty | None | Emulator/local testing only |

**MSI outbound flow (UserAssignedMSI mode):**
```
LangChain Agent (Container App)              Azure AD                Bot Connector
    │                                           │                        │
    │  1. context.send_activity(reply)           │                        │
    │                                           │                        │
    │  2. MsiAppCredentials.get_access_token()  │                        │
    │     ManagedIdentityCredential.get_token()  │                        │
    │─────────────────────────────────────────►│                        │
    │     scope: api://botframework.com/.default │                        │
    │                                           │                        │
    │  3. Azure AD returns access token          │                        │
    │     (via managed identity endpoint)         │                        │
    │◄─────────────────────────────────────────│                        │
    │                                           │                        │
    │  4. POST {serviceUrl}/v3/conversations/    │                        │
    │       {conversationId}/activities           │                        │
    │     Authorization: Bearer <msi-token>      │                        │
    │────────────────────────────────────────────────────────────────────►│
    │                                           │                        │
    │  5. 200 OK (reply delivered to user)       │                        │
    │◄────────────────────────────────────────────────────────────────────│
```

**MSI prerequisites:**
- The User-Assigned Managed Identity must be assigned to the Container App
- The MSI's service principal must be authorized for the Bot Framework API scope
- `BOT_AUTH_TENANT` must be set as an environment variable

**Custom adapter (botbuilder-python limitation):**

The `botbuilder-python` SDK does not natively support `UserAssignedMSI`. The SDK's
`BotFrameworkAdapter` always creates `MicrosoftAppCredentials` (which requires `client_secret`)
for outbound calls via a name-mangled private method `__get_app_credentials`.

The application includes `MsiBotFrameworkAdapter` (in `bot/adapter.py`) that:
1. Subclasses `BotFrameworkAdapter`
2. Monkey-patches `_BotFrameworkAdapter__get_app_credentials` in `__init__`
3. Returns `MsiAppCredentials` instead of `MicrosoftAppCredentials`
4. `MsiAppCredentials` extends `AppCredentials` and uses `azure.identity.ManagedIdentityCredential`

```python
# Auto-detection at startup:
if BOT_APP_ID and not BOT_APP_PASSWORD:
    adapter = MsiBotFrameworkAdapter(settings)   # MSI mode
else:
    adapter = BotFrameworkAdapter(settings)       # Standard mode
```

### ③ Backend Service Authentication

| Connection | Auth Method | Credential Source | Transport |
|------------|-------------|-------------------|-----------|
| Agent → Azure OpenAI (LLM) | Managed Identity (`DefaultAzureCredential`) | User-Assigned MSI via `AZURE_CLIENT_ID` | HTTPS (public endpoint) |
| Agent → Azure OpenAI (Embeddings) | Managed Identity (`DefaultAzureCredential`) | User-Assigned MSI via `AZURE_CLIENT_ID` | HTTPS (public endpoint) |
| Agent → Azure AI Search | Managed Identity (`DefaultAzureCredential`) | User-Assigned MSI via `AZURE_CLIENT_ID` | HTTPS (public endpoint) |
| Agent → Azure CosmosDB | Managed Identity (`DefaultAzureCredential`) | User-Assigned MSI via `AZURE_CLIENT_ID` | HTTPS (private endpoint) |
| Agent → LangSmith | API Key in header (`x-api-key`) | Container Apps secret: `LANGSMITH_API_KEY` | HTTPS (SaaS) |
| Foundry registration script → AI Foundry | `DefaultAzureCredential` (Bearer token) | Azure AD (interactive/MSI) | HTTPS |

### ④ TLS / Transport Security

```
Internet                    Container Apps Ingress          Container App
   │                           │                               │
   │  HTTPS (TLS 1.2+)        │                               │
   │  Managed certificate      │   HTTP (internal)             │
   │  (auto-provisioned)       │   No TLS (trusted network)    │
   │──────────────────────────►│──────────────────────────────►│
   │                           │                               │
   │  Certificate must be      │   Container Apps terminates   │
   │  trusted by Azure Bot     │   TLS and forwards as HTTP    │
   │  Service (NOT self-signed)│   to container on port 8080   │
```

**Critical requirement:** Azure Bot Service **will not connect** to endpoints with self-signed
or untrusted TLS certificates. There is no error returned to the user — Bot Service silently
drops the request. The container logs will show zero inbound POST requests.

**Certificate management:**
| Setting | Value |
|---------|-------|
| TLS Termination | Container Apps Ingress (managed) |
| Certificate | Auto-provisioned managed certificate |
| Recommended | Use Container Apps managed certificates (auto-renewal) or custom domain with your own cert |

### Network Security

```
┌──────────────────────────────────────────────────────────────────┐
│  Container Apps VNet                                             │
│                                                                  │
│  ┌──────────────────────────┐                                    │
│  │  Container Apps Subnet   │                                    │
│  │                          │                                    │
│  │  Container ──── Ingress ──── Internet                         │
│  │   │                                                           │
│  │   │  Private Endpoint (10.224.0.x)                            │
│  │   └──────────────────────────────────── CosmosDB              │
│  │                                         (public access OFF)   │
│  │                                                               │
│  │   Public Endpoint (HTTPS)                                     │
│  │   ├──────────────────────────────────── Azure OpenAI          │
│  │   ├──────────────────────────────────── Azure AI Search       │
│  │   └──────────────────────────────────── LangSmith (SaaS)      │
│  │                                                               │
│  └──────────────────────────────────────────────────────────────┘│
│                                                                  │
│  Private DNS Zone: privatelink.documents.azure.com               │
│    A record: <cosmos-account>       → <private-ip>               │
│    A record: <cosmos-account>-<region> → <private-ip>            │
│    (BOTH records required — SDK resolves regional endpoint)      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

| Service | Access Mode | Notes |
|---------|-------------|-------|
| CosmosDB | Private endpoint only | Public network access disabled. Requires private DNS zone with base AND regional A records |
| Azure OpenAI | Public endpoint | API key auth. Can be locked to VNet with private endpoint if needed |
| Azure AI Search | Public endpoint | API key auth. Can be locked to VNet with private endpoint if needed |
| LangSmith | SaaS (public) | Outbound HTTPS to `smith.langchain.com` |

### Secret Management

```
┌──────────────────────────────┐     ┌──────────────────────────────┐
│  Container Apps Secrets      │     │  Container Apps Env Vars      │
│                              │     │                              │
│  LANGSMITH_API_KEY       ●   │     │  AZURE_OPENAI_ENDPOINT   ○   │
│  CHAINLIT_AUTH_SECRET    ●   │     │  AZURE_SEARCH_ENDPOINT   ○   │
│                              │     │  AZURE_COSMOS_ENDPOINT   ○   │
│  ● = sensitive, encrypted    │     │  BOT_APP_ID              ○   │
│    at rest                   │     │  AZURE_CLIENT_ID         ○   │
│                              │     │  AZURE_SEARCH_INDEX_NAME ○   │
│  Auth: Managed Identity      │     │  LOG_LEVEL               ○   │
│  (no API keys needed)        │     │  ○ = non-sensitive            │
│                              │     │                              │
└──────────────────────────────┘     └──────────────────────────────┘

         │                                      │
         └──────────────┬───────────────────────┘
                        │
                        ▼
              Container env vars (injected at startup)
              Never written to disk inside container
```

**Security rules:**
- API keys stored in Container Apps secrets (encrypted at rest)
- Non-sensitive config (endpoints, feature flags) as environment variables
- No secrets in source code, Dockerfiles, or plain env vars
- `.env` files used only for local development (gitignored)
- Container runs as non-root user (Python slim base image)

### ⑤ Entra ID SSO — Chainlit UI Authentication

When `USER_INTERFACE=CHAINLIT_UI` and `ENABLE_SSO=true`, the Chainlit web UI is protected
by Microsoft Entra ID (Azure AD) Single Sign-On using the **OIDC Authorization Code Flow**.

**Components:**

| Module | Role |
|--------|------|
| `web/auth.py` | MSAL client, session cookie signing, login/callback/logout handlers |
| `web/middleware.py` | `SSOAuthMiddleware` — enforces auth on `/chat/` routes |
| `web/routes.py` | Registers `/chat/auth/*` endpoints on the FastAPI router |

**Authentication flow:**

```
Browser                 Agent (/chat/auth/*)            Entra ID (Azure AD)
  │                           │                              │
  │  1. GET /chat/            │                              │
  │──────────────────────────►│                              │
  │                           │                              │
  │  2. SSOAuthMiddleware:    │                              │
  │     No session cookie     │                              │
  │     → Redirect to login   │                              │
  │◄──────────────────────────│                              │
  │                           │                              │
  │  3. GET /chat/auth/login  │                              │
  │──────────────────────────►│                              │
  │                           │  4. build_auth_url()         │
  │                           │     MSAL get_authorization_  │
  │                           │     request_url()            │
  │                           │                              │
  │  5. 302 → Entra ID /authorize                            │
  │─────────────────────────────────────────────────────────►│
  │                           │                              │
  │  6. User signs in (Entra login page)                     │
  │     (MFA if configured)   │                              │
  │                           │                              │
  │  7. 302 → /chat/auth/callback?code=<auth_code>&state=   │
  │◄─────────────────────────────────────────────────────────│
  │                           │                              │
  │  8. GET /chat/auth/callback                              │
  │──────────────────────────►│                              │
  │                           │  9. Validate state cookie    │
  │                           │  10. MSAL acquire_token_by_  │
  │                           │      authorization_code()     │
  │                           │─────────────────────────────►│
  │                           │     id_token + access_token   │
  │                           │◄─────────────────────────────│
  │                           │                              │
  │                           │  11. Extract claims:         │
  │                           │      oid, name, email, groups │
  │                           │                              │
  │                           │  12. Create signed session   │
  │                           │      cookie (itsdangerous)   │
  │                           │                              │
  │  13. 302 → /chat/         │                              │
  │      Set-Cookie:          │                              │
  │        m365_sso_session   │                              │
  │◄──────────────────────────│                              │
  │                           │                              │
  │  14. Subsequent requests: │                              │
  │      Cookie present →     │                              │
  │      SSOAuthMiddleware    │                              │
  │      injects x-user-*     │                              │
  │      headers → Chainlit   │                              │
  │      reads via            │                              │
  │      header_auth_callback │                              │
```

**Session management:**

| Setting | Purpose | Default |
|---------|---------|---------|
| `SESSION_SECRET` | Key for signing session cookies (itsdangerous) | Required (from Key Vault) |
| `SESSION_IDLE_TIMEOUT` | Max age of session cookie (seconds) | 28800 (8 hours) |
| `SESSION_COOKIE_SECURE` | Require HTTPS for cookies | `true` in production |

**Role-based access:**

The middleware extracts `groups` from the Entra ID token claims and compares against
configured security group IDs. Users in the `AI_VA_ADMINS_GROUP_ID` group receive
the `admin` role; all other authenticated users receive `user`.

```
id_token claims → { oid, name, email, groups: ["<group-id-1>", "<group-id-2>"] }
                                          │
                                          ▼
                               AI_VA_ADMINS_GROUP_ID in groups?
                                    │               │
                                   Yes              No
                                    │               │
                                    ▼               ▼
                              role = "admin"   role = "user"
```

**Middleware passthrough rules:**

The `SSOAuthMiddleware` does NOT require authentication for:
- `/chat/auth/*` — login, callback, logout endpoints
- `/chat/ws/*` — Chainlit WebSocket connections (authenticated via headers)
- `/chat/project/*` — Chainlit project metadata
- `/chat/public/*` — Static assets (CSS, JS, images)
- `/chat/favicon*` — Favicon
- `/chat/files/*` — Uploaded files

**Entra ID prerequisites:**

| Requirement | Owner | Status |
|-------------|-------|--------|
| App Registration (client ID + secret) | Entra ID Admin | Required |
| Redirect URI: `https://<fqdn>/chat/auth/callback` | Entra ID Admin | Required |
| Security Group: `AI-VA-Users` | Entra ID Admin | Required |
| Security Group: `AI-VA-Admins` | Entra ID Admin | Optional |
| `groups` claim in token | App Registration manifest | Required |
| API permission: `User.Read` | App Registration | Required |

### Production Hardening Checklist

| Item | Current State | Production Recommendation |
|------|--------------|--------------------------|
| Bot auth | UserAssignedMSI (custom adapter) | Migrate to Microsoft 365 Agents SDK when stable |
| TLS cert | Let's Encrypt (manual renewal) | Azure Key Vault + cert-manager for auto-renewal |
| Azure OpenAI auth | API key | Managed Identity + RBAC (`Cognitive Services OpenAI User`) |
| AI Search auth | API key | Managed Identity + RBAC (`Search Index Data Reader`) |
| CosmosDB auth | API key + private endpoint | Managed Identity + RBAC (`Cosmos DB Built-in Data Contributor`) |
| CosmosDB network | Private endpoint (public OFF) | Already production-ready |
| OpenAI/Search network | Public endpoint | Add private endpoints + VNet integration |
| Container security | Default | Restrict egress to required endpoints only via VNet/NSG rules |
| Secret rotation | Manual | Azure Key Vault + CSI driver for auto-rotation |
| Audit logging | LangSmith traces | Add Azure Monitor + Log Analytics workspace |

---

## Data Flow

```
                    ┌───────────────────────────────────────────┐
                    │                                           │
  INGESTION         │  Document Source (SharePoint / Wiki)      │
  (separate         │       │                                   │
   pipeline)        │       ▼                                   │
                    │  Ingestion Pipeline                       │
                    │       │                                   │
                    │       ▼                                   │
                    │  Parse → Chunk → PII → Embed → Index      │
                    │       │                                   │
                    │       ▼                                   │
                    │  Azure AI Search (<index-name>)           │
                    │                                           │
                    └───────────────────┬───────────────────────┘
                                        │
                                        │ READ ONLY
                                        ▼
                    ┌───────────────────────────────────────────┐
                    │                                           │
  QUERY             │  User question                            │
  (this project)    │       │                                   │
                    │       ▼                                   │
                    │  Bot Service → LangChain Agent             │
                    │       │                                   │
                    │       ├──► AI Search (hybrid retrieval)    │
                    │       ├──► OpenAI (generate answer)        │
                    │       ├──► CosmosDB (conversation state)   │
                    │       │                                   │
                    │       ▼                                   │
                    │  Cited answer back to user                 │
                    │                                           │
                    └───────────────────────────────────────────┘
```

The agent is **read-only** against the search index. Document ingestion is handled by a separate pipeline.

---

## Environment Variables

All configuration is externalized via environment variables. No resource names, endpoints, or keys are hardcoded in the application code.

See [.env.example](.env.example) for the complete list of required variables.

| Group | Key Variables |
|-------|--------------|
| Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME` (auth via Managed Identity) |
| Azure AI Search | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX_NAME` (auth via Managed Identity) |
| CosmosDB | `AZURE_COSMOS_ENDPOINT`, `AZURE_COSMOS_DATABASE` (auth via Managed Identity) |
| Bot Framework | `BOT_APP_ID`, `BOT_APP_PASSWORD` |
| Entra ID SSO | `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET` (from Key Vault) |
| Session | `SESSION_SECRET` (from Key Vault), `SESSION_IDLE_TIMEOUT`, `SESSION_COOKIE_SECURE` |
| Key Vault | `KEYVAULT_URL`, secret name fields for runtime resolution |
| LangSmith | `LANGSMITH_API_KEY`, `LANGCHAIN_TRACING_V2` |
| UI Mode | `USER_INTERFACE` (`CHAINLIT_UI` or `BOT_SERVICE`), `ENABLE_SSO` |

---

## API Documentation

All HTTP endpoints served by the FastAPI application. The server listens on port 8080.

### Core Endpoints

| Method | Path | Auth | Module | Purpose |
|--------|------|------|--------|---------|
| `POST` | `/api/messages` | Bot Framework JWT | `web/routes.py` | Bot Framework messaging — receives Activity JSON from Azure Bot Service |
| `GET` | `/health` | None | `web/routes.py` | Liveness probe — returns `{"status": "healthy"}` |
| `GET` | `/readiness` | None | `web/routes.py` | Readiness probe — returns `{"status": "ready"}` |
| `GET` | `/sso-status` | None | `web/routes.py` | Reports whether SSO is enabled: `{"enabled": true/false}` |
| `GET` | `/starter-prompts` | None | `web/routes.py` | Returns starter prompt cards as JSON for UI consumption |
| `POST` | `/test/query` | None | `web/routes.py` | Test endpoint — invokes the full RAG pipeline without Bot Framework auth |
| `GET` | `/` | None | `web/app.py` | Root — redirects to `/chat/` (CHAINLIT_UI) or returns service info (BOT_SERVICE) |

### SSO Authentication Endpoints (Chainlit UI mode)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/chat/auth/login` | None | Initiates OIDC Authorization Code Flow — redirects to Entra ID |
| `GET` | `/chat/auth/callback` | None | Handles OAuth callback — exchanges code for tokens, sets session cookie |
| `GET` | `/chat/auth/logout` | Session cookie | Clears cookies, redirects to Entra ID logout |
| `GET` | `/chat/auth/signed-out` | None | Post-logout landing page with auto-redirect to login |
| `GET` | `/chat/auth/error` | None | Displays authentication error with retry link |

### Chainlit UI Routes (mounted sub-application)

| Path | Purpose |
|------|---------|
| `/chat/` | Chainlit web chat UI (main page) |
| `/chat/ws/*` | Chainlit WebSocket connections |
| `/chat/project/*` | Chainlit project metadata |
| `/chat/public/*` | Static assets (CSS, JS, images) |
| `/chat/files/*` | File uploads |

### Endpoint Details

#### `POST /api/messages`

Receives Bot Framework Activity JSON from Azure Bot Service. Validates the JWT in the
`Authorization` header via `BotFrameworkAdapter`, then routes to `DocAgentBot.on_turn()`.

```
Request:
  Headers:
    Authorization: Bearer <jwt>
    Content-Type: application/json
  Body: Bot Framework Activity JSON
    {
      "type": "message",
      "text": "What is the VPN policy?",
      "from": { "id": "...", "aadObjectId": "..." },
      "conversation": { "id": "..." },
      "serviceUrl": "https://smba.trafficmanager.net/...",
      ...
    }

Response:
  201 Created (success, no body)
  401 Unauthorized (invalid JWT)
  415 Unsupported Media Type (non-JSON content type)
  500 Internal Server Error
```

#### `POST /test/query`

Bypasses Bot Framework auth — invokes the RAG pipeline directly for testing and debugging.

```
Request:
  Content-Type: application/json
  Body:
    {
      "query": "What is the VPN policy?",
      "conversation_id": "test-session",   // optional
      "model": "gpt-4.1",                  // optional — override deployment
      "top_k": 5,                          // optional — override chunk count
      "temperature": 0.2,                  // optional — override LLM temperature
      "filter": "source_type eq 'wiki'"    // optional — OData filter
    }

Response (200 OK):
    {
      "query": "What is the VPN policy?",
      "conversation_id": "test-session",
      "steps": {
        "cosmos_read": { "status": "ok", "history_length": 0 },
        "agent": { "status": "ok", "answer_length": 512, "source_count": 3 },
        "cosmos_write": { "status": "ok" }
      },
      "answer": "Based on the documentation [1]...",
      "sources": [ { "index": 1, "title": "VPN Policy", "url": "..." } ],
      "raw_chunks": [ ... ]
    }
```

#### `GET /starter-prompts`

Returns configured starter prompt cards for the Chainlit UI.

```
Response (200 OK):
    {
      "prompts": [
        { "label": "How do I connect to VPN?", "message": "How do I connect to VPN?" },
        { "label": "Release schedule", "message": "What is the current release schedule?" }
      ]
    }
```

---

## Agent Architecture

### Current Design — Single RAG Agent with Specialized Detection

The agent follows a **coordinator pattern**: a single orchestrator (`core/agent.py`) handles
all queries, with specialized detection modules that adjust behavior without changing the
search or generation pipeline.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  AGENT ARCHITECTURE — Current                                                │
│                                                                              │
│  ┌───────────────────────────────────────────────────────────────────────┐   │
│  │                                                                       │   │
│  │  core/agent.py — RAG Orchestrator                                     │   │
│  │                                                                       │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │   │
│  │  │                                                                 │  │   │
│  │  │  1. QUERY CLASSIFICATION                                        │  │   │
│  │  │     ├── "sttm" in query? → STTM prompt + higher top_k          │  │   │
│  │  │     └── Default: standard system prompt                         │  │   │
│  │  │                                                                 │  │   │
│  │  │  2. QUERY REWRITE (if conversation history exists)              │  │   │
│  │  │     └── LLM call: rewrite follow-up into standalone query       │  │   │
│  │  │                                                                 │  │   │
│  │  │  3. SEARCH                                                      │  │   │
│  │  │     └── core/search.py: hybrid (BM25 + vector + semantic)       │  │   │
│  │  │                                                                 │  │   │
│  │  │  4. QUALITY GATE                                                │  │   │
│  │  │     ├── Check retrieval_score_threshold                         │  │   │
│  │  │     ├── If below: refine query → retry search                   │  │   │
│  │  │     └── If still below: return out-of-scope answer              │  │   │
│  │  │                                                                 │  │   │
│  │  │  5. CONTEXT BUILDING                                            │  │   │
│  │  │     ├── Format retrieved docs with [i] source headers           │  │   │
│  │  │     ├── Extract logical paths from blob URLs                    │  │   │
│  │  │     └── Multi-source synthesis hint when >1 unique document     │  │   │
│  │  │                                                                 │  │   │
│  │  │  6. GENERATION                                                  │  │   │
│  │  │     ├── Build messages: system + history + context + query       │  │   │
│  │  │     ├── LLM call (sync or streaming)                            │  │   │
│  │  │     └── Filter cited sources from answer text                   │  │   │
│  │  │                                                                 │  │   │
│  │  │  7. POST-PROCESSING                                             │  │   │
│  │  │     └── generate_suggested_prompts() — follow-up suggestions    │  │   │
│  │  │                                                                 │  │   │
│  │  └─────────────────────────────────────────────────────────────────┘  │   │
│  │                                                                       │   │
│  └───────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  SUPPORTING MODULES                                                          │
│  ─────────────────                                                           │
│                                                                              │
│  ┌──────────────────────────────┐  ┌──────────────────────────┐             │
│  │ core/prompts.py             │  │ core/search.py           │             │
│  │                             │  │                          │             │
│  │ Loads prompts from .txt     │  │ AsyncSearchClient        │             │
│  │ files with env var override │  │  search()                │             │
│  │ (system, sttm_system,       │  │  search_document_names() │             │
│  │  query_rewrite, etc.)       │  │                          │             │
│  └──────────────────────────────┘  └──────────────────────────┘             │
│                                                                              │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────────────┐     │
│  │ config.py       │  │ key_vault.py    │  │ prompts/*.txt            │     │
│  │                 │  │                 │  │                          │     │
│  │ Pydantic        │  │ Key Vault       │  │ system.txt               │     │
│  │ Settings with   │  │ secret          │  │ sttm_system.txt          │     │
│  │ Key Vault       │  │ resolution      │  │ query_rewrite.txt        │     │
│  │ resolution      │  │ (lazy client)   │  │ query_refine.txt         │     │
│  │                 │  │                 │  │ suggested_prompts.txt    │     │
│  └─────────────────┘  └─────────────────┘  └──────────────────────────┘     │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Agent Pipeline — Detailed Flow

```
User Query
    │
    ▼
┌─────────────────────────┐
│ 1. STTM Detection       │ ── "sttm" in query.lower()?
│    Yes → STTM prompt,   │    Simple string match
│           top_k bump     │    Prompt loaded from sttm_system.txt
│    No  → default prompt  │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ 2. Query Rewrite        │ ── Has conversation_history?
│    Yes → LLM rewrites   │    Last 4 turns → standalone query
│          follow-up       │    e.g., "What about VPN?" → "What is the VPN policy?"
│    No  → use as-is      │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ 3. Hybrid Search        │ ── core/search.py
│    • BM25 keyword       │    Over-retrieves 3x, then reranks
│    • 3072d vector       │    semantic_query = original intent
│    • Semantic reranker   │    exhaustive_knn = optional
│    • include_total_count │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ 4. Quality Gate         │ ── retrieval_score_threshold > 0?
│    Score OK → proceed   │    Checks max(reranker_score, score)
│    Score low:           │
│      → Refine query     │    LLM generates refined search terms
│      → Retry search     │    semantic_query = original intent
│      → Still low →      │    Return OUT_OF_SCOPE_ANSWER
│        out-of-scope     │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ 5. Build Context        │ ── _format_context()
│    • [i] source headers │    Multi-source hint when >1 doc
│    • Logical paths      │    _extract_logical_path() from blob URLs
│    • Document names     │    search_document_names() via facets
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ 6. LLM Generation       │ ── _build_llm() → AzureChatOpenAI
│    system + history      │    Supports reasoning models (o3/o1)
│    + context + query     │    token_provider (MSI, no API key)
│    → cited answer        │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ 7. Source Citation       │ ── _filter_cited_sources()
│    Parse [1], [2] refs   │    Only return sources actually cited
│    from answer text      │    Deduplicate by file name
└────────────┬────────────┘
             ▼
        AgentResult
        { answer, sources, raw_chunks, full_prompt }
```

### Two Invocation Modes

| Function | Use Case | Behavior |
|----------|----------|----------|
| `invoke_agent()` | Bot Framework (Teams/WebChat) | Returns complete `AgentResult` after full pipeline |
| `invoke_agent_stream()` | Chainlit UI (browser) | Yields event dicts (progress), token strings (streaming), and final metadata dict |

The streaming variant emits progress events that the Chainlit UI renders as a "thinking" accordion:

| Event | Meaning |
|-------|---------|
| `rewriting_query` | Query rewrite started |
| `query_rewritten` | Query rewrite complete |
| `search_start` | Search initiated |
| `search_complete` | Search results received |
| `refining_search` | Quality gate triggered retry |
| `retry_search_complete` | Retry search results received |
| `generating` | LLM generation started |

### Dual Interface Architecture

The same agent core serves two completely different user interfaces via the `USER_INTERFACE` setting:

```
                    ┌──────────────────────────────────────────────┐
                    │                                              │
                    │         USER_INTERFACE selector               │
                    │         (web/app.py create_app())             │
                    │                                              │
                    └───────────┬──────────────────┬───────────────┘
                                │                  │
                    ┌───────────▼──────┐  ┌───────▼──────────────┐
                    │                  │  │                      │
                    │  BOT_SERVICE     │  │  CHAINLIT_UI          │
                    │                  │  │                      │
                    │  • /api/messages │  │  • /chat/* (web UI)  │
                    │  • /health       │  │  • /chat/auth/* (SSO)│
                    │  • /readiness    │  │  • SSOAuthMiddleware │
                    │                  │  │  • WebSocket streams │
                    │  Bot Framework   │  │  • Debug panels      │
                    │  Activity JSON   │  │  • Chat settings     │
                    │  → invoke_agent()│  │  • Suggested prompts │
                    │                  │  │  • invoke_agent_     │
                    │                  │  │    stream()          │
                    └────────┬─────────┘  └──────────┬───────────┘
                             │                       │
                             └───────────┬───────────┘
                                         │
                             ┌───────────▼───────────┐
                             │                       │
                             │   core/agent.py        │
                             │   (shared RAG pipeline) │
                             │                       │
                             └───────────────────────┘
```

### Future — Coordinator + Sub-Agent Expansion

The current single-agent design is structured for expansion into a multi-agent coordinator
pattern. The pipeline stages (classification → search → generate) map cleanly to specialized
sub-agents:

```
                    ┌──────────────────────────────────────────────┐
                    │                                              │
                    │          core/orchestrator.py                 │
                    │          (Future Coordinator Agent)           │
                    │                                              │
                    │  Receives user query + context               │
                    │  Classifies intent                           │
                    │  Routes to appropriate sub-agent              │
                    │  Aggregates results                           │
                    │                                              │
                    └───┬──────────┬──────────┬──────────┬────────┘
                        │          │          │          │
                ┌───────▼──────┐ ┌▼────────┐ ┌▼────────┐ ┌▼──────────┐
                │              │ │         │ │         │ │           │
                │ RAG Agent    │ │ STTM    │ │ Service │ │ DQ Agent  │
                │ (current)    │ │ Agent   │ │ Now     │ │           │
                │              │ │         │ │ Agent   │ │ Data      │
                │ General KB   │ │ Data    │ │         │ │ Quality   │
                │ questions    │ │ lineage │ │ Ticket  │ │ checks    │
                │ → AI Search  │ │ queries │ │ create/ │ │ → ADF     │
                │ → LLM        │ │ → AI    │ │ status  │ │           │
                │              │ │ Search  │ │ → REST  │ │           │
                └──────────────┘ └─────────┘ └─────────┘ └───────────┘
```

**What exists today and what's planned:**

| Component | Status | Location |
|-----------|--------|----------|
| RAG Agent (general KB search + generate) | **Implemented** | `core/agent.py` |
| STTM detection (prompt switching) | **Implemented** | `core/agent.py` (inline) |
| Query rewrite (conversational context) | **Implemented** | `core/agent.py` |
| Quality gate (retrieval score threshold) | **Implemented** | `core/agent.py` |
| Suggested follow-up prompts | **Implemented** | `core/agent.py` |
| Dual UI (Bot Service + Chainlit) | **Implemented** | `web/app.py` |
| Entra ID SSO | **Implemented** | `web/auth.py`, `web/middleware.py` |
| STTM as dedicated sub-agent | Planned | `core/agents/sttm_agent.py` |
| ServiceNow integration agent | Planned | `core/agents/servicenow_agent.py` |
| Data Quality / ADF agent | Planned | `core/agents/dq_agent.py` |
| Coordinator / router | Planned | `core/orchestrator.py` |

The expansion path requires no breaking changes — the current `invoke_agent()` and
`invoke_agent_stream()` signatures remain stable. The coordinator would call
sub-agent functions internally and return the same `AgentResult` structure.
