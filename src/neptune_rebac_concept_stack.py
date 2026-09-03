import aws_cdk as cdk
import json
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_verifiedpermissions as avp
from constructs import Construct

_BUNDLING = cdk.BundlingOptions(
    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
    command=[
        "bash",
        "-c",
        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
    ],
)


def _policy_schema(namespace: str) -> str:
    return json.dumps(
        {
            "cedarJson": {
                namespace: {
                    "entityTypes": {
                        "User": {
                            "shape": {
                                "type": "Record",
                                "attributes": {},
                            }
                        },
                        "SynapseEntity": {
                            "shape": {
                                "type": "Record",
                                "attributes": {
                                    "bindingType": {"type": "String"},
                                    "accessRequirements": {
                                        "type": "Set",
                                        "element": {"type": "String"},
                                    },
                                },
                            }
                        },
                    },
                    "actions": {
                        "ACCESS": {
                            "appliesTo": {
                                "principalTypes": ["User"],
                                "resourceTypes": ["SynapseEntity"],
                                "context": {
                                    "type": "Record",
                                    "attributes": {
                                        "governanceEvidencePresent": {
                                            "type": "Boolean"
                                        },
                                        "principalMatchesGrant": {"type": "Boolean"},
                                        "permissionMatchesGrant": {"type": "Boolean"},
                                        "arSatisfied": {"type": "Boolean"},
                                        "hasInferredGrant": {"type": "Boolean"},
                                        "inferredAllSatisfied": {"type": "Boolean"},
                                        "inferredEdgeMode": {"type": "String"},
                                    },
                                },
                            }
                        }
                    },
                }
            }
        }
    )


def _policy_statement(namespace: str) -> str:
    return f"""
forbid (
    principal,
    action == {namespace}::Action::"ACCESS",
    resource
)
when {{
    !context.governanceEvidencePresent
}};

forbid (
    principal,
    action == {namespace}::Action::"ACCESS",
    resource
)
when {{
    context.inferredEdgeMode == "intersection" &&
    context.hasInferredGrant &&
    !context.inferredAllSatisfied
}};

permit (
    principal,
    action == {namespace}::Action::"ACCESS",
    resource
)
when {{
    context.governanceEvidencePresent &&
    context.principalMatchesGrant &&
    context.permissionMatchesGrant &&
    context.arSatisfied &&
    resource.bindingType == "Direct"
}};

permit (
    principal,
    action == {namespace}::Action::"ACCESS",
    resource
)
when {{
    context.governanceEvidencePresent &&
    context.principalMatchesGrant &&
    context.permissionMatchesGrant &&
    context.arSatisfied &&
    context.inferredEdgeMode == "union" &&
    resource.bindingType == "Inferred"
}};

permit (
    principal,
    action == {namespace}::Action::"ACCESS",
    resource
)
when {{
    context.governanceEvidencePresent &&
    context.principalMatchesGrant &&
    context.permissionMatchesGrant &&
    context.arSatisfied &&
    context.inferredEdgeMode == "intersection" &&
    context.inferredAllSatisfied &&
    resource.bindingType == "Inferred"
}};
""".strip()


class NeptuneRebacConceptStack(cdk.Stack):
    """Concept API: governance-graph-aware ReBAC authorization via Verified Permissions."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_read_endpoint: str,
        neptune_cluster_resource_id: str,
        neptune_security_group: ec2.SecurityGroup,
        synapse_team_id: str,
        rebac_config: dict,
        machine_api_key: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        namespace = rebac_config.get("namespace", "SageBrain")
        existing_policy_store_id = (rebac_config.get("policy_store_id") or "").strip()
        policy_store_id = existing_policy_store_id
        if not policy_store_id:
            policy_store = avp.CfnPolicyStore(
                self,
                "GovernanceRebacPolicyStore",
                validation_settings=avp.CfnPolicyStore.ValidationSettingsProperty(
                    mode=rebac_config.get("validation_mode", "STRICT")
                ),
                deletion_protection=avp.CfnPolicyStore.DeletionProtectionProperty(
                    mode=rebac_config.get("deletion_protection_mode", "DISABLED")
                ),
                description="Governance ReBAC concept policy store",
                schema=avp.CfnPolicyStore.SchemaDefinitionProperty(
                    cedar_json=_policy_schema(namespace)
                ),
            )
            policy_store_id = policy_store.attr_policy_store_id

        concept_policy = avp.CfnPolicy(
            self,
            "GovernanceRebacConceptPolicy",
            policy_store_id=policy_store_id,
            definition=avp.CfnPolicy.PolicyDefinitionProperty(
                static=avp.CfnPolicy.StaticPolicyDefinitionProperty(
                    statement=_policy_statement(namespace),
                    description=(
                        "Generic governance graph ReBAC policy for high-cardinality "
                        "ACL/AR relationships"
                    ),
                )
            ),
        )

        self.lambda_sg = ec2.SecurityGroup(
            self,
            "RebacAuthorizerFunctionSG",
            vpc=vpc,
            description="Security group for governance ReBAC authorizer Lambda",
            allow_all_outbound=True,
        )

        ec2.CfnSecurityGroupIngress(
            self,
            "RebacLambdaToNeptuneIngress",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.lambda_sg.security_group_id,
        )

        self.authorize_fn = lambda_.Function(
            self,
            "GovernanceRebacAuthorizerFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="authorize.handler",
            code=lambda_.Code.from_asset("src/lambda_rebac", bundling=_BUNDLING),
            vpc=vpc,
            security_groups=[self.lambda_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                "NEPTUNE_ENDPOINT": neptune_read_endpoint,
                "AVP_POLICY_STORE_ID": policy_store_id,
                "AVP_NAMESPACE": namespace,
                "INFERRED_EDGE_MODE": rebac_config.get(
                    "inferred_edge_mode", "intersection"
                ),
            },
            timeout=cdk.Duration.seconds(30),
            memory_size=512,
        )

        self.authorize_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "neptune-db:ReadDataViaQuery",
                    "neptune-db:GetEngineStatus",
                    "neptune-db:GetQueryStatus",
                ],
                resources=[
                    f"arn:{self.partition}:neptune-db:{self.region}:{self.account}:{neptune_cluster_resource_id}/*"
                ],
            )
        )
        self.authorize_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["verifiedpermissions:IsAuthorized"],
                resources=[
                    (
                        f"arn:{self.partition}:verifiedpermissions:{self.region}:"
                        f"{self.account}:policy-store/{policy_store_id}"
                    )
                ],
            )
        )

        cdk.CfnOutput(
            self,
            "GovernanceRebacAuthorizeFunctionName",
            value=self.authorize_fn.function_name,
            description="Internal ReBAC authorize Lambda used by the query submission path",
        )
        cdk.CfnOutput(
            self,
            "GovernanceRebacPolicyStoreId",
            value=policy_store_id,
            description="Verified Permissions policy store used by the concept endpoint",
        )
        cdk.CfnOutput(
            self,
            "GovernanceRebacPolicyId",
            value=concept_policy.attr_policy_id,
            description="Deployed generic governance ReBAC policy id",
        )
