import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_rebac_concept_stack import NeptuneRebacConceptStack


def _build_stack_template(rebac_config: dict) -> Template:
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)

    sg_stack = cdk.Stack(app, "TestSGStack")
    neptune_sg = ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune SG"
    )

    stack = NeptuneRebacConceptStack(
        app,
        "TestNeptuneRebacConceptStack",
        vpc=vpc,
        neptune_read_endpoint="test-neptune.cluster-ro.us-east-1.neptune.amazonaws.com",
        neptune_cluster_resource_id="cluster-ABCDEFGHIJKLMNOP",
        neptune_security_group=neptune_sg,
        synapse_team_id="273957",
        rebac_config=rebac_config,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template_with_existing_store():
    return _build_stack_template(
        {
            "policy_store_id": "ps-0123456789abcdef",
            "namespace": "SageBrain",
            "inferred_edge_mode": "intersection",
        }
    )


@pytest.fixture(scope="module")
def template_with_managed_store():
    return _build_stack_template(
        {
            "namespace": "SageBrain",
            "inferred_edge_mode": "intersection",
            "validation_mode": "STRICT",
            "deletion_protection_mode": "DISABLED",
        }
    )


def test_authorize_lambda_created(template_with_existing_store):
    template_with_existing_store.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "authorize.handler", "Runtime": "python3.11", "Timeout": 30},
    )


def test_authorize_lambda_has_expected_env_vars(template_with_existing_store):
    template_with_existing_store.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "authorize.handler",
            "Environment": {
                "Variables": {
                    "AVP_POLICY_STORE_ID": "ps-0123456789abcdef",
                    "AVP_NAMESPACE": "SageBrain",
                    "INFERRED_EDGE_MODE": "intersection",
                }
            },
        },
    )


def test_lambda_has_verified_permissions_policy(template_with_existing_store):
    template_with_existing_store.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Action": "verifiedpermissions:IsAuthorized",
                            }
                        )
                    ]
                )
            }
        },
    )


def test_rebac_authorizer_lambda_is_internal_only(
    template_with_existing_store,
):
    template_with_existing_store.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "authorize.handler",
            "Runtime": "python3.11",
            "Timeout": 30,
        },
    )
    assert not template_with_existing_store.find_resources("AWS::ApiGateway::Method")


def test_concept_outputs_exist(template_with_existing_store):
    template_with_existing_store.has_output("GovernanceRebacAuthorizeFunctionName", {})
    template_with_existing_store.has_output("GovernanceRebacPolicyStoreId", {})
    template_with_existing_store.has_output("GovernanceRebacPolicyId", {})


def test_concept_policy_is_deployed(template_with_existing_store):
    template_with_existing_store.has_resource_properties(
        "AWS::VerifiedPermissions::Policy",
        {
            "Definition": {
                "Static": Match.object_like(
                    {"Description": Match.string_like_regexp("high-cardinality")}
                )
            },
            "PolicyStoreId": "ps-0123456789abcdef",
        },
    )


def test_policy_store_created_when_id_not_provided(template_with_managed_store):
    template_with_managed_store.has_resource_properties(
        "AWS::VerifiedPermissions::PolicyStore",
        {
            "Description": "Governance ReBAC concept policy store",
            "ValidationSettings": {"Mode": "STRICT"},
            "DeletionProtection": {"Mode": "DISABLED"},
            "Schema": Match.any_value(),
        },
    )
