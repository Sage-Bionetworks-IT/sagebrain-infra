import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_api_stack import NeptuneApiStack


@pytest.fixture
def template():
    app = cdk.App()

    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)

    sg_stack = cdk.Stack(app, "TestSGStack")
    neptune_sg = ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune SG"
    )

    stack = NeptuneApiStack(
        app,
        "TestNeptuneApiStack",
        vpc=vpc,
        neptune_read_endpoint="test-neptune.cluster-ro.us-east-1.neptune.amazonaws.com",
        neptune_cluster_resource_id="cluster-ABCDEFGHIJKLMNOP",
        neptune_security_group=neptune_sg,
        synapse_team_id="273957",
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------


def test_lambda_function_created(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.11",
            "Handler": "query.handler",
        },
    )


def test_query_worker_lambda_timeout_is_75s(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "query.handler", "Timeout": 75},
    )


def test_submit_and_status_lambda_timeout_is_10s(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "submit.handler", "Timeout": 10},
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "status.handler", "Timeout": 10},
    )


def test_lambda_has_neptune_endpoint_env(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": {
                    "NEPTUNE_ENDPOINT": "test-neptune.cluster-ro.us-east-1.neptune.amazonaws.com"
                }
            }
        },
    )


def test_lambda_in_private_subnet(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"VpcConfig": Match.object_like({"SubnetIds": Match.any_value()})},
    )


def test_lambda_iam_read_only_actions(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Action": Match.array_equals(
                                    [
                                        "neptune-db:ReadDataViaQuery",
                                        "neptune-db:GetEngineStatus",
                                        "neptune-db:GetQueryStatus",
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_lambda_has_no_write_iam_actions(template):
    policies = template.find_resources("AWS::IAM::Policy")
    for policy in policies.values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            assert "neptune-db:WriteDataViaQuery" not in actions
            assert "neptune-db:DeleteDataViaQuery" not in actions


# ---------------------------------------------------------------------------
# Security group
# ---------------------------------------------------------------------------


def test_lambda_security_group_created(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"GroupDescription": "Security group for Neptune query Lambda"},
    )


def test_neptune_ingress_rule_on_port_8182(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"FromPort": 8182, "ToPort": 8182, "IpProtocol": "tcp"},
    )


# ---------------------------------------------------------------------------
# API Gateway
# ---------------------------------------------------------------------------


def test_api_gateway_created(template):
    template.has_resource_properties(
        "AWS::ApiGateway::RestApi",
        {"Name": "neptune-query-api"},
    )


def test_post_method_exists(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {"HttpMethod": "POST"},
    )


def test_get_method_exists_for_status_polling(template):
    # GET /query/{job_id} is required for async status polling
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {"HttpMethod": "GET"},
    )


def test_options_method_exists_for_cors(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {"HttpMethod": "OPTIONS"},
    )


def test_post_method_has_custom_authorizer(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "POST",
            "AuthorizationType": "CUSTOM",
            "AuthorizerId": Match.any_value(),
        },
    )


def test_get_method_has_custom_authorizer(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "GET",
            "AuthorizationType": "CUSTOM",
            "AuthorizerId": Match.any_value(),
        },
    )


def test_options_method_has_no_authorizer(template):
    # CORS preflight must not be gated — browsers can't send auth on OPTIONS
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {"HttpMethod": "OPTIONS", "AuthorizationType": "NONE"},
    )


def test_token_authorizer_created(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Authorizer",
        {
            "Type": "TOKEN",
            "AuthorizerResultTtlInSeconds": 300,
            "IdentitySource": "method.request.header.Authorization",
        },
    )


def test_authorizer_lambda_has_team_id_env(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "authorizer.handler",
            "Environment": {"Variables": {"SYNAPSE_TEAM_ID": "273957"}},
        },
    )


def test_gateway_response_remaps_access_denied_to_401(template):
    template.has_resource_properties(
        "AWS::ApiGateway::GatewayResponse",
        {"ResponseType": "ACCESS_DENIED", "StatusCode": "401"},
    )


def test_stage_has_throttling(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Stage",
        {
            "DefaultRouteSettings": Match.absent(),
            "MethodSettings": Match.any_value(),
        },
    )


def test_access_log_group_created_with_retention(template):
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {"RetentionInDays": 30},
    )


def test_cloudwatch_role_configured(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Account",
        {"CloudWatchRoleArn": Match.any_value()},
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_api_url_output_exists(template):
    template.has_output("ApiUrl", {})
