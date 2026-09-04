import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
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

        dashboard = cw.Dashboard(
            self,
            "SageBrainDashboard",
            dashboard_name=f"{construct_id}-overview",
            default_interval=cdk.Duration.hours(3),
        )

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
