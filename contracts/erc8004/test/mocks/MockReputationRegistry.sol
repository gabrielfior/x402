pragma solidity ^0.8.20;

contract MockReputationRegistry {
    event FeedbackGiven(
        uint256 agentId,
        int128 value,
        uint8 valueDecimals,
        string tag1,
        string tag2,
        string endpoint,
        string feedbackURI,
        bytes32 feedbackHash
    );

    function giveFeedback(
        uint256 agentId,
        int128 value,
        uint8 valueDecimals,
        string calldata tag1,
        string calldata tag2,
        string calldata endpoint,
        string calldata feedbackURI,
        bytes32 feedbackHash
    ) external {
        emit FeedbackGiven(agentId, value, valueDecimals, tag1, tag2, endpoint, feedbackURI, feedbackHash);
    }
}
