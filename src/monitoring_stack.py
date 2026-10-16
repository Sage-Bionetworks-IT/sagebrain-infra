import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct


class MonitoringStack(cdk.Stack):
    """
    CloudWatch dashboard covering the job backends (workers, SQS queues/DLQs,
    DynamoDB tables) and Neptune. The API itself has its own dashboard
    (app-*-api-overview, src/api_edge_monitoring.py).

    Layout (top to bottom):
      - Query jobs  (worker, SQS + DLQ alarm, DynamoDB)
      - Agent jobs  (worker + concurrency, SQS + DLQ alarm, DynamoDB)
      - Neptune     (CPU, memory, SPARQL req/s, cache hit ratio)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        # Query job resources
        query_worker_fn: lambda_.Function,
        query_job_queue: sqs.Queue,
        query_dlq: sqs.Queue,
        query_job_table: dynamodb.Table,
        # Agent job resources
        agent_worker_fn: lambda_.Function,
        agent_job_queue: sqs.Queue,
        agent_dlq: sqs.Queue,
        agent_job_table: dynamodb.Table,
        # Neptune
        neptune_cluster_id: str,
        # Alarms email this topic (the unified API's, src/api_edge_monitoring.py); None: no action
        alarm_topic: sns.ITopic | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

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
        # Query jobs
        # ------------------------------------------------------------------ #
        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# Query jobs  (`POST /query` · `GET /query/{job_id}` on the unified API)",
                width=24,
                height=1,
            )
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Worker Errors",
                left=[
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
        if alarm_topic:
            query_dlq_alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))
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
        # Agent jobs
        # ------------------------------------------------------------------ #
        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# Agent jobs  (`POST /ask` · `GET /ask/{job_id}` on the unified API)",
                width=24,
                height=1,
            )
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Worker Errors & Throttles",
                left=[
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
        if alarm_topic:
            agent_dlq_alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))
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
