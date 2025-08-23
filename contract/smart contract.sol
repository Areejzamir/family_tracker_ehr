// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

contract FamilyHealthRecords {

    struct Record {
        string ipfsHash;
        address patient;
        mapping(address => uint256) accessExpiry; // doctor/pharmacist => expiry timestamp
    }

    mapping(address => Record) private records;

    event RecordAdded(address indexed patient, string ipfsHash);
    event AccessGranted(address indexed patient, address indexed viewer, uint256 expiryTime);
    event AccessRevoked(address indexed patient, address indexed viewer);

    // Add or update a patient record
    function addRecord(string memory _ipfsHash) public {
        Record storage r = records[msg.sender];
        r.ipfsHash = _ipfsHash;
        r.patient = msg.sender;
        emit RecordAdded(msg.sender, _ipfsHash);
    }

    // Grant temporary access (default 24 hours)
    function grantAccess(address _viewer, uint256 durationInSeconds) public {
        require(_viewer != msg.sender, "Cannot grant access to self");
        Record storage r = records[msg.sender];
        r.accessExpiry[_viewer] = block.timestamp + durationInSeconds;
        emit AccessGranted(msg.sender, _viewer, r.accessExpiry[_viewer]);
    }

    // Revoke access manually before expiry
    function revokeAccess(address _viewer) public {
        Record storage r = records[msg.sender];
        require(r.accessExpiry[_viewer] > 0, "No access to revoke");
        r.accessExpiry[_viewer] = 0;
        emit AccessRevoked(msg.sender, _viewer);
    }

    // View record if access is valid
    function viewRecord(address _patient) public view returns (string memory) {
        Record storage r = records[_patient];
        require(
            msg.sender == _patient || r.accessExpiry[msg.sender] > block.timestamp,
            "Access denied"
        );
        return r.ipfsHash;
    }

    // Check if access is still valid
    function checkAccess(address _patient, address _viewer) public view returns (bool) {
        Record storage r = records[_patient];
        return r.accessExpiry[_viewer] > block.timestamp;
    }
}
