"""JSON Schema for the ERC-8004 extension declaration."""

from typing import Any

erc8004_schema: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "agentId": {
            "type": "integer",
            "minimum": 0,
        },
    },
    "required": ["agentId"],
}


def declare_erc8004_extension(agent_id: int) -> dict[str, Any]:
    """Declare the erc8004 extension for inclusion in PaymentRequired.extensions.

    Returns a dict with {info: {agentId}, schema} structure per x402 v2 spec.
    """
    return {
        "info": {"agentId": agent_id},
        "schema": erc8004_schema,
    }
