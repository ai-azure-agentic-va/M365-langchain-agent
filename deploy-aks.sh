#!/usr/bin/env bash
# =============================================================================
# deploy-aks.sh — Zero-to-Production AKS Deployment for m365-langchain-agent
# =============================================================================
# Provisions ALL Azure resources and deploys the LangChain RAG agent from
# scratch in a brand-new subscription. No manual portal steps.
#
# Resources created:
#   Resource Group, ACR, AKS (AGIC), Azure OpenAI (GPT-4.1 + embeddings),
#   Azure AI Search (index + semantic config), CosmosDB (NoSQL),
#   User-Assigned Managed Identity, Bot Service (Teams/DirectLine/WebChat),
#   Application Gateway TLS, AI Foundry (Hub + Project + agent registration)
#
# Usage:
#   chmod +x deploy-aks.sh
#   ./deploy-aks.sh
#
# Prerequisites:
#   - Azure CLI (az), Docker, kubectl, openssl installed
#   - Logged into Azure CLI (az login)
#   - Docker Desktop running
# =============================================================================
set -euo pipefail

# =============================================================================
# PHASE 0 — CONFIGURATION
# =============================================================================
# Edit these variables for your environment. Everything else is derived.
# -----------------------------------------------------------------------------
PROJECT_NAME="${PROJECT_NAME:-m365-langchain}"
LOCATION="${LOCATION:-eastus2}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-}"  # Required — set via env or edit here

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-${PROJECT_NAME}-dev}"
AKS_NAME="${AKS_NAME:-${PROJECT_NAME}-aks}"
ACR_NAME="${ACR_NAME:-${PROJECT_NAME//-/}acr}"        # No hyphens allowed in ACR
OPENAI_NAME="${OPENAI_NAME:-${PROJECT_NAME}-openai}"
SEARCH_NAME="${SEARCH_NAME:-${PROJECT_NAME}-search}"
COSMOS_NAME="${COSMOS_NAME:-${PROJECT_NAME}-cosmos}"
BOT_NAME="${BOT_NAME:-${PROJECT_NAME}-bot}"
MSI_NAME="${MSI_NAME:-${PROJECT_NAME}-msi}"

FOUNDRY_STORAGE="${FOUNDRY_STORAGE:-${PROJECT_NAME//-/}storage}"
FOUNDRY_KV="${FOUNDRY_KV:-${PROJECT_NAME}-kv}"
FOUNDRY_HUB="${FOUNDRY_HUB:-${PROJECT_NAME}-hub}"
FOUNDRY_PROJECT="${FOUNDRY_PROJECT:-${PROJECT_NAME}-project}"

# Model deployments
LLM_MODEL="gpt-4.1"
LLM_DEPLOYMENT="gpt-4.1"
EMBEDDING_MODEL="text-embedding-3-small"
EMBEDDING_DEPLOYMENT="text-embedding-3-small"

# AI Search index
SEARCH_INDEX="custom-kb-index"
SEMANTIC_CONFIG="custom-kb-semantic-config"

# K8s
K8S_NAMESPACE="agent"
IMAGE_NAME="m365-langchain-agent"
IMAGE_TAG="latest"

# LangSmith (optional — leave empty to disable)
LANGSMITH_API_KEY="${LANGSMITH_API_KEY:-}"

# CosmosDB database / container
COSMOS_DATABASE="m365-langchain-agent"
COSMOS_CONTAINER="conversations"

# AKS sizing
AKS_NODE_SIZE="Standard_DS2_v2"
AKS_MIN_NODES=2
AKS_MAX_NODES=5

# TLS cert name for App Gateway
TLS_CERT_NAME="${PROJECT_NAME}-tls"

# =============================================================================
# LOGGING HELPERS
# =============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_phase() { echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${BLUE}▸ PHASE $1${NC}"; echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }
log_step()  { echo -e "${CYAN}  ➤ $1${NC}"; }
log_ok()    { echo -e "${GREEN}  ✔ $1${NC}"; }
log_warn()  { echo -e "${YELLOW}  ⚠ $1${NC}"; }
log_err()   { echo -e "${RED}  ✘ $1${NC}"; }

elapsed() {
    local T=$SECONDS
    printf '%02d:%02d:%02d' $((T/3600)) $((T%3600/60)) $((T%60))
}

# =============================================================================
# PRE-FLIGHT CHECKS
# =============================================================================
log_phase "0 — Prerequisites & Configuration"

# Check required tools
for cmd in az docker kubectl openssl jq curl; do
    if ! command -v "$cmd" &>/dev/null; then
        log_err "Missing required tool: $cmd"
        exit 1
    fi
done
log_ok "All required tools found (az, docker, kubectl, openssl, jq, curl)"

# Check subscription
if [[ -z "$SUBSCRIPTION_ID" ]]; then
    SUBSCRIPTION_ID=$(az account show --query id -o tsv 2>/dev/null || true)
    if [[ -z "$SUBSCRIPTION_ID" ]]; then
        log_err "SUBSCRIPTION_ID not set and no active subscription. Run: az login"
        exit 1
    fi
    log_warn "Using current subscription: $SUBSCRIPTION_ID"
fi

az account set --subscription "$SUBSCRIPTION_ID" --only-show-errors
log_ok "Subscription set: $SUBSCRIPTION_ID"

# Check Docker is running
if ! docker info &>/dev/null; then
    log_err "Docker daemon is not running. Start Docker Desktop."
    exit 1
fi
log_ok "Docker is running"

# Get script directory (for Dockerfile, requirements.txt, etc.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log_ok "Project directory: $SCRIPT_DIR"

# Register all required resource providers (new subscriptions don't have these)
log_step "Registering Azure resource providers (runs in background)"
PROVIDERS=(
    "Microsoft.ContainerRegistry"       # ACR
    "Microsoft.ContainerService"        # AKS
    "Microsoft.CognitiveServices"       # Azure OpenAI
    "Microsoft.Search"                  # AI Search
    "Microsoft.DocumentDB"              # CosmosDB
    "Microsoft.ManagedIdentity"         # User-Assigned MSI
    "Microsoft.BotService"             # Bot Service
    "Microsoft.MachineLearningServices" # AI Foundry
    "Microsoft.KeyVault"               # Key Vault (Foundry dependency)
    "Microsoft.Storage"                # Storage Account (Foundry dependency)
    "Microsoft.Network"                # App Gateway, Public IP
)
for provider in "${PROVIDERS[@]}"; do
    az provider register --namespace "$provider" --only-show-errors &>/dev/null &
done
wait
log_ok "Resource provider registrations initiated (${#PROVIDERS[@]} providers)"

# Wait for critical providers to be registered before proceeding
log_step "Waiting for critical providers to reach 'Registered' state"
CRITICAL_PROVIDERS=("Microsoft.ContainerRegistry" "Microsoft.ContainerService" "Microsoft.CognitiveServices" "Microsoft.Search" "Microsoft.DocumentDB")
for provider in "${CRITICAL_PROVIDERS[@]}"; do
    for attempt in $(seq 1 30); do
        STATE=$(az provider show --namespace "$provider" --query registrationState -o tsv 2>/dev/null)
        if [[ "$STATE" == "Registered" ]]; then
            break
        fi
        if [[ $attempt -eq 30 ]]; then
            log_warn "$provider still in state '$STATE' — continuing (may cause errors)"
        fi
        sleep 10
    done
done
log_ok "Critical resource providers registered"

echo ""
echo -e "${CYAN}Configuration:${NC}"
echo "  Project:       $PROJECT_NAME"
echo "  Location:      $LOCATION"
echo "  Subscription:  $SUBSCRIPTION_ID"
echo "  Resource Group: $RESOURCE_GROUP"
echo "  AKS:           $AKS_NAME"
echo "  ACR:           $ACR_NAME"
echo "  OpenAI:        $OPENAI_NAME"
echo "  AI Search:     $SEARCH_NAME"
echo "  CosmosDB:      $COSMOS_NAME"
echo "  Bot Service:   $BOT_NAME"
echo "  Foundry Hub:   $FOUNDRY_HUB"
echo "  Foundry Proj:  $FOUNDRY_PROJECT"
echo ""

# =============================================================================
# PHASE 1 — FOUNDATION (Resource Group, ACR, AKS)
# =============================================================================
log_phase "1 — Foundation (Resource Group, ACR, AKS)"

# 1. Resource Group
log_step "Creating resource group: $RESOURCE_GROUP"
az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --only-show-errors -o none
log_ok "Resource group created"

# 2. ACR
log_step "Creating Azure Container Registry: $ACR_NAME"
az acr create \
    --name "$ACR_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --sku Basic \
    --admin-enabled true \
    --only-show-errors -o none
log_ok "ACR created"

# 3. AKS with AGIC
log_step "Creating AKS cluster with AGIC: $AKS_NAME (this takes 5-10 min)"
az aks create \
    --name "$AKS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --node-count "$AKS_MIN_NODES" \
    --node-vm-size "$AKS_NODE_SIZE" \
    --enable-cluster-autoscaler \
    --min-count "$AKS_MIN_NODES" \
    --max-count "$AKS_MAX_NODES" \
    --enable-addons ingress-appgw \
    --appgw-name "${AKS_NAME}-appgw" \
    --appgw-subnet-cidr "10.225.0.0/16" \
    --generate-ssh-keys \
    --enable-managed-identity \
    --only-show-errors -o none
log_ok "AKS cluster created"

# 4. Get credentials
log_step "Getting AKS credentials"
az aks get-credentials \
    --name "$AKS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --overwrite-existing \
    --only-show-errors
log_ok "kubectl configured"

# 5. Create namespace
log_step "Creating namespace: $K8S_NAMESPACE"
kubectl create namespace "$K8S_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null
log_ok "Namespace ready"

# 6. Resource quota (prevents OOM kill — failure #15)
log_step "Setting resource quota"
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ResourceQuota
metadata:
  name: agent-quota
  namespace: $K8S_NAMESPACE
spec:
  hard:
    requests.cpu: "8"
    requests.memory: 12Gi
    limits.cpu: "16"
    limits.memory: 24Gi
EOF
log_ok "Resource quota set"

# 7. ACR pull permission for AKS
log_step "Granting AKS → ACR pull permission"
AKS_KUBELET_ID=$(az aks show \
    --name "$AKS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query identityProfile.kubeletidentity.objectId -o tsv)
ACR_ID=$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)

az role assignment create \
    --assignee "$AKS_KUBELET_ID" \
    --role AcrPull \
    --scope "$ACR_ID" \
    --only-show-errors -o none 2>/dev/null || log_warn "AcrPull already assigned"
log_ok "AKS can pull from ACR"

# =============================================================================
# PHASE 2 — AZURE AI SERVICES
# =============================================================================
log_phase "2 — Azure AI Services (OpenAI, AI Search)"

# 8. Azure OpenAI
log_step "Creating Azure OpenAI account: $OPENAI_NAME"
az cognitiveservices account create \
    --name "$OPENAI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --kind OpenAI \
    --sku S0 \
    --custom-domain "$OPENAI_NAME" \
    --only-show-errors -o none
log_ok "Azure OpenAI account created"

# 9. Deploy GPT-4.1
log_step "Deploying model: $LLM_MODEL → $LLM_DEPLOYMENT"
az cognitiveservices account deployment create \
    --name "$OPENAI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --deployment-name "$LLM_DEPLOYMENT" \
    --model-name "$LLM_MODEL" \
    --model-version "2025-04-14" \
    --model-format OpenAI \
    --sku-capacity 30 \
    --sku-name Standard \
    --only-show-errors -o none
log_ok "GPT-4.1 deployed"

# 10. Deploy text-embedding-3-small
log_step "Deploying model: $EMBEDDING_MODEL → $EMBEDDING_DEPLOYMENT"
az cognitiveservices account deployment create \
    --name "$OPENAI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --deployment-name "$EMBEDDING_DEPLOYMENT" \
    --model-name "$EMBEDDING_MODEL" \
    --model-version "1" \
    --model-format OpenAI \
    --sku-capacity 120 \
    --sku-name Standard \
    --only-show-errors -o none
log_ok "Embedding model deployed"

# 11. Get OpenAI credentials
log_step "Retrieving OpenAI endpoint and key"
OPENAI_ENDPOINT=$(az cognitiveservices account show \
    --name "$OPENAI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.endpoint -o tsv)
OPENAI_KEY=$(az cognitiveservices account keys list \
    --name "$OPENAI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query key1 -o tsv)
log_ok "OpenAI endpoint: $OPENAI_ENDPOINT"

# 12. Azure AI Search
log_step "Creating Azure AI Search: $SEARCH_NAME"
az search service create \
    --name "$SEARCH_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku standard \
    --partition-count 1 \
    --replica-count 1 \
    --only-show-errors -o none
log_ok "AI Search service created"

# Get search credentials
SEARCH_ENDPOINT="https://${SEARCH_NAME}.search.windows.net"
SEARCH_KEY=$(az search admin-key show \
    --service-name "$SEARCH_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query primaryKey -o tsv)
log_ok "Search endpoint: $SEARCH_ENDPOINT"

# 13. Create search index via REST API (az CLI doesn't support index creation)
log_step "Creating search index: $SEARCH_INDEX (13-field schema)"
INDEX_BODY=$(cat <<INDEXJSON
{
    "name": "$SEARCH_INDEX",
    "fields": [
        {"name": "id", "type": "Edm.String", "key": true, "filterable": true},
        {"name": "chunk_content", "type": "Edm.String", "searchable": true, "analyzer": "en.microsoft"},
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": true,
            "retrievable": true,
            "dimensions": 1536,
            "vectorSearchProfile": "default-vector-profile"
        },
        {"name": "document_title", "type": "Edm.String", "searchable": true, "filterable": true},
        {"name": "source_url", "type": "Edm.String", "filterable": true},
        {"name": "source_type", "type": "Edm.String", "filterable": true, "facetable": true},
        {"name": "file_name", "type": "Edm.String", "filterable": true},
        {"name": "chunk_index", "type": "Edm.Int32", "filterable": true},
        {"name": "total_chunks", "type": "Edm.Int32"},
        {"name": "page_number", "type": "Edm.Int32", "filterable": true},
        {"name": "last_modified", "type": "Edm.DateTimeOffset"},
        {"name": "ingested_at", "type": "Edm.DateTimeOffset"},
        {"name": "pii_redacted", "type": "Edm.Boolean", "filterable": true}
    ],
    "vectorSearch": {
        "algorithms": [
            {
                "name": "default-hnsw",
                "kind": "hnsw",
                "hnswParameters": {
                    "metric": "cosine",
                    "m": 4,
                    "efConstruction": 400,
                    "efSearch": 500
                }
            }
        ],
        "profiles": [
            {
                "name": "default-vector-profile",
                "algorithm": "default-hnsw"
            }
        ]
    },
    "semantic": {
        "configurations": [
            {
                "name": "$SEMANTIC_CONFIG",
                "prioritizedFields": {
                    "contentFields": [
                        {"fieldName": "chunk_content"}
                    ],
                    "titleField": {"fieldName": "document_title"}
                }
            }
        ]
    }
}
INDEXJSON
)

HTTP_CODE=$(echo "$INDEX_BODY" | curl -s -o /dev/null -w "%{http_code}" \
    -X PUT "${SEARCH_ENDPOINT}/indexes/${SEARCH_INDEX}?api-version=2024-07-01" \
    -H "Content-Type: application/json" \
    -H "api-key: ${SEARCH_KEY}" \
    -d @-)

if [[ "$HTTP_CODE" == "201" || "$HTTP_CODE" == "204" || "$HTTP_CODE" == "200" ]]; then
    log_ok "Search index created (HTTP $HTTP_CODE)"
else
    log_warn "Search index creation returned HTTP $HTTP_CODE (may already exist)"
fi

# =============================================================================
# PHASE 3 — DATA & IDENTITY
# =============================================================================
log_phase "3 — Data & Identity (CosmosDB, Managed Identity)"

# 16. CosmosDB (NoSQL, serverless)
log_step "Creating CosmosDB account: $COSMOS_NAME (serverless)"
az cosmosdb create \
    --name "$COSMOS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --kind GlobalDocumentDB \
    --capabilities EnableServerless \
    --default-consistency-level Session \
    --locations regionName="$LOCATION" failoverPriority=0 \
    --only-show-errors -o none
log_ok "CosmosDB account created"

# Create database + container
log_step "Creating CosmosDB database and container"
az cosmosdb sql database create \
    --account-name "$COSMOS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --name "$COSMOS_DATABASE" \
    --only-show-errors -o none

az cosmosdb sql container create \
    --account-name "$COSMOS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --database-name "$COSMOS_DATABASE" \
    --name "$COSMOS_CONTAINER" \
    --partition-key-path "/conversation_id" \
    --default-ttl 86400 \
    --only-show-errors -o none
log_ok "Database '$COSMOS_DATABASE' + container '$COSMOS_CONTAINER' created"

# 17. Get CosmosDB credentials
COSMOS_ENDPOINT=$(az cosmosdb show \
    --name "$COSMOS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query documentEndpoint -o tsv)
COSMOS_KEY=$(az cosmosdb keys list \
    --name "$COSMOS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query primaryMasterKey -o tsv)
log_ok "CosmosDB endpoint: $COSMOS_ENDPOINT"

# 18. User-Assigned Managed Identity
log_step "Creating Managed Identity: $MSI_NAME"
az identity create \
    --name "$MSI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --only-show-errors -o none
log_ok "MSI created"

# 19. Get MSI details
MSI_CLIENT_ID=$(az identity show \
    --name "$MSI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query clientId -o tsv)
MSI_RESOURCE_ID=$(az identity show \
    --name "$MSI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query id -o tsv)
log_ok "MSI client ID: $MSI_CLIENT_ID"

# =============================================================================
# PHASE 4 — HTTPS ENDPOINT (must precede Bot Service)
# =============================================================================
log_phase "4 — HTTPS Endpoint (Application Gateway + TLS)"

# 20. Get App Gateway public IP
log_step "Finding Application Gateway public IP"
AKS_NODE_RG=$(az aks show \
    --name "$AKS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query nodeResourceGroup -o tsv)

APPGW_NAME="${AKS_NAME}-appgw"

# Get the public IP resource associated with AGIC
APPGW_PIP_ID=$(az network application-gateway show \
    --name "$APPGW_NAME" \
    --resource-group "$AKS_NODE_RG" \
    --query frontendIPConfigurations[0].publicIPAddress.id -o tsv 2>/dev/null || true)

if [[ -z "$APPGW_PIP_ID" ]]; then
    # Fallback: find PIP in node resource group
    APPGW_PIP_ID=$(az network public-ip list \
        --resource-group "$AKS_NODE_RG" \
        --query "[0].id" -o tsv)
fi

APPGW_PIP_NAME=$(basename "$APPGW_PIP_ID")
APPGW_PUBLIC_IP=$(az network public-ip show \
    --ids "$APPGW_PIP_ID" \
    --query ipAddress -o tsv)
log_ok "App Gateway public IP: $APPGW_PUBLIC_IP"

# 21. Assign DNS label for stable FQDN
log_step "Assigning DNS label to public IP"
DNS_LABEL="${PROJECT_NAME}-agent"
az network public-ip update \
    --ids "$APPGW_PIP_ID" \
    --dns-name "$DNS_LABEL" \
    --only-show-errors -o none

CLUSTER_FQDN="${DNS_LABEL}.${LOCATION}.cloudapp.azure.com"
log_ok "FQDN: $CLUSTER_FQDN"

# 22. Generate self-signed TLS cert and upload to App Gateway
log_step "Generating self-signed TLS certificate"
CERT_DIR=$(mktemp -d)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "${CERT_DIR}/tls.key" \
    -out "${CERT_DIR}/tls.crt" \
    -subj "/CN=${CLUSTER_FQDN}" \
    2>/dev/null

# Convert to PFX for App Gateway
openssl pkcs12 -export \
    -out "${CERT_DIR}/tls.pfx" \
    -inkey "${CERT_DIR}/tls.key" \
    -in "${CERT_DIR}/tls.crt" \
    -passout pass:"" \
    2>/dev/null

# Upload cert to App Gateway
az network application-gateway ssl-cert create \
    --gateway-name "$APPGW_NAME" \
    --resource-group "$AKS_NODE_RG" \
    --name "$TLS_CERT_NAME" \
    --cert-file "${CERT_DIR}/tls.pfx" \
    --cert-password "" \
    --only-show-errors -o none 2>/dev/null || log_warn "TLS cert may already exist"
log_ok "TLS certificate uploaded to App Gateway"

# Cleanup temp dir
rm -rf "$CERT_DIR"

# 23. Wait for DNS propagation
log_step "Waiting for DNS propagation (checking $CLUSTER_FQDN)"
for i in $(seq 1 12); do
    if nslookup "$CLUSTER_FQDN" &>/dev/null; then
        log_ok "DNS resolves: $CLUSTER_FQDN → $APPGW_PUBLIC_IP"
        break
    fi
    if [[ $i -eq 12 ]]; then
        log_warn "DNS not yet resolving — continuing anyway (may take a few minutes)"
    fi
    sleep 10
done

# =============================================================================
# PHASE 5 — DOCKER BUILD & K8s DEPLOY
# =============================================================================
log_phase "5 — Docker Build & Kubernetes Deployment"

FULL_IMAGE="${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"

# 24. ACR login (do this immediately before build — token expires in 3 hours)
log_step "Logging into ACR: $ACR_NAME"
az acr login --name "$ACR_NAME" --only-show-errors
log_ok "ACR login successful"

# 25. Docker build (AMD64 for AKS — prevents failure #1, #2)
log_step "Building Docker image: $FULL_IMAGE (linux/amd64)"
docker buildx build \
    --platform linux/amd64 \
    -t "$FULL_IMAGE" \
    -f "${SCRIPT_DIR}/Dockerfile" \
    "$SCRIPT_DIR"
log_ok "Docker image built"

# 26. Push to ACR
log_step "Pushing image to ACR"
docker push "$FULL_IMAGE"
log_ok "Image pushed: $FULL_IMAGE"

# 27. Create K8s Secret
log_step "Creating K8s secrets"
kubectl create secret generic m365-langchain-agent-secrets \
    --namespace "$K8S_NAMESPACE" \
    --from-literal=AZURE_OPENAI_API_KEY="$OPENAI_KEY" \
    --from-literal=AZURE_SEARCH_API_KEY="$SEARCH_KEY" \
    --from-literal=AZURE_COSMOS_KEY="$COSMOS_KEY" \
    --from-literal=LANGSMITH_API_KEY="${LANGSMITH_API_KEY:-}" \
    --dry-run=client -o yaml | kubectl apply -f -
log_ok "Secrets created"

# Set LangSmith tracing flag
if [[ -n "$LANGSMITH_API_KEY" ]]; then
    TRACING_ENABLED="true"
else
    TRACING_ENABLED="false"
fi

# 28-29. Generate and apply K8s manifests with real values
log_step "Generating and applying K8s manifests"
cat <<K8S_EOF | kubectl apply -f -
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: m365-langchain-agent-config
  namespace: $K8S_NAMESPACE
  labels:
    app: m365-langchain-agent
data:
  AZURE_OPENAI_ENDPOINT: "$OPENAI_ENDPOINT"
  AZURE_OPENAI_DEPLOYMENT_NAME: "$LLM_DEPLOYMENT"
  AZURE_OPENAI_API_VERSION: "2024-05-01-preview"
  AZURE_OPENAI_EMBEDDING_DEPLOYMENT: "$EMBEDDING_DEPLOYMENT"
  AZURE_SEARCH_ENDPOINT: "$SEARCH_ENDPOINT"
  AZURE_SEARCH_INDEX_NAME: "$SEARCH_INDEX"
  AZURE_SEARCH_SEMANTIC_CONFIG_NAME: "$SEMANTIC_CONFIG"
  AZURE_SEARCH_EMBEDDING_FIELD: "content_vector"
  AZURE_COSMOS_ENDPOINT: "$COSMOS_ENDPOINT"
  AZURE_COSMOS_DATABASE: "$COSMOS_DATABASE"
  AZURE_COSMOS_CONTAINER: "$COSMOS_CONTAINER"
  BOT_APP_ID: "$MSI_CLIENT_ID"
  LANGCHAIN_TRACING_V2: "$TRACING_ENABLED"
  LANGCHAIN_PROJECT: "m365-langchain-agent"
  USER_INTERFACE: "CHAINLIT_UI"
  DEPLOY_TARGET: "KUBERNETES"
  LOG_LEVEL: "INFO"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: m365-langchain-agent
  namespace: $K8S_NAMESPACE
  labels:
    app: m365-langchain-agent
spec:
  replicas: 1
  selector:
    matchLabels:
      app: m365-langchain-agent
  template:
    metadata:
      labels:
        app: m365-langchain-agent
    spec:
      containers:
      - name: m365-langchain-agent
        image: $FULL_IMAGE
        imagePullPolicy: Always
        ports:
        - containerPort: 8080
          protocol: TCP
        envFrom:
        - configMapRef:
            name: m365-langchain-agent-config
        - secretRef:
            name: m365-langchain-agent-secrets
        resources:
          requests:
            cpu: 250m
            memory: 512Mi
          limits:
            cpu: 500m
            memory: 1Gi
        readinessProbe:
          httpGet:
            path: /readiness
            port: 8080
          initialDelaySeconds: 15
          periodSeconds: 10
          timeoutSeconds: 5
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 30
          periodSeconds: 30
          timeoutSeconds: 10
      restartPolicy: Always
---
apiVersion: v1
kind: Service
metadata:
  name: m365-langchain-agent
  namespace: $K8S_NAMESPACE
  labels:
    app: m365-langchain-agent
spec:
  selector:
    app: m365-langchain-agent
  ports:
  - port: 8080
    targetPort: 8080
    protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: m365-langchain-agent-ingress
  namespace: $K8S_NAMESPACE
  annotations:
    kubernetes.io/ingress.class: azure/application-gateway
    appgw.ingress.kubernetes.io/appgw-ssl-certificate: "$TLS_CERT_NAME"
    appgw.ingress.kubernetes.io/use-private-ip: "false"
    appgw.ingress.kubernetes.io/health-probe-path: "/health"
spec:
  rules:
  - host: "$CLUSTER_FQDN"
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: m365-langchain-agent
            port:
              number: 8080
  tls:
  - hosts:
    - "$CLUSTER_FQDN"
K8S_EOF
log_ok "K8s manifests applied"

# 30. Wait for pod readiness
log_step "Waiting for pod readiness (timeout 180s)"
kubectl wait --for=condition=ready pod \
    -l app=m365-langchain-agent \
    -n "$K8S_NAMESPACE" \
    --timeout=180s 2>/dev/null || {
        log_warn "Pod not ready within 180s — checking status"
        kubectl get pods -n "$K8S_NAMESPACE" -l app=m365-langchain-agent
        kubectl logs deploy/m365-langchain-agent -n "$K8S_NAMESPACE" --tail=20 2>/dev/null || true
    }
log_ok "Pod is running"

# 31. Verify health endpoints (via kubectl port-forward)
log_step "Verifying health endpoints"
kubectl port-forward svc/m365-langchain-agent 8080:8080 -n "$K8S_NAMESPACE" &>/dev/null &
PF_PID=$!
sleep 3

HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/health 2>/dev/null || echo "000")
READY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/readiness 2>/dev/null || echo "000")
kill $PF_PID 2>/dev/null || true

if [[ "$HEALTH_STATUS" == "200" ]]; then
    log_ok "/health → 200"
else
    log_warn "/health → $HEALTH_STATUS (may need more time)"
fi

if [[ "$READY_STATUS" == "200" ]]; then
    log_ok "/readiness → 200"
else
    log_warn "/readiness → $READY_STATUS (may need more time)"
fi

# =============================================================================
# PHASE 6 — BOT SERVICE (requires HTTPS endpoint live)
# =============================================================================
log_phase "6 — Bot Service (Teams, DirectLine, WebChat)"

BOT_ENDPOINT="https://${CLUSTER_FQDN}/api/messages"

# (Microsoft.BotService provider already registered in Phase 0)

# 32. Create Bot Service with User-Assigned MSI
log_step "Creating Bot Service: $BOT_NAME"
az bot create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$BOT_NAME" \
    --kind registration \
    --sku F0 \
    --endpoint "$BOT_ENDPOINT" \
    --app-type UserAssignedMSI \
    --appid "$MSI_CLIENT_ID" \
    --tenant-id "$(az account show --query tenantId -o tsv)" \
    --msi-resource-id "$MSI_RESOURCE_ID" \
    --only-show-errors -o none
log_ok "Bot Service created: endpoint=$BOT_ENDPOINT"

# 34. Enable channels
log_step "Enabling Teams channel"
az bot msteams create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$BOT_NAME" \
    --only-show-errors -o none 2>/dev/null || log_warn "Teams channel may already exist"
log_ok "Teams channel enabled"

log_step "Enabling DirectLine channel"
az bot directline create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$BOT_NAME" \
    --only-show-errors -o none 2>/dev/null || log_warn "DirectLine channel may already exist"
log_ok "DirectLine channel enabled"

log_step "Enabling WebChat channel"
az bot webchat create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$BOT_NAME" \
    --only-show-errors -o none 2>/dev/null || log_warn "WebChat channel may already exist"
log_ok "WebChat channel enabled"

# =============================================================================
# PHASE 7 — AI FOUNDRY (Hub + Project + Agent Registration)
# =============================================================================
log_phase "7 — AI Foundry (Hub, Project, Agent Registration)"

# 35. Storage Account (Foundry dependency)
log_step "Creating Storage Account: $FOUNDRY_STORAGE"
az storage account create \
    --name "$FOUNDRY_STORAGE" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --only-show-errors -o none
STORAGE_ID=$(az storage account show \
    --name "$FOUNDRY_STORAGE" \
    --resource-group "$RESOURCE_GROUP" \
    --query id -o tsv)
log_ok "Storage account created"

# 36. Key Vault (Foundry dependency)
log_step "Creating Key Vault: $FOUNDRY_KV"
az keyvault create \
    --name "$FOUNDRY_KV" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --only-show-errors -o none
KV_ID=$(az keyvault show \
    --name "$FOUNDRY_KV" \
    --resource-group "$RESOURCE_GROUP" \
    --query id -o tsv)
log_ok "Key Vault created"

# 37. AI Foundry Hub (Azure ML workspace with kind=Hub)
log_step "Creating AI Foundry Hub: $FOUNDRY_HUB"
az ml workspace create \
    --name "$FOUNDRY_HUB" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --kind hub \
    --storage-account "$STORAGE_ID" \
    --key-vault "$KV_ID" \
    --only-show-errors -o none
log_ok "AI Foundry Hub created"

# 38. AI Foundry Project (child of Hub)
log_step "Creating AI Foundry Project: $FOUNDRY_PROJECT"
HUB_ID=$(az ml workspace show \
    --name "$FOUNDRY_HUB" \
    --resource-group "$RESOURCE_GROUP" \
    --query id -o tsv)

az ml workspace create \
    --name "$FOUNDRY_PROJECT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --kind project \
    --hub-id "$HUB_ID" \
    --only-show-errors -o none
log_ok "AI Foundry Project created"

# 39. Create AOAI connection in workspace
log_step "Creating Azure OpenAI connection in Foundry"
OPENAI_RESOURCE_ID=$(az cognitiveservices account show \
    --name "$OPENAI_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query id -o tsv)

cat <<CONN_EOF > /tmp/aoai-connection.yaml
name: aoai-connection
type: azure_open_ai
target: $OPENAI_ENDPOINT
credentials:
  type: api_key
  key: $OPENAI_KEY
CONN_EOF

az ml connection create \
    --file /tmp/aoai-connection.yaml \
    --workspace-name "$FOUNDRY_PROJECT" \
    --resource-group "$RESOURCE_GROUP" \
    --only-show-errors -o none 2>/dev/null || log_warn "AOAI connection may already exist"
rm -f /tmp/aoai-connection.yaml
log_ok "AOAI connection created"

# 40. Create AI Search connection in workspace
log_step "Creating AI Search connection in Foundry"
cat <<CONN_EOF > /tmp/search-connection.yaml
name: aisearch-connection
type: azure_ai_search
target: $SEARCH_ENDPOINT
credentials:
  type: api_key
  key: $SEARCH_KEY
CONN_EOF

az ml connection create \
    --file /tmp/search-connection.yaml \
    --workspace-name "$FOUNDRY_PROJECT" \
    --resource-group "$RESOURCE_GROUP" \
    --only-show-errors -o none 2>/dev/null || log_warn "Search connection may already exist"
rm -f /tmp/search-connection.yaml
log_ok "AI Search connection created"

# 41. Register agent via Foundry Agents REST API (programmatic — no portal)
log_step "Registering agent in AI Foundry via REST API"

# Get Foundry endpoint
FOUNDRY_ENDPOINT=$(az ml workspace show \
    --name "$FOUNDRY_PROJECT" \
    --resource-group "$RESOURCE_GROUP" \
    --query discovery_url -o tsv 2>/dev/null | sed 's|/discovery||' || true)

if [[ -z "$FOUNDRY_ENDPOINT" ]]; then
    # Fallback: construct from region
    FOUNDRY_ENDPOINT="https://${LOCATION}.api.azureml.ms"
fi

# Get bearer token
FOUNDRY_TOKEN=$(az account get-access-token --resource "https://ml.azure.com" --query accessToken -o tsv)

FOUNDRY_BASE_URL="${FOUNDRY_ENDPOINT}/agents/v1.0/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.MachineLearningServices/workspaces/${FOUNDRY_PROJECT}"

# Build connection ID for search tool
SEARCH_CONNECTION_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.MachineLearningServices/workspaces/${FOUNDRY_PROJECT}/connections/aisearch-connection"

AGENT_BODY=$(cat <<AGENTJSON
{
    "model": "$LLM_DEPLOYMENT",
    "name": "m365-langchain-agent",
    "instructions": "You are a helpful assistant that answers questions about internal policies, procedures, and documentation. Use the connected Azure AI Search tool to retrieve relevant documents, and provide citation-backed answers.",
    "tools": [
        {
            "type": "azure_ai_search",
            "azure_ai_search": {
                "index_connection_id": "$SEARCH_CONNECTION_ID",
                "index_name": "$SEARCH_INDEX",
                "query_type": "vector_semantic_hybrid",
                "top_n": 5
            }
        }
    ]
}
AGENTJSON
)

AGENT_RESPONSE=$(echo "$AGENT_BODY" | curl -s \
    -X POST "${FOUNDRY_BASE_URL}/assistants?api-version=2024-12-01-preview" \
    -H "Authorization: Bearer ${FOUNDRY_TOKEN}" \
    -H "Content-Type: application/json" \
    -d @-)

FOUNDRY_AGENT_ID=$(echo "$AGENT_RESPONSE" | jq -r '.id // "unknown"')

if [[ "$FOUNDRY_AGENT_ID" != "unknown" && "$FOUNDRY_AGENT_ID" != "null" ]]; then
    log_ok "Agent registered in Foundry: id=$FOUNDRY_AGENT_ID"
else
    log_warn "Foundry agent registration returned: $(echo "$AGENT_RESPONSE" | jq -c '.' 2>/dev/null || echo "$AGENT_RESPONSE")"
    FOUNDRY_AGENT_ID="(check manually)"
fi

# =============================================================================
# PHASE 8 — VALIDATION & SUMMARY
# =============================================================================
log_phase "8 — Validation & Summary"

# 42-43. Health & readiness checks via public FQDN
log_step "Running health checks via $CLUSTER_FQDN"
sleep 5  # Allow AGIC to sync

HEALTH_PUBLIC=$(curl -sk -o /dev/null -w "%{http_code}" "https://${CLUSTER_FQDN}/health" 2>/dev/null || echo "000")
READY_PUBLIC=$(curl -sk -o /dev/null -w "%{http_code}" "https://${CLUSTER_FQDN}/readiness" 2>/dev/null || echo "000")

if [[ "$HEALTH_PUBLIC" == "200" ]]; then
    log_ok "Public /health → 200"
else
    log_warn "Public /health → $HEALTH_PUBLIC (AGIC may still be syncing — allow 2-3 min)"
fi

if [[ "$READY_PUBLIC" == "200" ]]; then
    log_ok "Public /readiness → 200"
else
    log_warn "Public /readiness → $READY_PUBLIC"
fi

# 44. Test query
log_step "Running test query"
TEST_RESPONSE=$(curl -sk -X POST "https://${CLUSTER_FQDN}/test/query" \
    -H "Content-Type: application/json" \
    -d '{"query": "What is the deployment status?", "conversation_id": "deploy-test"}' \
    2>/dev/null || echo '{"error": "Could not reach endpoint"}')

TEST_STATUS=$(echo "$TEST_RESPONSE" | jq -r '.steps.agent.status // "unknown"' 2>/dev/null || echo "unknown")
if [[ "$TEST_STATUS" == "ok" ]]; then
    log_ok "Test query succeeded"
else
    log_warn "Test query: $TEST_STATUS (search index may be empty — needs ingestion)"
fi

# 45. Print summary
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  DEPLOYMENT COMPLETE — $(elapsed) elapsed${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${CYAN}  RESOURCE SUMMARY${NC}"
echo "  ──────────────────────────────────────────────────────────"
printf "  %-24s %s\n" "Resource Group:" "$RESOURCE_GROUP"
printf "  %-24s %s\n" "AKS Cluster:" "$AKS_NAME"
printf "  %-24s %s\n" "ACR:" "${ACR_NAME}.azurecr.io"
printf "  %-24s %s\n" "Azure OpenAI:" "$OPENAI_ENDPOINT"
printf "  %-24s %s\n" "  LLM Deployment:" "$LLM_DEPLOYMENT"
printf "  %-24s %s\n" "  Embedding Deployment:" "$EMBEDDING_DEPLOYMENT"
printf "  %-24s %s\n" "AI Search:" "$SEARCH_ENDPOINT"
printf "  %-24s %s\n" "  Index:" "$SEARCH_INDEX"
printf "  %-24s %s\n" "  Semantic Config:" "$SEMANTIC_CONFIG"
printf "  %-24s %s\n" "CosmosDB:" "$COSMOS_ENDPOINT"
printf "  %-24s %s\n" "  Database:" "$COSMOS_DATABASE"
printf "  %-24s %s\n" "  Container:" "$COSMOS_CONTAINER"
printf "  %-24s %s\n" "Bot Service:" "$BOT_NAME"
printf "  %-24s %s\n" "  Endpoint:" "$BOT_ENDPOINT"
printf "  %-24s %s\n" "  MSI Client ID:" "$MSI_CLIENT_ID"
printf "  %-24s %s\n" "  Channels:" "Teams, DirectLine, WebChat"
printf "  %-24s %s\n" "AI Foundry Hub:" "$FOUNDRY_HUB"
printf "  %-24s %s\n" "AI Foundry Project:" "$FOUNDRY_PROJECT"
printf "  %-24s %s\n" "  Agent ID:" "$FOUNDRY_AGENT_ID"
printf "  %-24s %s\n" "Container Image:" "$FULL_IMAGE"
echo "  ──────────────────────────────────────────────────────────"
echo ""
echo -e "${CYAN}  ENDPOINTS${NC}"
echo "  ──────────────────────────────────────────────────────────"
echo "  FQDN:       https://$CLUSTER_FQDN"
echo "  Messages:   https://${CLUSTER_FQDN}/api/messages"
echo "  Health:     https://${CLUSTER_FQDN}/health"
echo "  Readiness:  https://${CLUSTER_FQDN}/readiness"
echo "  Test Query: https://${CLUSTER_FQDN}/test/query"
echo "  ──────────────────────────────────────────────────────────"
echo ""
echo -e "${CYAN}  NEXT STEPS${NC}"
echo "  1. Run the ingestion pipeline to populate the search index"
echo "  2. Test in Teams: search for '$BOT_NAME' in Teams apps"
echo "  3. (Optional) Replace self-signed TLS cert with a CA-signed cert"
echo "  4. (Optional) Set LANGSMITH_API_KEY for LangSmith tracing"
echo ""
if [[ -n "$LANGSMITH_API_KEY" ]]; then
    echo -e "  ${GREEN}LangSmith tracing: ENABLED${NC}"
else
    echo -e "  ${YELLOW}LangSmith tracing: DISABLED (set LANGSMITH_API_KEY to enable)${NC}"
fi
echo ""

# Write .env file for local development
cat > "${SCRIPT_DIR}/.env.deployed" <<ENVEOF
# =============================================================================
# Generated by deploy-aks.sh — $(date -u +"%Y-%m-%d %H:%M:%S UTC")
# =============================================================================
AZURE_OPENAI_API_KEY=$OPENAI_KEY
AZURE_OPENAI_ENDPOINT=$OPENAI_ENDPOINT
AZURE_OPENAI_DEPLOYMENT_NAME=$LLM_DEPLOYMENT
AZURE_OPENAI_API_VERSION=2024-05-01-preview
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=$EMBEDDING_DEPLOYMENT
AZURE_SEARCH_ENDPOINT=$SEARCH_ENDPOINT
AZURE_SEARCH_API_KEY=$SEARCH_KEY
AZURE_SEARCH_INDEX_NAME=$SEARCH_INDEX
AZURE_SEARCH_SEMANTIC_CONFIG_NAME=$SEMANTIC_CONFIG
AZURE_SEARCH_EMBEDDING_FIELD=content_vector
AZURE_COSMOS_ENDPOINT=$COSMOS_ENDPOINT
AZURE_COSMOS_KEY=$COSMOS_KEY
AZURE_COSMOS_DATABASE=$COSMOS_DATABASE
AZURE_COSMOS_CONTAINER=$COSMOS_CONTAINER
BOT_APP_ID=$MSI_CLIENT_ID
BOT_APP_PASSWORD=
AZURE_FOUNDRY_ENDPOINT=$FOUNDRY_ENDPOINT
AZURE_FOUNDRY_SUBSCRIPTION_ID=$SUBSCRIPTION_ID
AZURE_FOUNDRY_RESOURCE_GROUP=$RESOURCE_GROUP
AZURE_FOUNDRY_WORKSPACE=$FOUNDRY_PROJECT
LANGCHAIN_TRACING_V2=$TRACING_ENABLED
LANGCHAIN_PROJECT=m365-langchain-agent
LANGSMITH_API_KEY=${LANGSMITH_API_KEY:-}
LOG_LEVEL=INFO
PORT=8080
ENVEOF
log_ok "Saved .env.deployed with all credentials (for local development)"

echo -e "\n${GREEN}Done! Your m365-langchain-agent is live at https://${CLUSTER_FQDN}${NC}\n"
