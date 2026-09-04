import json

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_ce as ce
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs
from constructs import Construct


class MonitoringStack(cdk.Stack):
    """
    CloudWatch dashboard covering both APIs, all Lambdas, SQS queues/DLQs,
    DynamoDB tables, and Neptune.

    Layout (top to bottom):
      - Query API  (API GW, Lambdas, SQS + DLQ alarm, DynamoDB)
      - Agent API  (API GW, Lambdas + concurrency, SQS + DLQ alarm, DynamoDB)
      - Neptune    (CPU, memory, SPARQL req/s, cache hit ratio)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        # Query API resources
        query_api: apigw.RestApi,
        query_submit_fn: lambda_.Function,
        query_status_fn: lambda_.Function,
        query_worker_fn: lambda_.Function,
        query_job_queue: sqs.Queue,
        query_dlq: sqs.Queue,
        query_job_table: dynamodb.Table,
        # Agent API resources
        agent_api: apigw.RestApi,
        agent_submit_fn: lambda_.Function,
        agent_status_fn: lambda_.Function,
        agent_worker_fn: lambda_.Function,
        agent_job_queue: sqs.Queue,
        agent_dlq: sqs.Queue,
        agent_job_table: dynamodb.Table,
        # Neptune
        neptune_cluster_id: str,
        cost_monitoring_config: dict | None = None,
        resource_tags: dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        def _apigw_metric(api: apigw.RestApi, metric_name: str, **kwargs) -> cw.Metric:
            return cw.Metric(
                namespace="AWS/ApiGateway",
                metric_name=metric_name,
                dimensions_map={"ApiName": api.rest_api_name},
                **kwargs,
            )

        def _neptune_metric(metric_name: str, **kwargs) -> cw.Metric:
            return cw.Metric(
                namespace="AWS/Neptune",
                metric_name=metric_name,
                dimensions_map={"DBClusterIdentifier": neptune_cluster_id},
                **kwargs,
            )

        def _resource_tags():
            if not resource_tags:
                return None
            return [
                {"key": key, "value": value}
                for key, value in sorted(resource_tags.items())
            ]

        dashboard = cw.Dashboard(
            self,
            "SageBrainDashboard",
            dashboard_name=f"{construct_id}-overview",
            default_interval=cdk.Duration.hours(3),
        )

        # Cost monitoring
        cost_monitoring_config = cost_monitoring_config or {}
        self._setup_service_anomaly_detection(
            construct_id, cost_monitoring_config, _resource_tags
        )
        self._setup_account_budget(construct_id, cost_monitoring_config)

        # ------------------------------------------------------------------ #
        # Query API
        # ------------------------------------------------------------------ #
        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# Query API  (`POST /query` · `GET /query/{job_id}`)",
                width=24,
                height=1,
            )
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Requests & Errors",
                left=[
                    _apigw_metric(
                        query_api,
                        "Count",
                        statistic="Sum",
                        label="Requests",
                        period=cdk.Duration.minutes(1),
                    )
                ],
                right=[
                    _apigw_metric(
                        query_api,
                        "4XXError",
                        statistic="Sum",
                        label="4XX",
                        period=cdk.Duration.minutes(1),
                    ),
                    _apigw_metric(
                        query_api,
                        "5XXError",
                        statistic="Sum",
                        label="5XX",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Latency (ms)",
                left=[
                    _apigw_metric(
                        query_api,
                        "Latency",
                        statistic="p50",
                        label="p50",
                        period=cdk.Duration.minutes(1),
                    ),
                    _apigw_metric(
                        query_api,
                        "Latency",
                        statistic="p99",
                        label="p99",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda Errors",
                left=[
                    query_submit_fn.metric_errors(
                        label="submit", period=cdk.Duration.minutes(1)
                    ),
                    query_status_fn.metric_errors(
                        label="status", period=cdk.Duration.minutes(1)
                    ),
                    query_worker_fn.metric_errors(
                        label="worker", period=cdk.Duration.minutes(1)
                    ),
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="Worker Duration (ms)",
                left=[
                    query_worker_fn.metric_duration(
                        statistic="p50", label="p50", period=cdk.Duration.minutes(1)
                    ),
                    query_worker_fn.metric_duration(
                        statistic="p99", label="p99", period=cdk.Duration.minutes(1)
                    ),
                    query_worker_fn.metric_duration(
                        statistic="Maximum", label="max", period=cdk.Duration.minutes(1)
                    ),
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="SQS Queue Depth",
                left=[
                    query_job_queue.metric_approximate_number_of_messages_visible(
                        label="visible", period=cdk.Duration.minutes(1)
                    ),
                    query_job_queue.metric_approximate_number_of_messages_not_visible(
                        label="in-flight", period=cdk.Duration.minutes(1)
                    ),
                ],
                width=8,
            ),
        )

        query_dlq_alarm = cw.Alarm(
            self,
            "QueryDLQAlarm",
            metric=query_dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(1)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Query jobs landing in DLQ — all 2 attempts failed",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        dashboard.add_widgets(
            cw.AlarmWidget(
                title="Query DLQ (alarm if > 0 messages)",
                alarm=query_dlq_alarm,
                width=8,
            ),
            cw.GraphWidget(
                title="DynamoDB — Query Jobs Latency (ms)",
                left=[
                    query_job_table.metric_successful_request_latency(
                        dimensions_map={
                            "TableName": query_job_table.table_name,
                            "Operation": "PutItem",
                        },
                        label="PutItem",
                        period=cdk.Duration.minutes(1),
                    ),
                    query_job_table.metric_successful_request_latency(
                        dimensions_map={
                            "TableName": query_job_table.table_name,
                            "Operation": "GetItem",
                        },
                        label="GetItem",
                        period=cdk.Duration.minutes(1),
                    ),
                    query_job_table.metric_successful_request_latency(
                        dimensions_map={
                            "TableName": query_job_table.table_name,
                            "Operation": "UpdateItem",
                        },
                        label="UpdateItem",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=16,
            ),
        )

        # ------------------------------------------------------------------ #
        # Agent API
        # ------------------------------------------------------------------ #
        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# Agent API  (`POST /ask` · `GET /ask/{job_id}`)",
                width=24,
                height=1,
            )
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Requests & Errors",
                left=[
                    _apigw_metric(
                        agent_api,
                        "Count",
                        statistic="Sum",
                        label="Requests",
                        period=cdk.Duration.minutes(1),
                    )
                ],
                right=[
                    _apigw_metric(
                        agent_api,
                        "4XXError",
                        statistic="Sum",
                        label="4XX",
                        period=cdk.Duration.minutes(1),
                    ),
                    _apigw_metric(
                        agent_api,
                        "5XXError",
                        statistic="Sum",
                        label="5XX",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Latency (ms)",
                left=[
                    _apigw_metric(
                        agent_api,
                        "Latency",
                        statistic="p50",
                        label="p50",
                        period=cdk.Duration.minutes(1),
                    ),
                    _apigw_metric(
                        agent_api,
                        "Latency",
                        statistic="p99",
                        label="p99",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda Errors & Throttles",
                left=[
                    agent_submit_fn.metric_errors(
                        label="submit errors", period=cdk.Duration.minutes(1)
                    ),
                    agent_status_fn.metric_errors(
                        label="status errors", period=cdk.Duration.minutes(1)
                    ),
                    agent_worker_fn.metric_errors(
                        label="worker errors", period=cdk.Duration.minutes(1)
                    ),
                    agent_worker_fn.metric_throttles(
                        label="worker throttles", period=cdk.Duration.minutes(1)
                    ),
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="Worker Duration (ms)",
                left=[
                    agent_worker_fn.metric_duration(
                        statistic="p50", label="p50", period=cdk.Duration.minutes(1)
                    ),
                    agent_worker_fn.metric_duration(
                        statistic="p99", label="p99", period=cdk.Duration.minutes(1)
                    ),
                    agent_worker_fn.metric_duration(
                        statistic="Maximum", label="max", period=cdk.Duration.minutes(1)
                    ),
                ],
                width=8,
            ),
            # Concurrency is critical — agent worker is capped at 10
            cw.GraphWidget(
                title="Worker Concurrency (max=10)",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="ConcurrentExecutions",
                        dimensions_map={"FunctionName": agent_worker_fn.function_name},
                        statistic="Maximum",
                        label="concurrent",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=8,
            ),
        )

        agent_dlq_alarm = cw.Alarm(
            self,
            "AgentDLQAlarm",
            metric=agent_dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(1)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Agent jobs landing in DLQ — all 2 attempts failed",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        dashboard.add_widgets(
            cw.AlarmWidget(
                title="Agent DLQ (alarm if > 0 messages)",
                alarm=agent_dlq_alarm,
                width=8,
            ),
            cw.GraphWidget(
                title="SQS Queue Depth",
                left=[
                    agent_job_queue.metric_approximate_number_of_messages_visible(
                        label="visible", period=cdk.Duration.minutes(1)
                    ),
                    agent_job_queue.metric_approximate_number_of_messages_not_visible(
                        label="in-flight", period=cdk.Duration.minutes(1)
                    ),
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="DynamoDB — Agent Jobs Latency (ms)",
                left=[
                    agent_job_table.metric_successful_request_latency(
                        dimensions_map={
                            "TableName": agent_job_table.table_name,
                            "Operation": "PutItem",
                        },
                        label="PutItem",
                        period=cdk.Duration.minutes(1),
                    ),
                    agent_job_table.metric_successful_request_latency(
                        dimensions_map={
                            "TableName": agent_job_table.table_name,
                            "Operation": "GetItem",
                        },
                        label="GetItem",
                        period=cdk.Duration.minutes(1),
                    ),
                    agent_job_table.metric_successful_request_latency(
                        dimensions_map={
                            "TableName": agent_job_table.table_name,
                            "Operation": "UpdateItem",
                        },
                        label="UpdateItem",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=8,
            ),
        )

        # ------------------------------------------------------------------ #
        # Neptune
        # ------------------------------------------------------------------ #
        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# Neptune",
                width=24,
                height=1,
            )
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="CPU Utilization (%)",
                left=[
                    _neptune_metric(
                        "CPUUtilization",
                        statistic="Average",
                        label="CPU",
                        period=cdk.Duration.minutes(1),
                    )
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="Freeable Memory (bytes)",
                left=[
                    _neptune_metric(
                        "FreeableMemory",
                        statistic="Average",
                        label="Free Memory",
                        period=cdk.Duration.minutes(1),
                    )
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="Buffer Cache Hit Ratio (%)",
                left=[
                    _neptune_metric(
                        "BufferCacheHitRatio",
                        statistic="Average",
                        label="Cache Hit %",
                        period=cdk.Duration.minutes(1),
                    )
                ],
                width=8,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="SPARQL Requests / sec",
                left=[
                    _neptune_metric(
                        "SparqlRequestsPerSec",
                        statistic="Average",
                        label="req/s",
                        period=cdk.Duration.minutes(1),
                    )
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Network Throughput (bytes/s)",
                left=[
                    _neptune_metric(
                        "NetworkReceiveThroughput",
                        statistic="Average",
                        label="in",
                        period=cdk.Duration.minutes(1),
                    ),
                    _neptune_metric(
                        "NetworkTransmitThroughput",
                        statistic="Average",
                        label="out",
                        period=cdk.Duration.minutes(1),
                    ),
                ],
                width=12,
            ),
        )

        # -------------------------------------------------------------- #
        # Outputs
        # -------------------------------------------------------------- #
        result = f"https://{self.region}.console.aws.amazon.com/cloudwatch/home#dashboards:name={construct_id}-overview"
        cdk.CfnOutput(
            self,
            "DashboardUrl",
            value=result,
            description="CloudWatch dashboard URL",
        )

    def _setup_service_anomaly_detection(
        self, construct_id: str, config: dict, resource_tags_fn
    ) -> None:
        """Set up service-level cost anomaly detection."""
        service_config = config.get("service_anomaly", {})
        if not service_config.get("enabled", False):
            return

        threshold_usd = service_config.get("threshold_usd")
        email_subscribers = service_config.get("email_subscribers", [])

        # Validate threshold
        try:
            threshold_value = float(threshold_usd) if threshold_usd is not None else 0
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"COST_MONITORING.service_anomaly.threshold_usd must be a "
                f"positive number, got: {threshold_usd!r}"
            ) from exc
        if threshold_usd is None or threshold_value <= 0:
            raise ValueError(
                "COST_MONITORING.service_anomaly.threshold_usd must be set "
                "to a positive number"
            )

        # Validate email subscribers
        if not isinstance(email_subscribers, list):
            raise ValueError(
                f"COST_MONITORING.service_anomaly.email_subscribers must be "
                f"a list, got: {type(email_subscribers).__name__}"
            )
        if not email_subscribers:
            raise ValueError(
                "COST_MONITORING.service_anomaly.email_subscribers must "
                "contain at least one email"
            )

        # Create monitor
        service_monitor = ce.CfnAnomalyMonitor(
            self,
            "ServiceCostAnomalyMonitor",
            monitor_name=f"{construct_id}-service-cost-anomalies",
            monitor_type="DIMENSIONAL",
            monitor_dimension="SERVICE",
            resource_tags=resource_tags_fn(),
        )

        # Create subscription
        threshold_expression = json.dumps(
            {
                "Dimensions": {
                    "Key": "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
                    "MatchOptions": ["GREATER_THAN_OR_EQUAL"],
                    "Values": [str(threshold_usd)],
                }
            },
            separators=(",", ":"),
        )
        ce.CfnAnomalySubscription(
            self,
            "ServiceCostAnomalySubscription",
            subscription_name=f"{construct_id}-service-cost-anomaly-subscription",
            frequency=service_config.get("frequency", "IMMEDIATE"),
            monitor_arn_list=[service_monitor.attr_monitor_arn],
            subscribers=[
                ce.CfnAnomalySubscription.SubscriberProperty(
                    address=email, type="EMAIL"
                )
                for email in email_subscribers
            ],
            threshold_expression=threshold_expression,
            resource_tags=resource_tags_fn(),
        )

    def _setup_account_budget(self, construct_id: str, config: dict) -> None:
        """Set up account-level monthly budget."""
        budget_config = config.get("account_budget", {})
        if not budget_config.get("enabled", False):
            return

        monthly_limit = budget_config.get("monthly_limit_usd")
        alert_thresholds = budget_config.get("alert_thresholds", [80, 100])
        email_subscribers = budget_config.get("email_subscribers", [])

        # Validate monthly limit
        try:
            limit_value = float(monthly_limit) if monthly_limit is not None else 0
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"COST_MONITORING.account_budget.monthly_limit_usd must be a "
                f"positive number, got: {monthly_limit!r}"
            ) from exc
        if monthly_limit is None or limit_value <= 0:
            raise ValueError(
                "COST_MONITORING.account_budget.monthly_limit_usd must be set "
                "to a positive number"
            )

        # Validate alert thresholds
        if not isinstance(alert_thresholds, list) or not alert_thresholds:
            raise ValueError(
                f"COST_MONITORING.account_budget.alert_thresholds must be a "
                f"non-empty list, got: {alert_thresholds!r}"
            )

        # Validate email subscribers
        if not isinstance(email_subscribers, list):
            raise ValueError(
                f"COST_MONITORING.account_budget.email_subscribers must be a "
                f"list, got: {type(email_subscribers).__name__}"
            )
        if not email_subscribers:
            raise ValueError(
                "COST_MONITORING.account_budget.email_subscribers must "
                "contain at least one email"
            )

        # Create budget
        budgets.CfnBudget(
            self,
            "AccountMonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{construct_id}-monthly-budget",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=limit_value,
                    unit="USD",
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=email,
                            subscription_type="EMAIL",
                        )
                        for email in email_subscribers
                    ],
                )
                for threshold in alert_thresholds
            ],
        )
