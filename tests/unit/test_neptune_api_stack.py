import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_api_stack import NeptuneApiStack
from src.utils import load_context_config

# Differs from both env values, so the test shows the stack passes the config value through.
QUERY_WORKER_CONCURRENCY = 7


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

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
        query_worker_concurrency=QUERY_WORKER_CONCURRENCY,
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


def test_query_worker_reserved_concurrency_comes_from_config(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "query.handler",
            "ReservedConcurrentExecutions": QUERY_WORKER_CONCURRENCY,
        },
    )


@pytest.mark.parametrize("env_name, expected", [("dev", 5), ("prod", 10)])
def test_query_worker_concurrency_per_env(env_name, expected):
    """spec 001 D-14: dev 5, prod 10."""
    config = load_context_config(env_name)
    assert config["NEPTUNE_API"]["query_worker_concurrency"] == expected


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
# Spec 001 T503 — the old API is gone; the job backend keeps its logical IDs
# ---------------------------------------------------------------------------

# The unified API (app-*-api) imports the table and queue, and the table is RemovalPolicy.DESTROY:
# a changed logical ID would replace it and drop its jobs.
KEPT_LOGICAL_IDS = {
    "QueryJobTableEE5582B4",
    "QueryJobDLQE4A49099",
    "QueryJobQueueCEFE3CFA",
    "NeptuneQueryFunctionSG19038A6B",
    "LambdaToNeptuneIngress",
    "NeptuneQueryFunctionB8541B6D",
    "NeptuneQueryFunctionServiceRole499C98DA",
    "NeptuneQueryFunctionServiceRoleDefaultPolicy1D59776F",
    "SageBrainCoreLayerA5AFF5B7",
}


def test_job_backend_logical_ids_unchanged(template):
    resources = template.to_json()["Resources"]
    assert KEPT_LOGICAL_IDS <= set(resources)
    added = {
        resources[k]["Type"]
        for k in set(resources) - KEPT_LOGICAL_IDS
        if not k.startswith("NeptuneQueryFunctionSqsEventSource")
    }
    assert added == set()


def test_no_api_gateway_resources(template):
    types = {r["Type"] for r in template.to_json()["Resources"].values()}
    assert not {t for t in types if t.startswith("AWS::ApiGateway::")}


def test_only_the_worker_lambda(template):
    handlers = [
        f["Properties"]["Handler"]
        for f in template.find_resources("AWS::Lambda::Function").values()
    ]
    assert handlers == ["query.handler"]


def test_job_table_output_exists(template):
    template.has_output("QueryJobTableName", {})
    assert "ApiUrl" not in template.to_json()["Outputs"]


# ---------------------------------------------------------------------------
# sagebrain_core layer (spec 001 T106)
# ---------------------------------------------------------------------------


def test_core_layer_created_for_both_architectures(template):
    template.has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {
            "CompatibleRuntimes": ["python3.11"],
            "CompatibleArchitectures": ["x86_64", "arm64"],
        },
    )


def test_query_worker_uses_core_layer(template):
    layer_id = next(iter(template.find_resources("AWS::Lambda::LayerVersion")))
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "query.handler", "Layers": [{"Ref": layer_id}]},
    )


def test_only_query_worker_uses_core_layer(template):
    functions = template.find_resources("AWS::Lambda::Function")
    with_layers = [
        f["Properties"]["Handler"]
        for f in functions.values()
        if f["Properties"].get("Layers")
    ]
    assert with_layers == ["query.handler"]
