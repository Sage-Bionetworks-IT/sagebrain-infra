import aws_cdk as cdk
import pytest
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk.assertions import Template

from src.monitoring_stack import MonitoringStack


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

    # VPC (shared across helper stacks)
    vpc_stack = cdk.Stack(app, "TestVpc")
    ec2.Vpc(vpc_stack, "Vpc", max_azs=2)

    # Stub stack — all supporting resources live here to keep MonitoringStack isolated
    stub = cdk.Stack(app, "Stubs")

    def _fn(name: str) -> lambda_.Function:
        return lambda_.Function(
            stub,
            name,
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_inline("def handler(e, c): pass"),
        )

    def _queue(name: str) -> sqs.Queue:
        return sqs.Queue(stub, name)

    def _table(name: str) -> dynamodb.Table:
        return dynamodb.Table(
            stub,
            name,
            partition_key=dynamodb.Attribute(
                name="job_id", type=dynamodb.AttributeType.STRING
            ),
        )

    stack = MonitoringStack(
        app,
        "TestMonitoringStack",
        query_worker_fn=_fn("QWorker"),
        query_job_queue=_queue("QQueue"),
        query_dlq=_queue("QDLQ"),
        query_job_table=_table("QTable"),
        agent_worker_fn=_fn("AWorker"),
        agent_job_queue=_queue("AQueue"),
        agent_dlq=_queue("ADLQ"),
        agent_job_table=_table("ATable"),
        neptune_cluster_id="cluster-TESTCLUSTERID",
        neptune_instance_ids=["instance-TESTINSTANCEID"],
        neptune_alarms={
            "neptune_cpu_percent": 80,
            "neptune_cpu_minutes": 15,
            "neptune_freeable_memory_mib": 512,
            "neptune_freeable_memory_minutes": 15,
        },
        alarm_topic=sns.Topic(stub, "AlarmTopic"),
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_created(template):
    template.resource_count_is("AWS::CloudWatch::Dashboard", 1)


def test_dashboard_name_contains_stack_id(template):
    template.has_resource_properties(
        "AWS::CloudWatch::Dashboard",
        {"DashboardName": "TestMonitoringStack-overview"},
    )


def test_dashboard_url_output_exists(template):
    template.has_output("DashboardUrl", {})


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------


def test_two_dlq_alarms_and_cpu_and_memory_alarms_per_instance(template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 4)


def test_neptune_instance_cpu_alarm(template):
    # Vanta "SQL database CPU monitored": a CPUUtilization alarm on each DB instance
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "AWS/Neptune",
            "MetricName": "CPUUtilization",
            "Dimensions": [
                {"Name": "DBInstanceIdentifier", "Value": "instance-TESTINSTANCEID"}
            ],
            "Statistic": "Average",
            "Period": 300,
            "Threshold": 80,
            "EvaluationPeriods": 3,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        },
    )


def test_every_alarm_notifies_the_alarm_topic(template):
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert alarms
    for props in (a["Properties"] for a in alarms.values()):
        assert len(props["AlarmActions"]) == 1
        assert "AlarmTopic" in str(props["AlarmActions"][0])


def test_query_dlq_alarm_threshold_is_one(template):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "AlarmDescription": "Query jobs landing in DLQ — all 2 attempts failed",
            "Threshold": 1,
            "EvaluationPeriods": 1,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "TreatMissingData": "notBreaching",
        },
    )


def test_agent_dlq_alarm_threshold_is_one(template):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "AlarmDescription": "Agent jobs landing in DLQ — all 2 attempts failed",
            "Threshold": 1,
            "EvaluationPeriods": 1,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "TreatMissingData": "notBreaching",
        },
    )


def test_neptune_instance_freeable_memory_alarm(template):
    # Vanta "SQL database freeable memory monitored": a FreeableMemory alarm on each DB instance
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "AWS/Neptune",
            "MetricName": "FreeableMemory",
            "Dimensions": [
                {"Name": "DBInstanceIdentifier", "Value": "instance-TESTINSTANCEID"}
            ],
            "Statistic": "Average",
            "Period": 300,
            "Threshold": 512 * 1024 * 1024,
            "EvaluationPeriods": 3,
            "ComparisonOperator": "LessThanThreshold",
        },
    )


# ---------------------------------------------------------------------------
# Dashboard body — key widget presence
# ---------------------------------------------------------------------------


def _dashboard_body(template) -> str:
    dashboards = template.find_resources("AWS::CloudWatch::Dashboard")
    assert len(dashboards) == 1
    props = next(iter(dashboards.values()))["Properties"]
    return props["DashboardBody"]["Fn::Join"][1]  # CDK serialises as Fn::Join


def test_dashboard_contains_query_jobs_section(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "Query jobs" in combined


def test_dashboard_contains_agent_jobs_section(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "Agent jobs" in combined


def test_dashboard_contains_neptune_section(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "Neptune" in combined


def test_dashboard_references_neptune_cluster_id(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "cluster-TESTCLUSTERID" in combined


def test_dashboard_graphs_no_api_gateway_metrics(template):
    """The API's metrics live on the edge stack's dashboard (spec 001 T503)."""
    assert "AWS/ApiGateway" not in "".join(str(p) for p in _dashboard_body(template))
