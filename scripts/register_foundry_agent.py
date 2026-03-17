#!/usr/bin/env python3
"""One-shot script to register the agent in Azure AI Foundry.

Usage:
    python scripts/register_foundry_agent.py
    python scripts/register_foundry_agent.py --list
    python scripts/register_foundry_agent.py --delete <agent-id>
"""

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level="INFO")

from m365_langchain_agent.foundry_register import (
    register_agent,
    list_agents,
    delete_agent,
)


def main():
    parser = argparse.ArgumentParser(description="Manage Foundry agents")
    parser.add_argument("--list", action="store_true", help="List all agents")
    parser.add_argument("--delete", type=str, help="Delete an agent by ID")
    parser.add_argument("--name", type=str, default="m365-langchain-agent",
                        help="Agent name for registration")
    args = parser.parse_args()

    if args.list:
        agents = list_agents()
        if not agents:
            print("No agents found.")
            return
        for a in agents:
            print(f"  {a.get('id')}  {a.get('name')}  {a.get('model')}")
        return

    if args.delete:
        delete_agent(args.delete)
        print(f"Deleted agent: {args.delete}")
        return

    # Default: register
    result = register_agent(name=args.name)
    print(f"Agent registered: id={result.get('id')}, name={result.get('name')}")


if __name__ == "__main__":
    main()
