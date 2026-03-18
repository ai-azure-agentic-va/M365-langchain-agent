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
│    │ [Microsoft Managed]    │──────►│ [AGIC on AKS]                │         │
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
│    AKS CLUSTER · namespace: <namespace>                                     │
│    ────────────────────────────────────                                      │
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
│    │  │ FastAPI App (app.py)                                     │    │       │
│    │  │                                                          │    │       │
│    │  │  POST /api/messages  ← Bot Framework Activity JSON       │    │       │
│    │  │  GET  /health        ← K8s liveness probe                │    │       │
│    │  │  GET  /readiness     ← K8s readiness probe               │    │       │
│    │  └──────────┬───────────────────────────────────────────────┘    │       │
│    │             │                                                   │       │
│    │             │ Deserializes Activity, invokes bot handler         │       │
│    │             ▼                                                   │       │
│    │  ┌──────────────────────┐    ┌──────────────────────────┐      │       │
│    │  │ Bot Framework        │    │ LangChain RAG Agent      │      │       │
│    │  │ Adapter + Handler    │───►│ (agent.py)               │      │       │
│    │  │ (bot.py)             │    │                          │      │       │
│    │  │                      │    │ 1. Search (AI Search)    │      │       │
│    │  │ Validates auth       │    │ 2. Generate (LLM)        │      │       │
│    │  │ Extracts user text   │    │ 3. Return cited answer   │      │       │
│    │  │ Sends reply back     │    │                          │      │       │
│    │  └──────────┬───────────┘    └──────────────────────────┘      │       │
│    │             │                                                   │       │
│    │             │ Loads/saves conversation history                   │       │
│    │             ▼                                                   │       │
│    │  ┌──────────────────────┐                                      │       │
│    │  │ CosmosDB Client      │                                      │       │
│    │  │ (cosmos_store.py)    │                                      │       │
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
| M365 LangChain Agent | Python 3.10, FastAPI, uvicorn | AKS pod | Receives Bot messages, runs RAG, replies |
| Application Gateway | AGIC (Azure-managed) | AKS cluster | TLS termination, path routing |
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
│  │  app.py — FastAPI Application                                       │     │
│  │  ════════════════════════════                                       │     │
│  │                                                                     │     │
│  │  Responsibilities:                                                  │     │
│  │    • HTTP server (uvicorn, port 8080)                               │     │
│  │    • POST /api/messages — Bot Framework entry point                 │     │
│  │    • GET /health, /readiness — Kubernetes probes                    │     │
│  │    • Initializes BotFrameworkAdapter with credentials                │     │
│  │    • Global error handler (on_turn_error)                           │     │
│  │                                                                     │     │
│  │  Depends on: bot.py, botbuilder-core                                │     │
│  │                                                                     │     │
│  └──────────────────────────┬──────────────────────────────────────────┘     │
│                             │                                                │
│                             │ Deserializes Activity → calls bot.on_turn()    │
│                             ▼                                                │
│  ┌─────────────────────────────────────────────────────────────────────┐     │
│  │                                                                     │     │
│  │  bot.py — DocAgentBot (ActivityHandler)                             │     │
│  │  ══════════════════════════════════════                             │     │
│  │                                                                     │     │
│  │  Responsibilities:                                                  │     │
│  │    • on_message_activity() — main message handler                   │     │
│  │    • on_members_added_activity() — welcome message                  │     │
│  │    • Sends typing indicator while processing                        │     │
│  │    • Orchestrates: load history → invoke agent → save turn → reply  │     │
│  │                                                                     │     │
│  │  Depends on: agent.py, cosmos_store.py                              │     │
│  │                                                                     │     │
│  └───────┬───────────────────────────────┬─────────────────────────────┘     │
│          │                               │                                   │
│          │ invoke_agent(query, history)   │ get_history() / save_turn()       │
│          ▼                               ▼                                   │
│  ┌─────────────────────────┐   ┌──────────────────────────────┐             │
│  │                         │   │                              │             │
│  │  agent.py               │   │  cosmos_store.py             │             │
│  │  LangChain RAG Agent    │   │  CosmosConversationStore     │             │
│  │  ═══════════════════    │   │  ════════════════════════    │             │
│  │                         │   │                              │             │
│  │  invoke_agent():        │   │  get_history():              │             │
│  │   1. Search docs        │   │   Read conversation from     │             │
│  │   2. Build context      │   │   CosmosDB by conversation_  │             │
│  │   3. Build messages     │   │   id (partition key)         │             │
│  │      (system + history  │   │                              │             │
│  │       + current query)  │   │  save_turn():                │             │
│  │   4. Call LLM           │   │   Append user+bot messages   │             │
│  │   5. Append sources     │   │   to conversation doc        │             │
│  │   6. Return answer      │   │   (max N msgs, TTL-based)    │             │
│  │                         │   │                              │             │
│  │  Depends on:            │   │  Depends on:                 │             │
│  │   search.py,            │   │   azure-cosmos SDK           │             │
│  │   langchain-openai      │   │                              │             │
│  │                         │   │  Connects to:                │             │
│  └──────────┬──────────────┘   │   Azure CosmosDB             │             │
│             │                  │   (HTTPS, API key auth)       │             │
│             │                  │                              │             │
│             │                  └──────────────────────────────┘             │
│             │                                                               │
│             │ search(query, top_k=5)                                        │
│             ▼                                                               │
│  ┌──────────────────────────────────────────────────┐                       │
│  │                                                  │                       │
│  │  utils/search.py — AzureSearchClient             │                       │
│  │  ═══════════════════════════════════             │                       │
│  │                                                  │                       │
│  │  Singleton via get_search_client()               │                       │
│  │                                                  │                       │
│  │  search(query, top_k):                           │                       │
│  │   1. Embed query → vector                        │                       │
│  │      (embedding model via Azure OpenAI)          │                       │
│  │   2. Send to Azure AI Search:                    │                       │
│  │      • search_text = query (keyword)             │                       │
│  │      • vector_queries = [VectorizedQuery]        │                       │
│  │      • query_type = "semantic"                   │                       │
│  │      • semantic_configuration from env           │                       │
│  │   3. Parse results → list of dicts:              │                       │
│  │      {content, score, reranker_score,            │                       │
│  │       document_title, source_url, ...}           │                       │
│  │                                                  │                       │
│  │  Connects to:                                    │                       │
│  │   Azure AI Search (HTTPS, API key auth)          │                       │
│  │   Azure OpenAI embeddings (HTTPS, API key auth)  │                       │
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
| 4 | App Gateway | TLS termination | HTTPS → HTTP | Forwards to pod port 8080 |
| 5 | app.py | Deserializes Activity | In-process | BotFrameworkAdapter.process_activity() |
| 6 | bot.py | Validates, extracts text | In-process | on_message_activity() |
| 7 | cosmos_store.py | Loads conversation history | HTTPS → CosmosDB | get_history(conversation_id) |
| 8 | agent.py | Embeds query | HTTPS → Azure OpenAI | Embedding model → vector |
| 9 | search.py | Hybrid search | HTTPS → Azure AI Search | keyword + vector + semantic reranking, top 5 |
| 10 | agent.py | Builds prompt | In-process | System prompt + history + docs + question |
| 11 | agent.py | Generates answer | HTTPS → Azure OpenAI | LLM model, temperature=0.2 |
| 12 | agent.py | Appends source citations | In-process | [1] title, [2] title... |
| 13 | cosmos_store.py | Saves turn | HTTPS → CosmosDB | save_turn(conversation_id, user_msg, bot_msg) |
| 14 | bot.py | Sends reply | HTTPS → Bot Service | Via serviceUrl callback in Activity |
| 15 | Bot Service | Routes to Teams | Internal | Activity response rendered as chat message |
| 16 | Employee | Sees answer | Teams UI | Grounded answer with [1], [2] citations |

---

## Deployment Topology

**Physical view — what runs where.**

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  AZURE (<region>)                                                    │
│                                                                      │
│  Resource Group: <aks-resource-group>                                 │
│  ┌────────────────────────────────────────────────────────────┐      │
│  │                                                            │      │
│  │  AKS Cluster: <cluster-name>                               │      │
│  │  Namespace: <namespace>                                    │      │
│  │                                                            │      │
│  │  ┌──────────────────────────────────────────────────┐     │      │
│  │  │ Pod: m365-langchain-agent-xxx                     │     │      │
│  │  │ Image: <acr-name>.azurecr.io/                     │     │      │
│  │  │        m365-langchain-agent:<tag>                  │     │      │
│  │  │ Port: 8000                                        │     │      │
│  │  │ CPU: 250m-500m | Memory: 512Mi-1Gi                │     │      │
│  │  └──────────────────────────────────────────────────┘     │      │
│  │                                                            │      │
│  │  Service: m365-langchain-agent (ClusterIP :8080)           │      │
│  │  Ingress: /api/messages, /health, /readiness               │      │
│  │                                                            │      │
│  │  Application Gateway (AGIC):                               │      │
│  │    FQDN: <cluster-fqdn>                                    │      │
│  │    TLS: <ssl-cert-name>                                    │      │
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
| **UserAssignedMSI** | `BOT_APP_ID` set, `BOT_APP_PASSWORD` empty | `MsiAppCredentials` | Uses `ManagedIdentityCredential` from `azure-identity` to acquire tokens from the IMDS endpoint on the AKS node |
| **App Password** | `BOT_APP_ID` set, `BOT_APP_PASSWORD` set | `MicrosoftAppCredentials` | Uses client_credentials grant with app ID + secret |
| **No Auth** | `BOT_APP_ID` empty | None | Emulator/local testing only |

**MSI outbound flow (UserAssignedMSI mode):**
```
LangChain Agent (AKS Pod)                    Azure AD                Bot Connector
    │                                           │                        │
    │  1. context.send_activity(reply)           │                        │
    │                                           │                        │
    │  2. MsiAppCredentials.get_access_token()  │                        │
    │     ManagedIdentityCredential.get_token()  │                        │
    │─────────────────────────────────────────►│                        │
    │     scope: api://botframework.com/.default │                        │
    │                                           │                        │
    │  3. Azure AD returns access token          │                        │
    │     (via IMDS on AKS node VMSS)            │                        │
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
- The User-Assigned Managed Identity must be assigned to the AKS VMSS node pool
  (not just created — the pod needs to reach the IMDS endpoint for that identity)
- The MSI's service principal must be authorized for the Bot Framework API scope
- `BOT_AUTH_TENANT` must be set in the ConfigMap

**Custom adapter (botbuilder-python limitation):**

The `botbuilder-python` SDK does not natively support `UserAssignedMSI`. The SDK's
`BotFrameworkAdapter` always creates `MicrosoftAppCredentials` (which requires `client_secret`)
for outbound calls via a name-mangled private method `__get_app_credentials`.

The application includes `MsiBotFrameworkAdapter` (in `app.py`) that:
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
| Agent → Azure OpenAI (LLM) | API Key in header (`api-key`) | K8s Secret: `AZURE_OPENAI_API_KEY` | HTTPS (public endpoint) |
| Agent → Azure OpenAI (Embeddings) | API Key in header (`api-key`) | K8s Secret: `AZURE_OPENAI_API_KEY` | HTTPS (public endpoint) |
| Agent → Azure AI Search | API Key in header (`api-key`) | K8s Secret: `AZURE_SEARCH_API_KEY` | HTTPS (public endpoint) |
| Agent → Azure CosmosDB | API Key in header | K8s Secret: `AZURE_COSMOS_KEY` | HTTPS (private endpoint) |
| Agent → LangSmith | API Key in header (`x-api-key`) | K8s Secret: `LANGSMITH_API_KEY` | HTTPS (SaaS) |
| Foundry registration script → AI Foundry | `DefaultAzureCredential` (Bearer token) | Azure AD (interactive/MSI) | HTTPS |

### ④ TLS / Transport Security

```
Internet                    App Gateway                     AKS Pod
   │                           │                               │
   │  HTTPS (TLS 1.2+)        │                               │
   │  CA-signed certificate    │   HTTP (plain, in-cluster)    │
   │  (e.g. Let's Encrypt)    │   No TLS (trusted network)    │
   │──────────────────────────►│──────────────────────────────►│
   │                           │                               │
   │  Certificate must be      │   App Gateway terminates      │
   │  trusted by Azure Bot     │   TLS and forwards as HTTP    │
   │  Service (NOT self-signed)│   to ClusterIP service        │
   │                           │   on port 8080                │
```

**Critical requirement:** Azure Bot Service **will not connect** to endpoints with self-signed
or untrusted TLS certificates. There is no error returned to the user — Bot Service silently
drops the request. The pod logs will show zero inbound POST requests.

**Certificate management:**
| Setting | Value |
|---------|-------|
| TLS Termination | App Gateway (AGIC) |
| Certificate Reference | `appgw.ingress.kubernetes.io/appgw-ssl-certificate` ingress annotation |
| Recommended CA | Let's Encrypt (free, 90-day validity, HTTP-01 challenge) |
| Renewal | Certbot with auth/cleanup hooks → convert PEM to PFX → upload to App Gateway |

### Network Security

```
┌──────────────────────────────────────────────────────────────────┐
│  AKS VNet                                                        │
│                                                                  │
│  ┌──────────────────────────┐                                    │
│  │  AKS Subnet              │                                    │
│  │                          │                                    │
│  │  Pod ──── ClusterIP ──── App Gateway ──── Internet            │
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
│  K8s Secret                  │     │  K8s ConfigMap                │
│  (m365-langchain-agent-      │     │  (m365-langchain-agent-       │
│   secrets)                   │     │   config)                     │
│                              │     │                              │
│  AZURE_OPENAI_API_KEY    ●   │     │  AZURE_OPENAI_ENDPOINT   ○   │
│  AZURE_SEARCH_API_KEY    ●   │     │  AZURE_SEARCH_ENDPOINT   ○   │
│  AZURE_COSMOS_KEY        ●   │     │  AZURE_COSMOS_ENDPOINT   ○   │
│  LANGSMITH_API_KEY       ●   │     │  BOT_APP_ID              ○   │
│  BOT_APP_PASSWORD        ●   │     │  BOT_AUTH_TENANT         ○   │
│  (empty for MSI)             │     │  AZURE_SEARCH_INDEX_NAME ○   │
│                              │     │  LOG_LEVEL               ○   │
│  ● = sensitive, encrypted    │     │  ○ = non-sensitive            │
│    at rest by K8s            │     │                              │
└──────────────────────────────┘     └──────────────────────────────┘

         │                                      │
         └──────────────┬───────────────────────┘
                        │
                        ▼
              Pod env vars (injected at startup)
              Never written to disk inside container
```

**Security rules:**
- API keys stored in K8s Secrets (encrypted at rest by etcd encryption)
- Non-sensitive config (endpoints, feature flags) in ConfigMaps
- No secrets in source code, Dockerfiles, or ConfigMaps
- `.env` files used only for local development (gitignored)
- Container runs as non-root user (Python slim base image)

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
| Pod security | Default | Add NetworkPolicy to restrict egress to required endpoints only |
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
| Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT_NAME` |
| Azure AI Search | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME` |
| CosmosDB | `AZURE_COSMOS_ENDPOINT`, `AZURE_COSMOS_KEY`, `AZURE_COSMOS_DATABASE` |
| Bot Framework | `BOT_APP_ID`, `BOT_APP_PASSWORD` |
| LangSmith | `LANGSMITH_API_KEY`, `LANGCHAIN_TRACING_V2` |
| Foundry | `AZURE_FOUNDRY_ENDPOINT`, `AZURE_FOUNDRY_SUBSCRIPTION_ID` |
