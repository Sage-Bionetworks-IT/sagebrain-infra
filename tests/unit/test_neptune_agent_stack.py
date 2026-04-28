import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_agent_stack import NeptuneAgentStack


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)

    stack = NeptuneAgentStack(
        app,
        "TestNeptuneAgentStack",
        vpc=vpc,
        neptune_query_url="https://example.execute-api.us-east-1.amazonaws.com/prod/query",
        neptune_query_status_url="https://example.execute-api.us-east-1.amazonaws.com/prod/query",
        synapse_team_id="273957",
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------


def test_submit_and_status_lambda_created(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "submit.handler", "Timeout": 10},
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "status.handler", "Timeout": 10},
    )


def test_agent_worker_lambda_created(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "agent.handler", "Timeout": 300},
    )


def test_authorizer_lambda_has_team_id_env(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "authorizer.handler",
            "Environment": {"Variables": {"SYNAPSE_TEAM_ID": "273957"}},
        },
    )


# ---------------------------------------------------------------------------
# API Gateway — authentication
# ---------------------------------------------------------------------------


def test_api_gateway_created(template):
    template.has_resource_properties(
        "AWS::ApiGateway::RestApi",
        {"Name": "neptune-agent-api"},
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


def test_request_authorizer_created(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Authorizer",
        {
            "Type": "REQUEST",
            "AuthorizerResultTtlInSeconds": 0,
            "IdentitySource": "method.request.header.Authorization",
        },
    )


def test_gateway_response_remaps_access_denied_to_401(template):
    template.has_resource_properties(
        "AWS::ApiGateway::GatewayResponse",
        {"ResponseType": "ACCESS_DENIED", "StatusCode": "401"},
    )


def test_gateway_response_unauthorized_has_cors_header(template):
    template.has_resource_properties(
        "AWS::ApiGateway::GatewayResponse",
        {
            "ResponseType": "UNAUTHORIZED",
            "ResponseParameters": {
                "gatewayresponse.header.Access-Control-Allow-Origin": "'*'"
            },
        },
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_agent_api_url_output_exists(template):
    template.has_output("AgentApiUrl", {})
