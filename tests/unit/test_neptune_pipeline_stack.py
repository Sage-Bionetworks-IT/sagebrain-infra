import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from src.neptune_pipeline_stack import NeptunePipelineStack
from src.utils import load_context_config

# Differs from both env values, so the test shows the stack passes the config value through.
DROP_BATCH_SIZE = 12345


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

    base = cdk.Stack(app, "TestBaseStack")
    vpc = ec2.Vpc(base, "TestVpc", max_azs=2)
    neptune_sg = ec2.SecurityGroup(
        base, "TestNeptuneSG", vpc=vpc, description="Test Neptune SG"
    )
    bucket = s3.Bucket(base, "TestBucket")

    stack = NeptunePipelineStack(
        app,
        "TestNeptunePipelineStack",
        vpc=vpc,
        data_bucket=bucket,
        neptune_security_group=neptune_sg,
        neptune_cluster_endpoint="test-neptune.cluster.us-east-1.neptune.amazonaws.com",
        neptune_cluster_resource_id="cluster-ABCDEFGHIJKLMNOP",
        neptune_load_role_arn="arn:aws:iam::123456789012:role/NeptuneLoadRole",
        pipeline_config={
            "graph_uri_template": "urn:sagebrain:{portal}:{tag}",
            "parallelism": "HIGH",
            "wait_seconds": 30,
            "drop_batch_size": DROP_BATCH_SIZE,
        },
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Loader Lambda
# ---------------------------------------------------------------------------


def test_loader_lambda_created_in_vpc(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "loader.handler",
            "Runtime": "python3.11",
            "VpcConfig": Match.object_like({"SubnetIds": Match.any_value()}),
        },
    )


def test_loader_env_uses_writer_endpoint(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "loader.handler",
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "NEPTUNE_ENDPOINT": "test-neptune.cluster.us-east-1.neptune.amazonaws.com",
                        "GRAPH_URI_TEMPLATE": "urn:sagebrain:{portal}:{tag}",
                    }
                )
            },
        },
    )


def test_loader_iam_bulk_load_actions(template):
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
                                        "neptune-db:StartLoaderJob",
                                        "neptune-db:GetLoaderJobStatus",
                                        "neptune-db:CancelLoaderJob",
                                        "neptune-db:GetEngineStatus",
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_loader_iam_query_and_delete_for_drop(template):
    # Spec 004 FR-8: the batched DELETE + ASK that clear a failed tag's graph.
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
                                        "neptune-db:DeleteDataViaQuery",
                                    ]
                                ),
                                "Resource": {
                                    "Fn::Join": [
                                        "",
                                        Match.array_with(
                                            [":cluster-ABCDEFGHIJKLMNOP/*"]
                                        ),
                                    ]
                                },
                            }
                        )
                    ]
                )
            }
        },
    )


def test_loader_timeout_fits_one_drop_batch(template):
    # A drop batch may run up to Neptune's 120 s query timeout, plus its ASK.
    template.has_resource_properties(
        "AWS::Lambda::Function", {"Handler": "loader.handler", "Timeout": 150}
    )


def test_loader_drop_batch_size_comes_from_config(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "loader.handler",
            "Environment": {
                "Variables": Match.object_like(
                    {"DROP_BATCH_SIZE": str(DROP_BATCH_SIZE)}
                )
            },
        },
    )


@pytest.mark.parametrize("env_name, expected", [("dev", 100000), ("prod", 300000)])
def test_drop_batch_size_per_env(env_name, expected):
    config = load_context_config(env_name)
    assert config["NEPTUNE_PIPELINE"]["drop_batch_size"] == expected


def test_dev_pipeline_block_keeps_base_values():
    # dev.yaml's NEPTUNE_PIPELINE replaces base.yaml's (shallow merge); it must repeat the rest.
    dev, prod = load_context_config("dev"), load_context_config("prod")
    for key in ("enabled", "graph_uri_template", "parallelism", "wait_seconds"):
        assert dev["NEPTUNE_PIPELINE"][key] == prod["NEPTUNE_PIPELINE"][key], key


def test_neptune_ingress_rule_on_port_8182(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"FromPort": 8182, "ToPort": 8182, "IpProtocol": "tcp"},
    )


# ---------------------------------------------------------------------------
# DynamoDB tracking table
# ---------------------------------------------------------------------------


def test_tracking_table_has_composite_key(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": Match.array_equals(
                [
                    {"AttributeName": "portal", "KeyType": "HASH"},
                    {"AttributeName": "snapshot", "KeyType": "RANGE"},
                ]
            )
        },
    )


def test_tracking_table_has_no_ttl(template):
    # Append-only audit trail — must not expire load records.
    tables = template.find_resources("AWS::DynamoDB::Table")
    for table in tables.values():
        assert "TimeToLiveSpecification" not in table["Properties"]


# ---------------------------------------------------------------------------
# Step Functions + EventBridge
# ---------------------------------------------------------------------------


def test_state_machine_created(template):
    template.resource_count_is("AWS::StepFunctions::StateMachine", 1)


def _state_machine_definition(template) -> str:
    (sm,) = template.find_resources("AWS::StepFunctions::StateMachine").values()
    parts = sm["Properties"]["DefinitionString"]["Fn::Join"][1]
    return "".join(p if isinstance(p, str) else "" for p in parts)


def _states(template) -> dict:
    return json.loads(_state_machine_definition(template))["States"]


def test_prepare_passes_execution_start_time_as_released_at(template):
    # Spec 004 FR-4: arrival order is the execution start time, not anything in the event.
    prepare = _states(template)["Prepare"]
    assert prepare["Parameters"]["action"] == "prepare"
    assert prepare["Parameters"]["released_at.$"] == "$$.Execution.StartTime"


def test_execution_starts_at_prepare_and_routes_each_case(template):
    states = _states(template)
    assert json.loads(_state_machine_definition(template))["StartAt"] == "Prepare"
    assert states["Prepare"]["Next"] == "Route"
    assert states["Prepare"]["Catch"][0]["Next"] == "BadManifestKey"
    routes = {c["StringEquals"]: c["Next"] for c in states["Route"]["Choices"]}
    assert routes == {
        "skip": "LoadSkipped",
        "publish": "Publish",  # FR-7: no reload
        "drop": "DropBatch",  # FR-8
        "load": "StartLoad",
    }
    assert states["Route"]["Default"] == "BadManifestKey"


def test_drop_loops_until_the_graph_is_empty_then_loads(template):
    states = _states(template)
    assert states["DropBatch"]["Next"] == "GraphCleared?"
    (loop,) = states["GraphCleared?"]["Choices"]
    assert (loop["Variable"], loop["BooleanEquals"], loop["Next"]) == (
        "$.drop.remaining",
        True,
        "DropBatch",
    )
    assert states["GraphCleared?"]["Default"] == "StartLoad"


def test_only_a_completed_load_or_a_loaded_tag_publishes(template):
    # FR-10: a failed load is never published.
    states = _states(template)
    into_publish = sorted(
        name
        for name, st in states.items()
        if st.get("Next") == "Publish"
        or any(c.get("Next") == "Publish" for c in st.get("Choices", []))
    )
    assert into_publish == ["RecordSuccess", "Route"]
    assert states["RecordFailure"]["Next"] == "LoadFailed"
    assert states["Publish"]["Next"] == "LoadSucceeded"
    assert (
        "Catch" not in states["Publish"]
    )  # a publish failure must not mark the tag for drop


def test_failures_after_the_claim_mark_the_row_error(template):
    states = _states(template)
    for name in (
        "StartLoad",
        "CheckLoad",
        "DropBatch",
        "RecordSuccess",
        "RecordFailure",
    ):
        (catch,) = states[name]["Catch"]
        assert catch["Next"] == "Abort", name
        assert catch["ResultPath"] == "$.error"
    assert states["Abort"]["Parameters"]["action"] == "abort"
    assert states["Abort"]["Next"] == "LoadAborted"
    assert states["LoadAborted"]["Type"] == "Fail"


def test_eventbridge_rule_matches_manifest_suffix(template):
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": Match.object_like(
                {
                    "detail-type": ["Object Created"],
                    "source": ["aws.s3"],
                    "detail": Match.object_like(
                        {"object": {"key": [{"suffix": "manifest.ttl"}]}}
                    ),
                }
            )
        },
    )


def test_rule_targets_state_machine(template):
    template.has_resource_properties(
        "AWS::Events::Rule",
        {"Targets": Match.array_with([Match.object_like({"Arn": Match.any_value()})])},
    )


def test_rule_dlq_created(template):
    # Rule DLQ + any Step Functions internal queues — at least one SQS queue.
    template.resource_count_is("AWS::SQS::Queue", 1)


# ---------------------------------------------------------------------------
# Alarms + outputs
# ---------------------------------------------------------------------------


def test_alarms_created(template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 2)


def test_outputs_exist(template):
    template.has_output("LoadPipelineStateMachineArn", {})
    template.has_output("LoadTrackingTableName", {})
