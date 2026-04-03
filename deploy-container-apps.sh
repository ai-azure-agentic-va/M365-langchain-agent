#!/usr/bin/env bash
# =============================================================================
# deploy-container-apps.sh — Deploy to Azure Container Apps
# =============================================================================
# Standalone deployment on Azure Container Apps. Reads configuration from
# environment variables or a .env file.
#
# Usage:
#   # Option 1: Set env vars then run
#   export RESOURCE_GROUP=rg-myproject-dev ACR_NAME=myprojectacr
#   ./deploy-container-apps.sh
#
#   # Option 2: Source an existing .env.deployed file
#   source .env.deployed && ./deploy-container-apps.sh
#
# Prerequisites:
#   - Azure CLI (az) authenticated
#   - ACR with the Docker image already pushed
#   - Azure OpenAI, AI Search, CosmosDB already provisioned
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Required configuration — from env vars
# ---------------------------------------------------------------------------
RESOURCE_GROUP="${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
ACR_NAME="${ACR_NAME:?Set ACR_NAME}"

# Azure OpenAI (auth via Managed Identity — no API key needed)
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:?Set AZURE_OPENAI_ENDPOINT}"
AZURE_OPENAI_DEPLOYMENT_NAME="${AZURE_OPENAI_DEPLOYMENT_NAME:-gpt-4.1}"
AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2024-05-01-preview}"
AZURE_OPENAI_EMBEDDING_DEPLOYMENT="${AZURE_OPENAI_EMBEDDING_DEPLOYMENT:-text-embedding-3-large}"

# Azure AI Search (auth via Managed Identity — no API key needed)
AZURE_SEARCH_ENDPOINT="${AZURE_SEARCH_ENDPOINT:?Set AZURE_SEARCH_ENDPOINT}"
AZURE_SEARCH_INDEX_NAME="${AZURE_SEARCH_INDEX_NAME:-nfcu-rag-index}"
AZURE_SEARCH_SEMANTIC_CONFIG_NAME="${AZURE_SEARCH_SEMANTIC_CONFIG_NAME:-custom-kb-semantic-config}"

# CosmosDB (auth via Managed Identity — no API key needed)
AZURE_COSMOS_ENDPOINT="${AZURE_COSMOS_ENDPOINT:?Set AZURE_COSMOS_ENDPOINT}"
AZURE_COSMOS_DATABASE="${AZURE_COSMOS_DATABASE:-m365-langchain-agent}"
AZURE_COSMOS_CONTAINER="${AZURE_COSMOS_CONTAINER:-conversations}"

# Bot Framework
BOT_APP_ID="${BOT_APP_ID:-}"
BOT_APP_PASSWORD="${BOT_APP_PASSWORD:-}"

# Chainlit auth (required for conversation history sidebar)
CHAINLIT_AUTH_SECRET="${CHAINLIT_AUTH_SECRET:?Set CHAINLIT_AUTH_SECRET — run: python3 -c \"import secrets; print(secrets.token_hex(32))\"}"

# Entra ID SSO (required for CHAINLIT_UI with ENABLE_SSO=true)
ENABLE_SSO="${ENABLE_SSO:-true}"
ENTRA_TENANT_ID="${ENTRA_TENANT_ID:-}"
ENTRA_CLIENT_ID="${ENTRA_CLIENT_ID:-}"
ENTRA_CLIENT_SECRET="${ENTRA_CLIENT_SECRET:-}"
SESSION_SECRET="${SESSION_SECRET:-}"
SESSION_MAX_AGE="${SESSION_MAX_AGE:-28800}"
SESSION_IDLE_TIMEOUT="${SESSION_IDLE_TIMEOUT:-900}"

# Group-based RBAC (optional)
AI_VA_USERS_GROUP_ID="${AI_VA_USERS_GROUP_ID:-}"
AI_VA_ADMINS_GROUP_ID="${AI_VA_ADMINS_GROUP_ID:-}"

# LangSmith (optional)
LANGSMITH_API_KEY="${LANGSMITH_API_KEY:-}"

# UI prompt controls (optional)
SHOW_SUGGESTED_PROMPTS="${SHOW_SUGGESTED_PROMPTS:-true}"
SHOW_STARTER_PROMPTS="${SHOW_STARTER_PROMPTS:-true}"
STARTER_PROMPTS="${STARTER_PROMPTS:-[]}"

# ---------------------------------------------------------------------------
# Optional configuration — defaults provided
# ---------------------------------------------------------------------------
LOCATION="${LOCATION:-eastus2}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
ENV_NAME="${ENV_NAME:-m365-langchain-env}"
APP_NAME="${APP_NAME:-m365-langchain-agent}"
IMAGE="${ACR_NAME}.azurecr.io/${APP_NAME}:${IMAGE_TAG}"

echo "=== Azure Container Apps Deployment ==="
echo "  Resource Group: $RESOURCE_GROUP"
echo "  Location:       $LOCATION"
echo "  ACR:            $ACR_NAME"
echo "  Image:          $IMAGE"
echo "  Environment:    $ENV_NAME"
echo "  App:            $APP_NAME"
echo ""

# Step 1: Register providers
echo "[1/5] Registering providers..."
az provider register --namespace Microsoft.App --only-show-errors 2>/dev/null || true
az provider register --namespace Microsoft.OperationalInsights --only-show-errors 2>/dev/null || true

# Step 2: Create Container Apps Environment
echo "[2/5] Creating Container Apps environment: $ENV_NAME"
az containerapp env create \
    --name "$ENV_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --only-show-errors -o none 2>/dev/null || echo "  (environment may already exist)"

# Step 3: Get ACR credentials
echo "[3/5] Getting ACR credentials..."
ACR_SERVER="${ACR_NAME}.azurecr.io"
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)

# Step 4: Create/update Container App
echo "[4/6] Creating Container App: $APP_NAME"

TRACING_ENABLED="false"
if [[ -n "$LANGSMITH_API_KEY" ]]; then
    TRACING_ENABLED="true"
fi

az containerapp create \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "$ENV_NAME" \
    --image "$IMAGE" \
    --registry-server "$ACR_SERVER" \
    --registry-username "$ACR_USERNAME" \
    --registry-password "$ACR_PASSWORD" \
    --target-port 8080 \
    --ingress external \
    --min-replicas 1 \
    --max-replicas 3 \
    --cpu 0.5 \
    --memory 1.0Gi \
    --env-vars \
        AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
        AZURE_OPENAI_DEPLOYMENT_NAME="$AZURE_OPENAI_DEPLOYMENT_NAME" \
        AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
        AZURE_OPENAI_EMBEDDING_DEPLOYMENT="$AZURE_OPENAI_EMBEDDING_DEPLOYMENT" \
        AZURE_SEARCH_ENDPOINT="$AZURE_SEARCH_ENDPOINT" \
        AZURE_SEARCH_INDEX_NAME="$AZURE_SEARCH_INDEX_NAME" \
        AZURE_SEARCH_SEMANTIC_CONFIG_NAME="$AZURE_SEARCH_SEMANTIC_CONFIG_NAME" \
        AZURE_SEARCH_EMBEDDING_FIELD="content_vector" \
        AZURE_COSMOS_ENDPOINT="$AZURE_COSMOS_ENDPOINT" \
        AZURE_COSMOS_DATABASE="$AZURE_COSMOS_DATABASE" \
        AZURE_COSMOS_CONTAINER="$AZURE_COSMOS_CONTAINER" \
        BOT_APP_ID="$BOT_APP_ID" \
        BOT_APP_PASSWORD="$BOT_APP_PASSWORD" \
        USER_INTERFACE="CHAINLIT_UI" \
        LANGCHAIN_TRACING_V2="$TRACING_ENABLED" \
        LANGCHAIN_PROJECT="m365-langchain-agent" \
        LANGSMITH_API_KEY="$LANGSMITH_API_KEY" \
        CHAINLIT_AUTH_SECRET="$CHAINLIT_AUTH_SECRET" \
        ENABLE_SSO="$ENABLE_SSO" \
        ENTRA_TENANT_ID="$ENTRA_TENANT_ID" \
        ENTRA_CLIENT_ID="$ENTRA_CLIENT_ID" \
        SESSION_MAX_AGE="$SESSION_MAX_AGE" \
        SESSION_IDLE_TIMEOUT="$SESSION_IDLE_TIMEOUT" \
        AI_VA_USERS_GROUP_ID="$AI_VA_USERS_GROUP_ID" \
        AI_VA_ADMINS_GROUP_ID="$AI_VA_ADMINS_GROUP_ID" \
        SHOW_SUGGESTED_PROMPTS="$SHOW_SUGGESTED_PROMPTS" \
        SHOW_STARTER_PROMPTS="$SHOW_STARTER_PROMPTS" \
        STARTER_PROMPTS="$STARTER_PROMPTS" \
        LOG_LEVEL="INFO" \
        PORT="8080" \
    --only-show-errors -o none

# Step 5: Add secrets and reference them as env vars
echo "[5/6] Adding secrets (ENTRA_CLIENT_SECRET, SESSION_SECRET)..."

# Add secrets to the container app
az containerapp secret set \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --secrets \
        entra-client-secret="$ENTRA_CLIENT_SECRET" \
        session-secret="$SESSION_SECRET" \
    --only-show-errors -o none

# Get the FQDN for the redirect URI
FQDN=$(az containerapp show \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query properties.configuration.ingress.fqdn -o tsv)

# Set secret-ref env vars + redirect URI (requires the FQDN)
az containerapp update \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --set-env-vars \
        ENTRA_CLIENT_SECRET=secretref:entra-client-secret \
        SESSION_SECRET=secretref:session-secret \
        ENTRA_REDIRECT_URI="https://$FQDN/chat/auth/callback" \
    --only-show-errors -o none

echo "  Secrets added and ENTRA_REDIRECT_URI set to https://$FQDN/chat/auth/callback"

# Step 6: Get the FQDN and verify
echo "[6/6] Getting Container App URL..."

echo ""
echo "=== DEPLOYMENT COMPLETE ==="
echo ""
echo "  Container App URL:  https://$FQDN"
echo "  Chat UI:            https://$FQDN/chat/"
echo "  Health:             https://$FQDN/health"
echo "  Bot Messages:       https://$FQDN/api/messages"
echo "  Test Query:         https://$FQDN/test/query  (POST)"
echo ""

# Quick health check
echo "Running health check..."
sleep 10
HEALTH=$(curl -s "https://$FQDN/health" 2>/dev/null || echo '{"status":"starting"}')
echo "  Health: $HEALTH"
echo ""
echo "Done!"
