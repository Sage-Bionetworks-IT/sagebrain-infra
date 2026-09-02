permit (
    principal == SageBrain::User::"9000001",
    action == SageBrain::Action::"DOWNLOAD",
    resource in SageBrain::AccessGrant::"https://sagebionetworks.org/governance/grant-001"
)
when {
    "https://sagebionetworks.org/governance/AR-42" in resource.accessRequirements
};
