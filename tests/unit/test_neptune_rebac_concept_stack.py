import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_rebac_concept_stack import NeptuneRebacConceptStack


@pytest.fixture(scope="module")
def template():
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
        rebac_config={
            "policy_store_id": "ps-0123456789abcdef",
            "namespace": "SageBrain",
            "inferred_edge_mode": "intersection",
        },
    )
    return Template.from_stack(stack)


def test_authorize_lambda_created(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "authorize.handler", "Runtime": "python3.11", "Timeout": 30},
    )


def test_authorize_lambda_has_expected_env_vars(template):
    template.has_resource_properties(
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


def test_lambda_has_verified_permissions_policy(template):
    template.has_resource_properties(
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


def test_api_gateway_post_authorize_uses_custom_authorizer(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "POST",
            "AuthorizationType": "CUSTOM",
            "AuthorizerId": Match.any_value(),
        },
    )


def test_rebac_output_exists(template):
    template.has_output("GovernanceRebacAuthorizeUrl", {})


def test_missing_policy_store_id_raises():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})
    vpc_stack = cdk.Stack(app, "TestVpcStackMissingStore")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)
    sg_stack = cdk.Stack(app, "TestSGStackMissingStore")
    neptune_sg = ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune SG"
    )

    with pytest.raises(ValueError, match="policy_store_id"):
        NeptuneRebacConceptStack(
            app,
            "TestNeptuneRebacConceptStackMissingStore",
            vpc=vpc,
            neptune_read_endpoint="test-neptune.cluster-ro.us-east-1.neptune.amazonaws.com",
            neptune_cluster_resource_id="cluster-ABCDEFGHIJKLMNOP",
            neptune_security_group=neptune_sg,
            synapse_team_id="273957",
            rebac_config={},
        )
