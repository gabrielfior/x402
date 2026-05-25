// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IReputationRegistry} from "./interfaces/IReputationRegistry.sol";
import {IIdentityRegistry} from "./interfaces/IIdentityRegistry.sol";

struct FeedbackParams {
    uint256 agentId;
    int128 value;
    uint8 valueDecimals;
    string tag1;
    string tag2;
    string endpoint;
    string feedbackURI;
    bytes32 feedbackHash;
}

struct FeedbackTicket {
    bytes32 settlementTxHash;
    address payer;
    uint256 agentId;
    uint256 nonce;
    bytes signature;
}

contract FeedbackGateway {
    address public immutable identityRegistry;
    mapping(uint256 => bool) public usedNonces;

    event FeedbackSubmitted(
        uint256 indexed agentId,
        address indexed client,
        bytes32 indexed settlementTxHash
    );

    error InvalidTicket();
    error NonceUsed(uint256 nonce);
    error PayerMismatch();

    constructor(address _identityRegistry) {
        identityRegistry = _identityRegistry;
    }

    function submitFeedback(
        address registry,
        FeedbackParams calldata params,
        FeedbackTicket calldata ticket
    ) external {
        if (ticket.payer != msg.sender) {
            revert PayerMismatch();
        }

        if (usedNonces[ticket.nonce]) {
            revert NonceUsed(ticket.nonce);
        }

        address authority = IIdentityRegistry(identityRegistry).ownerOf(ticket.agentId);

        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                block.chainid,
                ticket.settlementTxHash,
                ticket.payer,
                ticket.agentId,
                ticket.nonce
            )
        );

        address signer = _recoverSigner(digest, ticket.signature);
        if (signer != authority) {
            revert InvalidTicket();
        }

        usedNonces[ticket.nonce] = true;

        IReputationRegistry(registry).giveFeedback(
            params.agentId,
            params.value,
            params.valueDecimals,
            params.tag1,
            params.tag2,
            params.endpoint,
            params.feedbackURI,
            bytes32(0)
        );

        emit FeedbackSubmitted(params.agentId, msg.sender, ticket.settlementTxHash);
    }

    function _recoverSigner(bytes32 digest, bytes memory signature) internal pure returns (address) {
        require(signature.length == 65, "invalid sig length");
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := mload(add(signature, 32))
            s := mload(add(signature, 64))
            v := byte(0, mload(add(signature, 96)))
        }
        return ecrecover(digest, v, r, s);
    }
}
