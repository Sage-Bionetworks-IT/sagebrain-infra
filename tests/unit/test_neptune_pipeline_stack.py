import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from src.neptune_pipeline_stack import NeptunePipelineStack


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
            "graph_uri_template": "urn:sagebrain:{portal}:{date}",
            "parallelism": "HIGH",
            "wait_seconds": 30,
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
                        "GRAPH_URI_TEMPLATE": "urn:sagebrain:{portal}:{date}",
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
