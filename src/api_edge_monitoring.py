"""Alarms and a dashboard for the unified API (spec 001 Phase 3).

At least what the old APIs have in the monitoring stack (constitution VII), split per route
by the stage's method-level metrics, plus the scaling-path signals from the plan: daily
request volume, 429s on the status methods, and the age of the oldest queued job.

Thresholds come from `API_EDGE_ALARMS` in config. No alarm has an action yet, like the
monitoring stack's DLQ alarms.
"""

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from src.contract import operations

# 429s don't have an API Gateway metric of their own, so they're counted from the access log
THROTTLED_NAMESPACE = "SageBrain/Api"
THROTTLED_METRIC = "Throttled"
STATUS_PATHS = ("/query/{job_id}", "/ask/{job_id}")

ONE_MINUTE = cdk.Duration.minutes(1)
FIVE_MINUTES = cdk.Duration.minutes(5)


class ApiEdgeMonitoring(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        api: apigw.RestApi,
        stage_name: str,
        access_log_group: logs.ILogGroup,
        functions: dict[str, lambda_.IFunction],
        queues: dict[str, sqs.IQueue],
        thresholds: dict,
    ) -> None:
        super().__init__(scope, construct_id)

        api_dimensions = {"ApiName": api.rest_api_name, "Stage": stage_name}

        def api_metric(metric_name, statistic="Sum", period=ONE_MINUTE, **dimensions):
            return cw.Metric(
                namespace="AWS/ApiGateway",
                metric_name=metric_name,
                dimensions_map={**api_dimensions, **dimensions},
                statistic=statistic,
                period=period,
            )

        def route_metric(metric_name, method, path, statistic="Sum"):
            return api_metric(
                metric_name, statistic, Method=method, Resource=path
            ).with_(label=f"{method} {path}")

        def throttled_metric(path, period=ONE_MINUTE):
            return cw.Metric(
                namespace=THROTTLED_NAMESPACE,
                metric_name=THROTTLED_METRIC,
                dimensions_map={"ResourcePath": path},
                statistic="Sum",
                period=period,
            )

        logs.MetricFilter(
            self,
            "ThrottledFilter",
            log_group=access_log_group,
            metric_namespace=THROTTLED_NAMESPACE,
            metric_name=THROTTLED_METRIC,
            filter_pattern=logs.FilterPattern.string_value("$.status", "=", "429"),
            metric_value="1",
            dimensions={"ResourcePath": "$.resourcePath"},
        )

        # -------------------
        # Alarms
        # -------------------
        def alarm(construct_id, metric, threshold, description):
            return cw.Alarm(
                self,
                construct_id,
                metric=metric,
                threshold=threshold,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                alarm_description=description,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )

        alarms = [
            alarm(
                "ServerErrorsAlarm",
                api_metric("5XXError", period=FIVE_MINUTES),
                thresholds["server_errors_per_5_minutes"],
                "5XX responses from the unified API (handler errors, timeouts, Synapse down)",
            ),
            alarm(
                "RequestVolumeAlarm",
                api_metric("Count", period=cdk.Duration.days(1)),
                thresholds["requests_per_day"],
                "Requests per day reached the point where a fixed-cost front door is "
                "cheaper; a week above it reopens spec 001 R-1",
            ),
        ]
        for path in STATUS_PATHS:
            alarms.append(
                alarm(
                    f"Throttled{path.split('/')[1].title()}StatusAlarm",
                    throttled_metric(path, period=FIVE_MINUTES),
                    thresholds["status_throttled_per_5_minutes"],
                    f"GET {path} returned 429: polling outgrew the status throttle "
                    "(spec 001 scaling path 1)",
                )
            )
        for name, queue in queues.items():
            alarms.append(
                alarm(
                    f"{name.title()}QueueAgeAlarm",
                    queue.metric_approximate_age_of_oldest_message(
                        statistic="Maximum", period=FIVE_MINUTES
                    ),
                    thresholds["queue_oldest_message_seconds"],
                    f"The oldest {name} job has waited in the queue too long: the workers "
                    "can't keep up (spec 001 scaling path 2)",
                )
            )

        # -------------------
        # Dashboard
        # -------------------
        routes = sorted(operations())
        dashboard = cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"{cdk.Stack.of(self).stack_name}-overview",
            default_interval=cdk.Duration.hours(3),
        )
        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# Unified API  (`/query` · `/ask` · `/api`), per route",
                width=24,
                height=1,
            )
        )
        dashboard.add_widgets(
            *(
                cw.GraphWidget(
                    title=title,
                    left=[
                        route_metric(metric_name, m, p, statistic) for p, m in routes
                    ],
                    width=12,
                )
                for title, metric_name, statistic in (
                    ("Requests", "Count", "Sum"),
                    ("4XX", "4XXError", "Sum"),
                    ("5XX", "5XXError", "Sum"),
                    ("Latency p99 (ms)", "Latency", "p99"),
                )
            ),
            cw.GraphWidget(
                title="429 Throttled (from the access log)",
                left=[
                    throttled_metric(p).with_(label=p)
                    for p in sorted({p for p, _ in routes})
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Lambda Errors",
                left=[
                    fn.metric_errors(label=name, period=ONE_MINUTE)
                    for name, fn in functions.items()
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Lambda Duration p99 (ms)",
                left=[
                    fn.metric_duration(statistic="p99", label=name, period=ONE_MINUTE)
                    for name, fn in functions.items()
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Oldest Queued Job (s)",
                left=[
                    queue.metric_approximate_age_of_oldest_message(
                        statistic="Maximum", label=name, period=ONE_MINUTE
                    )
                    for name, queue in queues.items()
                ],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.AlarmStatusWidget(alarms=alarms, title="Alarms", width=24)
        )
