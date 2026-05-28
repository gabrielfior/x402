"""Tests for ERC-8004 extension schema."""

from x402.extensions.erc8004.schema import declare_erc8004_extension, erc8004_schema


def test_declare_extension() -> None:
    decl = declare_erc8004_extension(agent_id=42)
    assert decl["info"]["agentId"] == 42
    assert "schema" in decl
    assert decl["schema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_schema_structure() -> None:
    assert erc8004_schema["type"] == "object"
    assert "agentId" in erc8004_schema["properties"]
    assert erc8004_schema["properties"]["agentId"]["type"] == "integer"
    assert "agentId" in erc8004_schema["required"]
