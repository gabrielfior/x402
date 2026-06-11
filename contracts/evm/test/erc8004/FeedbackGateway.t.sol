// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {FeedbackGateway} from "../../src/erc8004/FeedbackGateway.sol";
import {IFeedbackGateway} from "../../src/erc8004/interfaces/IFeedbackGateway.sol";
import {ISignatureTransfer} from "../../src/interfaces/ISignatureTransfer.sol";
import {x402ExactPermit2Proxy} from "../../src/x402ExactPermit2Proxy.sol";
import {MockERC20} from "../mocks/MockERC20.sol";
import {MockERC3009Token} from "../mocks/MockERC3009Token.sol";
import {MockPermit2} from "../mocks/MockPermit2.sol";
import {MockIdentityRegistry} from "./mocks/MockIdentityRegistry.sol";
import {MockReputationRegistry} from "./mocks/MockReputationRegistry.sol";

/// @dev Exercises the merged FeedbackGateway: settle/mint, self-paid + sponsored feedback,
///      and client-signed revocation. The registry records `msg.sender == gateway` as author.
contract FeedbackGatewayTest is Test {
    FeedbackGateway public gateway;
    MockReputationRegistry public registry;
    MockIdentityRegistry public identity;
    MockERC20 public token;
    MockERC3009Token public t3009;

    address public owner = makeAddr("owner");
    address public payTo = makeAddr("payTo");
    address public relayer = makeAddr("relayer");

    uint256 internal payerPk = 0xC11E27;
    address public payer;

    uint256 public constant AGENT_ID = 7;
    uint256 private _nextNonce;

    function setUp() public {
        payer = vm.addr(payerPk);

        identity = new MockIdentityRegistry();
        identity.setOwner(AGENT_ID, payTo); // pay_to == agent owner, so mint-time binding holds
        registry = new MockReputationRegistry();

        gateway = new FeedbackGateway(owner, address(0), address(identity), address(registry));

        token = new MockERC20("USDC", "USDC", 6);
        token.mint(payer, 1_000e6);
        t3009 = new MockERC3009Token("USDC3009", "USDC", 6);
        t3009.mint(payer, 1_000e6);
    }

    // ----- helpers -----

    function _mintTicketEIP3009(address from, uint256 value, bytes32 nonce) internal returns (uint256 ticketId) {
        IFeedbackGateway.EIP3009Settlement memory s = IFeedbackGateway.EIP3009Settlement({
            token: address(t3009),
            payTo: payTo,
            value: value,
            validAfter: 0,
            validBefore: type(uint256).max,
            nonce: nonce,
            signature: ""
        });
        ticketId = gateway.settleAndMintTicketEIP3009(from, AGENT_ID, payTo, s);
    }

    function _mintTicket() internal returns (uint256 ticketId) {
        ticketId = _mintTicketEIP3009(payer, 10e6, keccak256(abi.encode("mint", _nextNonce++)));
    }

    function _params(bytes32 feedbackHash) internal pure returns (IFeedbackGateway.FeedbackParams memory) {
        return IFeedbackGateway.FeedbackParams({
            value: 95,
            valueDecimals: 0,
            tag1: "quality",
            tag2: "x402",
            endpoint: "https://agent.example/r",
            feedbackURI: "mem://fb",
            feedbackHash: feedbackHash
        });
    }

    // ----- settle + mint -----

    function test_settleAndMintTicket_mintsPlainFields() public {
        uint256 ticketId = _mintTicketEIP3009(payer, 100e6, keccak256("plain"));

        assertEq(ticketId, 1);
        assertEq(t3009.balanceOf(payTo), 100e6);

        IFeedbackGateway.Ticket memory ticket = gateway.tickets(ticketId);
        assertEq(ticket.payer, payer);
        assertEq(ticket.agentId, AGENT_ID);
        assertEq(ticket.agentAddress, payTo);
        assertEq(ticket.token, address(t3009));
        assertEq(ticket.amount, 100e6);
        assertFalse(ticket.consumed);
    }

    function test_ticketMinted_emittedForReceiptRecovery() public {
        vm.expectEmit(true, true, true, true);
        emit IFeedbackGateway.TicketMinted(1, payer, AGENT_ID, payTo, address(t3009), 50e6);
        _mintTicketEIP3009(payer, 50e6, keccak256("emit"));
    }

    function test_revertWhen_payToMismatch() public {
        IFeedbackGateway.EIP3009Settlement memory s = IFeedbackGateway.EIP3009Settlement({
            token: address(t3009),
            payTo: makeAddr("other"),
            value: 1,
            validAfter: 0,
            validBefore: type(uint256).max,
            nonce: keccak256("mismatch"),
            signature: ""
        });
        vm.expectRevert(FeedbackGateway.PayToMismatch.selector);
        gateway.settleAndMintTicketEIP3009(payer, AGENT_ID, payTo, s);
    }

    function test_revertWhen_invalidAgent() public {
        IFeedbackGateway.EIP3009Settlement memory s = IFeedbackGateway.EIP3009Settlement({
            token: address(t3009),
            payTo: payTo,
            value: 1,
            validAfter: 0,
            validBefore: type(uint256).max,
            nonce: keccak256("badagent"),
            signature: ""
        });
        vm.expectRevert(FeedbackGateway.InvalidAgent.selector);
        gateway.settleAndMintTicketEIP3009(payer, 9999, payTo, s);
    }

    function test_revertWhen_agentAddressNotOwner() public {
        address notOwner = makeAddr("notOwner");
        IFeedbackGateway.EIP3009Settlement memory s = IFeedbackGateway.EIP3009Settlement({
            token: address(t3009),
            payTo: notOwner,
            value: 1,
            validAfter: 0,
            validBefore: type(uint256).max,
            nonce: keccak256("notowner"),
            signature: ""
        });
        vm.expectRevert(FeedbackGateway.InvalidAgent.selector);
        gateway.settleAndMintTicketEIP3009(payer, AGENT_ID, notOwner, s);
    }

    function test_settleAndMintTicketPermit2_mintsAndTransfers() public {
        MockPermit2 permit2 = new MockPermit2();
        permit2.setShouldActuallyTransfer(true);

        x402ExactPermit2Proxy proxy = new x402ExactPermit2Proxy(address(permit2));
        FeedbackGateway gw = new FeedbackGateway(owner, address(proxy), address(identity), address(registry));

        vm.prank(payer);
        token.approve(address(permit2), type(uint256).max);

        ISignatureTransfer.PermitTransferFrom memory permit = ISignatureTransfer.PermitTransferFrom({
            permitted: ISignatureTransfer.TokenPermissions({token: address(token), amount: 75e6}),
            nonce: 7,
            deadline: block.timestamp + 1 hours
        });

        IFeedbackGateway.Permit2Settlement memory s = IFeedbackGateway.Permit2Settlement({
            permit: permit,
            payTo: payTo,
            validAfter: 0,
            signature: ""
        });

        uint256 ticketId = gw.settleAndMintTicketPermit2(payer, AGENT_ID, payTo, s);

        assertEq(ticketId, 1);
        assertEq(token.balanceOf(payTo), 75e6);
        assertFalse(gw.tickets(ticketId).consumed);
    }
}
