import inspect

import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_agent_stack import NeptuneAgentStack
from src.rate_limit_stack import RateLimitStack

READ_ENDPOINT = "test-neptune.cluster-ro.us-east-1.neptune.amazonaws.com"
MODEL_ID = "us.anthropic.claude-test-model"


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)

    sg_stack = cdk.Stack(app, "TestSGStack")
    neptune_sg = ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune SG"
    )

    rate_limit_stack = RateLimitStack(app, "TestRateLimitStack")

    stack = NeptuneAgentStack(
        app,
        "TestNeptuneAgentStack",
        vpc=vpc,
        neptune_read_endpoint=READ_ENDPOINT,
        neptune_cluster_resource_id="cluster-ABCDEFGHIJKLMNOP",
        neptune_security_group=neptune_sg,
        rate_limit_table=rate_limit_stack.table,
        bedrock_model_id=MODEL_ID,
        synapse_team_id="273957",
    )
    return Template.from_stack(stack)


def _worker(template):
    (worker,) = [
        r
        for r in template.find_resources("AWS::Lambda::Function").values()
        if r["Properties"]["Handler"] == "agent.handler"
    ]
    return worker["Properties"]


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


# ---------------------------------------------------------------------------
# Spec 001 T124 — the agent worker calls sagebrain_core.run_query in-process
# ---------------------------------------------------------------------------


def test_worker_env_points_at_neptune_not_the_query_api(template):
    env = _worker(template)["Environment"]["Variables"]
    assert env["NEPTUNE_ENDPOINT"] == READ_ENDPOINT
    assert "NEPTUNE_QUERY_URL" not in env
    assert "NEPTUNE_QUERY_STATUS_URL" not in env
    # AWS_REGION is reserved: the Lambda runtime sets it (NeptuneConfig.from_env reads it),
    # and CloudFormation rejects a function that sets it explicitly.
    assert "AWS_REGION" not in env


def test_constructor_no_longer_takes_query_api_urls():
    params = inspect.signature(NeptuneAgentStack.__init__).parameters
    assert "neptune_query_url" not in params
    assert "neptune_query_status_url" not in params


def test_worker_uses_arm64_core_layer(template):
    layer_id, layer = next(
        iter(template.find_resources("AWS::Lambda::LayerVersion").items())
    )
    assert "arm64" in layer["Properties"]["CompatibleArchitectures"]
    worker = _worker(template)
    assert worker["Layers"] == [{"Ref": layer_id}]
    assert worker["Architectures"] == ["arm64"]


def test_only_worker_uses_core_layer(template):
    with_layers = [
        f["Properties"]["Handler"]
        for f in template.find_resources("AWS::Lambda::Function").values()
        if f["Properties"].get("Layers")
    ]
    assert with_layers == ["agent.handler"]


def test_worker_role_has_read_only_neptune_actions_on_the_cluster(template):
    statements = [
        stmt
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]
        if any(
            str(a).startswith("neptune-db:")
            for a in (
                stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            )
        )
    ]
    (stmt,) = statements
    assert stmt["Effect"] == "Allow"
    # The same set as the query worker (tests/unit/test_neptune_api_stack.py).
    assert stmt["Action"] == [
        "neptune-db:ReadDataViaQuery",
        "neptune-db:GetEngineStatus",
        "neptune-db:GetQueryStatus",
    ]
    assert "cluster-ABCDEFGHIJKLMNOP/*" in str(stmt["Resource"])
    (policy,) = [
        p
        for p in template.find_resources("AWS::IAM::Policy").values()
        if stmt in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    assert policy["Properties"]["Roles"] == [
        {"Ref": next(iter(_worker_role(template)))}
    ]


def _worker_role(template):
    role_ref = _worker(template)["Role"]["Fn::GetAtt"][0]
    return {role_ref: template.find_resources("AWS::IAM::Role")[role_ref]}


def test_no_write_neptune_actions(template):
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            assert "neptune-db:WriteDataViaQuery" not in actions
            assert "neptune-db:DeleteDataViaQuery" not in actions
            assert "neptune-db:*" not in actions


def test_agent_sg_to_neptune_sg_ingress_on_8182(template):
    (ingress,) = template.find_resources("AWS::EC2::SecurityGroupIngress").values()
    props = ingress["Properties"]
    assert (props["IpProtocol"], props["FromPort"], props["ToPort"]) == (
        "tcp",
        8182,
        8182,
    )
    assert props["SourceSecurityGroupId"] == {
        "Fn::GetAtt": ["NeptuneAgentFunctionSGC9AE14F1", "GroupId"]
    }
    # The Neptune SG lives in another stack: imported, not created here.
    assert "Fn::ImportValue" in props["GroupId"]
    assert _worker(template)["VpcConfig"]["SecurityGroupIds"] == [
        {"Fn::GetAtt": ["NeptuneAgentFunctionSGC9AE14F1", "GroupId"]}
    ]


def test_worker_limits_unchanged(template):
    worker = _worker(template)
    assert worker["ReservedConcurrentExecutions"] == 10
    assert worker["Timeout"] == 300
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "VisibilityTimeout": 360,
            "RedrivePolicy": {
                "maxReceiveCount": 2,
                "deadLetterTargetArn": Match.any_value(),
            },
        },
    )


# Every logical ID the stack had before T124 (the API deployment's ID is a content hash).
EXISTING_LOGICAL_IDS = {
    "AgentJobDLQ30F7FF98",
    "AgentJobQueueEAEF08DA",
    "AgentJobTable5482E7EB",
    "NeptuneAgentAccessLogsC1007D96",
    "NeptuneAgentApi4F3F5290",
    "NeptuneAgentApiAccessDeniedAs4017A6B8C52",
    "NeptuneAgentApiAccount07E6D351",
    "NeptuneAgentApiCloudWatchRoleE723F6F6",
    "NeptuneAgentApiDeploymentStageprodC71B2C9E",
    "NeptuneAgentApiOPTIONS9894226E",
    "NeptuneAgentApiUnauthorizedWithCors629D3313",
    "NeptuneAgentApiask0E99693D",
    "NeptuneAgentApiaskOPTIONS056871A6",
    "NeptuneAgentApiaskPOST7AF4927F",
    "NeptuneAgentApiaskjobid56745EF3",
    "NeptuneAgentApiaskjobidGET1F378ACF",
    "NeptuneAgentApiaskjobidOPTIONS9B782B66",
    "NeptuneAgentFunctionSGC9AE14F1",
    "NeptuneAgentStatusFunctionFB033FB1",
    "NeptuneAgentStatusFunctionServiceRole415BA59A",
    "NeptuneAgentStatusFunctionServiceRoleDefaultPolicy1B844915",
    "NeptuneAgentSubmitFunction7F9431EE",
    "NeptuneAgentSubmitFunctionServiceRole9E13EF94",
    "NeptuneAgentSubmitFunctionServiceRoleDefaultPolicy182D6619",
    "NeptuneAgentWorkerFunction0009DF71",
    "NeptuneAgentWorkerFunctionServiceRole59D98EA4",
    "NeptuneAgentWorkerFunctionServiceRoleDefaultPolicy3492502D",
    "SynapseAuthorizerFunction4072D60B",
    "SynapseAuthorizerFunctionServiceRoleC8515237",
    "SynapseRequestAuthorizer7349CCBD",
    "NeptuneAgentApiaskPOSTApiPermissionTestNeptuneAgentStackNeptuneAgentApiC2C49D5CPOSTask64FF7BE9",
    "NeptuneAgentApiaskPOSTApiPermissionTestTestNeptuneAgentStackNeptuneAgentApiC2C49D5CPOSTask7F6F1E07",
    "NeptuneAgentApiaskjobidGETApiPermissionTestNeptuneAgentStackNeptuneAgentApiC2C49D5CGETaskjobid18BA9D54",
    "NeptuneAgentApiaskjobidGETApiPermissionTestTestNeptuneAgentStackNeptuneAgentApiC2C49D5CGETaskjobid3ABCF580",
    "NeptuneAgentWorkerFunctionSqsEventSourceTestNeptuneAgentStackAgentJobQueue2FFEAA7999F89FD9",
    "SynapseAuthorizerFunctionTestNeptuneAgentStackSynapseRequestAuthorizer9CC15965Permissions26CDE25E",
}


def test_logical_ids_unchanged(template):
    resources = template.to_json()["Resources"]
    assert EXISTING_LOGICAL_IDS <= set(resources)
    added = {
        resources[k]["Type"]
        for k in set(resources) - EXISTING_LOGICAL_IDS
        if not k.startswith("NeptuneAgentApiDeployment")
    }
    assert added == {"AWS::Lambda::LayerVersion", "AWS::EC2::SecurityGroupIngress"}


# ---------------------------------------------------------------------------
# Spec 001 T147 — the worker charges the shared DynamoDB rate-limit table
# ---------------------------------------------------------------------------


def _statements(template):
    for policy_id, policy in template.find_resources("AWS::IAM::Policy").items():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            yield policy_id, policy, stmt


def _on_rate_limit_table(stmt):
    return "RateLimitTable" in str(stmt["Resource"])


def test_worker_env_has_the_rate_limit_table(template):
    env = _worker(template)["Environment"]["Variables"]
    # The table lives in the rate-limit stack: imported, not created here.
    assert "Fn::ImportValue" in env["RATE_LIMIT_TABLE_NAME"]
    assert "TestRateLimitStack" in str(env["RATE_LIMIT_TABLE_NAME"])
    # Each of the 10 concurrent instances gets rate / 10 if DynamoDB is unreachable.
    assert env["RATE_LIMIT_FALLBACK_INSTANCES"] == str(
        _worker(template)["ReservedConcurrentExecutions"]
    )


def test_only_the_worker_knows_the_rate_limit_table(template):
    for fn in template.find_resources("AWS::Lambda::Function").values():
        if fn["Properties"]["Handler"] != "agent.handler":
            env = fn["Properties"].get("Environment", {}).get("Variables", {})
            assert "RATE_LIMIT_TABLE_NAME" not in env


def test_worker_may_only_update_items_in_the_rate_limit_table(template):
    grants = [
        (policy, stmt)
        for _, policy, stmt in _statements(template)
        if _on_rate_limit_table(stmt)
    ]
    ((policy, stmt),) = grants
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "dynamodb:UpdateItem"  # no Get/Put/Delete/Scan/Query
    # The table ARN itself: no index or stream sub-resources.
    assert not isinstance(stmt["Resource"], list)
    assert "Fn::ImportValue" in stmt["Resource"]
    assert policy["Properties"]["Roles"] == [
        {"Ref": next(iter(_worker_role(template)))}
    ]


def test_no_wildcard_dynamodb_actions(template):
    for _, _, stmt in _statements(template):
        actions = (
            stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
        )
        assert "dynamodb:*" not in actions
        if _on_rate_limit_table(stmt):
            for denied in ("dynamodb:Scan", "dynamodb:Query", "dynamodb:DeleteItem"):
                assert denied not in actions


# ---------------------------------------------------------------------------
# Bedrock model: comes from config (AGENT.bedrock_model_id), not a constructor default
# ---------------------------------------------------------------------------


def test_worker_env_has_the_configured_model(template):
    assert _worker(template)["Environment"]["Variables"]["BEDROCK_MODEL_ID"] == MODEL_ID


def test_model_id_has_no_default():
    param = inspect.signature(NeptuneAgentStack.__init__).parameters["bedrock_model_id"]
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "env_name, expected",
    [
        ("dev", "us.anthropic.claude-sonnet-5-5"),  # config/base.yaml
        ("prod", "us.anthropic.claude-sonnet-4-6"),  # pinned in config/prod.yaml
    ],
)
def test_configured_model_per_env_is_covered_by_iam(env_name, expected):
    from src.utils import load_context_config

    model_id = load_context_config(env_name)["AGENT"]["bedrock_model_id"]
    assert model_id == expected
    # The worker's Bedrock statement allows inference-profile/us.anthropic.claude-*.
    assert model_id.startswith("us.anthropic.claude-")
