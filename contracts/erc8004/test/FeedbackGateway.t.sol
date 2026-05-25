pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/FeedbackGateway.sol";
import "./mocks/MockIdentityRegistry.sol";
import "./mocks/MockReputationRegistry.sol";

contract FeedbackGatewayTest is Test {
    FeedbackGateway gateway;
    MockIdentityRegistry identityRegistry;
    MockReputationRegistry reputationRegistry;
    
    uint256 agentId = 42;
    uint256 serverKey = 0xabc123;
    address serverAddr = vm.addr(serverKey);
    uint256 clientKey = 0xdef456;
    address clientAddr = vm.addr(clientKey);
    
    function setUp() public {
        identityRegistry = new MockIdentityRegistry();
        identityRegistry.setOwner(agentId, serverAddr);
        gateway = new FeedbackGateway(address(identityRegistry));
        reputationRegistry = new MockReputationRegistry();
    }
    
    function _createTicket(bytes32 txHash, uint256 nonce) internal view returns (FeedbackTicket memory) {
        bytes32 digest = keccak256(abi.encodePacked(
            "\x19\x01",
            block.chainid,
            txHash,
            clientAddr,
            agentId,
            nonce
        ));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(serverKey, digest);
        bytes memory sig = abi.encodePacked(r, s, v);
        return FeedbackTicket(txHash, clientAddr, agentId, nonce, sig);
    }
    
    function test_submitFeedback_success() public {
        FeedbackParams memory params = FeedbackParams({
            agentId: agentId,
            value: 95,
            valueDecimals: 0,
            tag1: "x402",
            tag2: "weather",
            endpoint: "https://example.com/weather",
            feedbackURI: "",
            feedbackHash: bytes32(0)
        });
        FeedbackTicket memory ticket = _createTicket(bytes32(uint256(0x123)), 1);
        
        vm.prank(clientAddr);
        gateway.submitFeedback(address(reputationRegistry), params, ticket);
    }
    
    function test_revert_payerMismatch() public {
        FeedbackParams memory params = FeedbackParams(agentId, 95, 0, "x402", "weather", "https://example.com/weather", "", bytes32(0));
        FeedbackTicket memory ticket = _createTicket(bytes32(uint256(0x123)), 1);
        vm.prank(address(0xdead));
        vm.expectRevert(FeedbackGateway.PayerMismatch.selector);
        gateway.submitFeedback(address(reputationRegistry), params, ticket);
    }
    
    function test_revert_invalidTicket() public {
        identityRegistry.setOwner(agentId, address(0xbad));
        FeedbackParams memory params = FeedbackParams(agentId, 95, 0, "x402", "weather", "https://example.com/weather", "", bytes32(0));
        FeedbackTicket memory ticket = _createTicket(bytes32(uint256(0x123)), 1);
        vm.prank(clientAddr);
        vm.expectRevert(FeedbackGateway.InvalidTicket.selector);
        gateway.submitFeedback(address(reputationRegistry), params, ticket);
    }
    
    function test_revert_nonceUsed() public {
        FeedbackParams memory params = FeedbackParams(agentId, 95, 0, "x402", "weather", "https://example.com/weather", "", bytes32(0));
        FeedbackTicket memory ticket = _createTicket(bytes32(uint256(0x123)), 1);
        vm.prank(clientAddr);
        gateway.submitFeedback(address(reputationRegistry), params, ticket);
        
        vm.prank(clientAddr);
        vm.expectRevert(abi.encodeWithSelector(FeedbackGateway.NonceUsed.selector, 1));
        gateway.submitFeedback(address(reputationRegistry), params, ticket);
    }
}
