permit (
    principal,
    action == SageBrain::Action::"ACCESS",
    resource
)
when {
    context.governanceEvidencePresent &&
    context.principalMatchesGrant &&
    context.permissionMatchesGrant &&
    context.arSatisfied &&
    resource.bindingType == "Direct"
};

permit (
    principal,
    action == SageBrain::Action::"ACCESS",
    resource
)
when {
    context.governanceEvidencePresent &&
    context.principalMatchesGrant &&
    context.permissionMatchesGrant &&
    context.arSatisfied &&
    context.inferredEdgeMode == "union" &&
    resource.bindingType == "Inferred"
};

permit (
    principal,
    action == SageBrain::Action::"ACCESS",
    resource
)
when {
    context.governanceEvidencePresent &&
    context.principalMatchesGrant &&
    context.permissionMatchesGrant &&
    context.arSatisfied &&
    context.inferredEdgeMode == "intersection" &&
    context.inferredAllSatisfied &&
    resource.bindingType == "Inferred"
};

forbid (
    principal,
    action == SageBrain::Action::"ACCESS",
    resource
)
when {
    !context.governanceEvidencePresent
};

forbid (
    principal,
    action == SageBrain::Action::"ACCESS",
    resource
)
when {
    context.inferredEdgeMode == "intersection" &&
    context.hasInferredGrant &&
    !context.inferredAllSatisfied
};
