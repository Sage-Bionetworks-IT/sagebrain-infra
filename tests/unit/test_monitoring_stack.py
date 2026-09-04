import aws_cdk as cdk
import pytest
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_lambda as lambda_
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

    def _api(name: str, api_name: str) -> apigw.RestApi:
        api = apigw.RestApi(stub, name, rest_api_name=api_name)
        # RestApi requires at least one method to pass CDK validation
        api.root.add_method("GET", apigw.MockIntegration())
        return api

    query_api = _api("QueryApi", "neptune-query-api")
    agent_api = _api("AgentApi", "neptune-agent-api")

    stack = MonitoringStack(
        app,
        "TestMonitoringStack",
        query_api=query_api,
        query_submit_fn=_fn("QSubmit"),
        query_status_fn=_fn("QStatus"),
        query_worker_fn=_fn("QWorker"),
        query_job_queue=_queue("QQueue"),
        query_dlq=_queue("QDLQ"),
        query_job_table=_table("QTable"),
        agent_api=agent_api,
        agent_submit_fn=_fn("ASubmit"),
        agent_status_fn=_fn("AStatus"),
        agent_worker_fn=_fn("AWorker"),
        agent_job_queue=_queue("AQueue"),
        agent_dlq=_queue("ADLQ"),
        agent_job_table=_table("ATable"),
        neptune_cluster_id="cluster-TESTCLUSTERID",
        cost_monitoring_config={
            "service_anomaly": {
                "enabled": True,
                "threshold_usd": 25,
                "frequency": "IMMEDIATE",
                "email_subscribers": [
                    "owner1@example.com",
                    "owner2@example.com",
                ],
            },
            "account_budget": {
                "enabled": True,
                "monthly_limit_usd": 1000,
                "alert_thresholds": [80, 100, 120],
                "email_subscribers": [
                    "owner1@example.com",
                    "owner2@example.com",
                ],
            },
        },
        resource_tags={"CostCenter": "NO PROGRAM / 000000"},
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


def test_two_dlq_alarms_created(template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 2)


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


def test_service_anomaly_monitor_created(template):
    # Should have only service-level anomaly monitor (account uses budget instead)
    template.resource_count_is("AWS::CE::AnomalyMonitor", 1)


def test_service_anomaly_monitor_properties(template):
    template.has_resource_properties(
        "AWS::CE::AnomalyMonitor",
        {
            "MonitorType": "DIMENSIONAL",
            "MonitorDimension": "SERVICE",
            "MonitorName": "TestMonitoringStack-service-cost-anomalies",
            "ResourceTags": [{"Key": "CostCenter", "Value": "NO PROGRAM / 000000"}],
        },
    )


def test_service_anomaly_subscription_created(template):
    # Should have only service-level anomaly subscription
    template.resource_count_is("AWS::CE::AnomalySubscription", 1)


def test_service_anomaly_subscription_properties(template):
    template.has_resource_properties(
        "AWS::CE::AnomalySubscription",
        {
            "Frequency": "IMMEDIATE",
            "SubscriptionName": "TestMonitoringStack-service-cost-anomaly-subscription",
            "Subscribers": [
                {"Address": "owner1@example.com", "Type": "EMAIL"},
                {"Address": "owner2@example.com", "Type": "EMAIL"},
            ],
            "ThresholdExpression": (
                '{"Dimensions":{"Key":"ANOMALY_TOTAL_IMPACT_ABSOLUTE",'
                '"MatchOptions":["GREATER_THAN_OR_EQUAL"],"Values":["25"]}}'
            ),
            "ResourceTags": [{"Key": "CostCenter", "Value": "NO PROGRAM / 000000"}],
        },
    )


def test_account_monthly_budget_created(template):
    # Should have account-level monthly budget
    template.resource_count_is("AWS::Budgets::Budget", 1)


def test_account_monthly_budget_properties(template):
    template.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": {
                "BudgetName": "TestMonitoringStack-monthly-budget",
                "BudgetType": "COST",
                "TimeUnit": "MONTHLY",
                "BudgetLimit": {
                    "Amount": 1000,
                    "Unit": "USD",
                },
            },
        },
    )


def test_account_budget_has_threshold_notifications(template):
    # Should have notifications for 80%, 100%, 120%
    template.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "NotificationsWithSubscribers": [
                {
                    "Notification": {
                        "ComparisonOperator": "GREATER_THAN",
                        "NotificationType": "ACTUAL",
                        "Threshold": 80,
                        "ThresholdType": "PERCENTAGE",
                    },
                    "Subscribers": [
                        {
                            "Address": "owner1@example.com",
                            "SubscriptionType": "EMAIL",
                        },
                        {
                            "Address": "owner2@example.com",
                            "SubscriptionType": "EMAIL",
                        },
                    ],
                },
                {
                    "Notification": {
                        "ComparisonOperator": "GREATER_THAN",
                        "NotificationType": "ACTUAL",
                        "Threshold": 100,
                        "ThresholdType": "PERCENTAGE",
                    },
                    "Subscribers": [
                        {
                            "Address": "owner1@example.com",
                            "SubscriptionType": "EMAIL",
                        },
                        {
                            "Address": "owner2@example.com",
                            "SubscriptionType": "EMAIL",
                        },
                    ],
                },
                {
                    "Notification": {
                        "ComparisonOperator": "GREATER_THAN",
                        "NotificationType": "ACTUAL",
                        "Threshold": 120,
                        "ThresholdType": "PERCENTAGE",
                    },
                    "Subscribers": [
                        {
                            "Address": "owner1@example.com",
                            "SubscriptionType": "EMAIL",
                        },
                        {
                            "Address": "owner2@example.com",
                            "SubscriptionType": "EMAIL",
                        },
                    ],
                },
            ],
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


def test_dashboard_contains_query_api_section(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "Query API" in combined


def test_dashboard_contains_agent_api_section(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "Agent API" in combined


def test_dashboard_contains_neptune_section(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "Neptune" in combined


def test_dashboard_references_neptune_cluster_id(template):
    parts = _dashboard_body(template)
    combined = "".join(str(p) for p in parts)
    assert "cluster-TESTCLUSTERID" in combined
