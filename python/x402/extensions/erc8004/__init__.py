"""ERC-8004 Feedback Extension for x402 Python SDK."""

from x402.extensions.erc8004.client import (
    ERC8004ClientExtension,
    ERCFeedbackClient,
    echo_erc8004_in_payment_payload,
    extract_erc8004_info,
)
from x402.extensions.erc8004.schema import declare_erc8004_extension, erc8004_schema
from x402.extensions.erc8004.server import create_erc8004_resource_server_extension
from x402.extensions.erc8004.types import (
    ERC8004Config,
    ERC8004ExtensionDeclaration,
    ERC8004ExtensionInfo,
    EXTENSION_KEY,
    FeedbackParams,
    FeedbackTicket,
)

__all__ = [
    "create_erc8004_resource_server_extension",
    "ERCFeedbackClient",
    "ERC8004ClientExtension",
    "echo_erc8004_in_payment_payload",
    "extract_erc8004_info",
    "declare_erc8004_extension",
    "erc8004_schema",
    "ERC8004Config",
    "ERC8004ExtensionDeclaration",
    "ERC8004ExtensionInfo",
    "EXTENSION_KEY",
    "FeedbackParams",
    "FeedbackTicket",
]
