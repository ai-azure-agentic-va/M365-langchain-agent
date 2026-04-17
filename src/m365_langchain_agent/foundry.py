"""Azure AI Foundry agent registration (run via: python -m m365_langchain_agent.foundry)."""

import logging

import requests

from m365_langchain_agent.config import settings, credential

logger = logging.getLogger(__name__)

API_VERSION = "2024-12-01-preview"


def _get_base_url() -> str:
    return (
        f"{settings.azure_foundry_endpoint}/agents/v1.0"
        f"/subscriptions/{settings.azure_foundry_subscription_id}"
        f"/resourceGroups/{settings.azure_foundry_resource_group}"
        f"/providers/Microsoft.MachineLearningServices"
        f"/workspaces/{settings.azure_foundry_workspace}"
    )


def _get_auth_headers() -> dict[str, str]:
    token = credential.get_token("https://ml.azure.com/.default")
    return {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json",
    }


def register_agent(
    name: str = "m365-langchain-agent",
    instructions: str = (
        "You are a helpful assistant that answers questions about internal "
        "policies, procedures, and documentation. Use the connected Azure AI Search "
        "tool to retrieve relevant documents, and provide citation-backed answers."
    ),
    model: str = "gpt-4.1",
) -> dict:
    base_url = _get_base_url()
    headers = _get_auth_headers()

    connection_id = (
        f"/subscriptions/{settings.azure_foundry_subscription_id}"
        f"/resourceGroups/{settings.azure_foundry_resource_group}"
        f"/providers/Microsoft.MachineLearningServices"
        f"/workspaces/{settings.azure_foundry_workspace}"
        f"/connections/{settings.azure_foundry_search_connection}"
    )

    payload = {
        "model": model,
        "name": name,
        "instructions": instructions,
        "tools": [
            {
                "type": "azure_ai_search",
                "azure_ai_search": {
                    "index_connection_id": connection_id,
                    "index_name": settings.azure_search_index_name,
                    "query_type": "vector_semantic_hybrid",
                    "top_n": 5,
                },
            }
        ],
    }

    url = f"{base_url}/assistants?api-version={API_VERSION}"
    logger.info("Registering agent: name=%s", name)

    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    result = response.json()
    logger.info("Agent registered: id=%s, name=%s", result.get("id"), result.get("name"))
    return result


def list_agents() -> list:
    base_url = _get_base_url()
    headers = _get_auth_headers()

    url = f"{base_url}/assistants?api-version={API_VERSION}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    agents = response.json().get("data", [])
    logger.info("Found %d agents", len(agents))
    return agents


def delete_agent(agent_id: str) -> None:
    base_url = _get_base_url()
    headers = _get_auth_headers()

    url = f"{base_url}/assistants/{agent_id}?api-version={API_VERSION}"
    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    logger.info("Deleted agent: id=%s", agent_id)


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
