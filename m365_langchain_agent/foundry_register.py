"""Programmatic Azure AI Foundry agent registration.

Registers this agent in Azure AI Foundry using the Agents REST API,
with Azure AI Search as a connected tool. This enables the agent to be
published to M365 Copilot and Teams through Foundry.

Usage:
    python -m m365_langchain_agent.foundry_register

Or via the script:
    python scripts/register_foundry_agent.py
"""

import logging
import os

import requests
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)


def get_foundry_base_url() -> str:
    """Build the Foundry Agents API base URL from env vars."""
    endpoint = os.environ["AZURE_FOUNDRY_ENDPOINT"]
    subscription_id = os.environ["AZURE_FOUNDRY_SUBSCRIPTION_ID"]
    resource_group = os.environ["AZURE_FOUNDRY_RESOURCE_GROUP"]
    workspace = os.environ["AZURE_FOUNDRY_WORKSPACE"]

    return (
        f"{endpoint}/agents/v1.0/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.MachineLearningServices"
        f"/workspaces/{workspace}"
    )


def get_auth_token() -> str:
    """Get a bearer token for the Azure ML API."""
    credential = DefaultAzureCredential()
    token = credential.get_token("https://ml.azure.com/.default")
    return token.token


def register_agent(
    name: str = "m365-langchain-agent",
    instructions: str = (
        "You are a helpful assistant that answers questions about internal "
        "policies, procedures, and documentation. Use the connected Azure AI Search "
        "tool to retrieve relevant documents, and provide citation-backed answers."
    ),
    model: str = "gpt-4.1",
) -> dict:
    """Register an agent in Azure AI Foundry with Azure AI Search as a tool.

    Args:
        name: Display name for the agent.
        instructions: System instructions for the agent.
        model: The model deployment name.

    Returns:
        The API response dict with the created agent details.
    """
    base_url = get_foundry_base_url()
    token = get_auth_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Agent creation payload
    payload = {
        "model": model,
        "name": name,
        "instructions": instructions,
        "tools": [
            {
                "type": "azure_ai_search",
                "azure_ai_search": {
                    "index_connection_id": (
                        f"/subscriptions/{os.environ['AZURE_FOUNDRY_SUBSCRIPTION_ID']}"
                        f"/resourceGroups/{os.environ['AZURE_FOUNDRY_RESOURCE_GROUP']}"
                        f"/providers/Microsoft.MachineLearningServices"
                        f"/workspaces/{os.environ['AZURE_FOUNDRY_WORKSPACE']}"
                        f"/connections/{os.environ.get('AZURE_FOUNDRY_SEARCH_CONNECTION', 'aisearch-connection')}"
                    ),
                    "index_name": os.environ["AZURE_SEARCH_INDEX_NAME"],
                    "query_type": "vector_semantic_hybrid",
                    "top_n": 5,
                },
            }
        ],
    }

    url = f"{base_url}/assistants?api-version=2024-12-01-preview"
    logger.info(f"[Foundry] Registering agent: name={name}, url={url}")

    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    result = response.json()
    logger.info(f"[Foundry] Agent registered: id={result.get('id')}, name={result.get('name')}")
    return result


def list_agents() -> list:
    """List all registered agents in the Foundry workspace."""
    base_url = get_foundry_base_url()
    token = get_auth_token()

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{base_url}/assistants?api-version=2024-12-01-preview"

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    agents = response.json().get("data", [])
    logger.info(f"[Foundry] Found {len(agents)} agents")
    return agents


def delete_agent(agent_id: str) -> None:
    """Delete an agent by ID."""
    base_url = get_foundry_base_url()
    token = get_auth_token()

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{base_url}/assistants/{agent_id}?api-version=2024-12-01-preview"

    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    logger.info(f"[Foundry] Deleted agent: id={agent_id}")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level="INFO")

    print("Registering agent in Azure AI Foundry...")
    result = register_agent()
    print(f"Agent registered successfully!")
    print(f"  ID:    {result.get('id')}")
    print(f"  Name:  {result.get('name')}")
    print(f"  Model: {result.get('model')}")
