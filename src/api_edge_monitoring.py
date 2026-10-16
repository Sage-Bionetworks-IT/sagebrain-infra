"""Alarms and a dashboard for the unified API (spec 001 Phase 3).

At least what the old APIs had in the monitoring stack (constitution VII), split per route
by the stage's method-level metrics, plus the scaling-path signals from the plan: daily
request volume, 429s on the status methods, and the age of the oldest queued job.

Thresholds come from `API_EDGE_ALARMS` in config. Every alarm emails `ALARM_EMAIL` (per env)
through one SNS topic; the address has to confirm the subscription once.
"""

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from src.contract import operations

# Counted from the access log: API Gateway has no 429 metric, and its 5XXError includes the
# 503 that the authorizer returns while Synapse is down (D-13), which happens for ~30 min
# every Sunday. No handler returns 503 (a Lambda error is a 502, a timeout a 504), so a 503
# always means the authorizer failed.
ACCESS_LOG_NAMESPACE = "SageBrain/Api"
THROTTLED_METRIC = "Throttled"
SERVER_ERRORS_METRIC = "ServerErrors"
AUTH_UNAVAILABLE_METRIC = "AuthUnavailable"
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
        alarm_email: str,
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

        def access_log_metric(metric_name, period=ONE_MINUTE, **dimensions):
            return cw.Metric(
                namespace=ACCESS_LOG_NAMESPACE,
                metric_name=metric_name,
                dimensions_map=dimensions or None,
                statistic="Sum",
                period=period,
            )

        def throttled_metric(path, period=ONE_MINUTE):
            return access_log_metric(THROTTLED_METRIC, period, ResourcePath=path)

        def access_log_filter(construct_id, metric_name, pattern, **kwargs):
            logs.MetricFilter(
                self,
                construct_id,
                log_group=access_log_group,
                metric_namespace=ACCESS_LOG_NAMESPACE,
                metric_name=metric_name,
                filter_pattern=pattern,
                metric_value="1",
                **kwargs,
            )

        status = logs.FilterPattern.string_value
        access_log_filter(
            "ThrottledFilter",
            THROTTLED_METRIC,
            status("$.status", "=", "429"),
            dimensions={"ResourcePath": "$.resourcePath"},
        )
        access_log_filter(
            "ServerErrorsFilter",
            SERVER_ERRORS_METRIC,
            logs.FilterPattern.all(
                status("$.status", "=", "5*"), status("$.status", "!=", "503")
            ),
        )
        access_log_filter(
            "AuthUnavailableFilter",
            AUTH_UNAVAILABLE_METRIC,
            status("$.status", "=", "503"),
        )

        # -------------------
        # Alarms
        # -------------------
        self.alarm_topic = sns.Topic(self, "AlarmTopic")
        self.alarm_topic.add_subscription(subscriptions.EmailSubscription(alarm_email))
        alarm_action = cw_actions.SnsAction(self.alarm_topic)

        # Actions fire on a change into ALARM only (no OK action), so an alarm that stays in
        # ALARM sends one email. Those needing several periods don't flap on a one-off spike.
        def alarm(construct_id, metric, threshold, description, periods=1, of=None):
            created = cw.Alarm(
                self,
                construct_id,
                metric=metric,
                threshold=threshold,
                evaluation_periods=of or periods,
                datapoints_to_alarm=periods,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                alarm_description=description,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            created.add_alarm_action(alarm_action)
            return created

        alarms = [
            alarm(
                "ServerErrorsAlarm",
                access_log_metric(SERVER_ERRORS_METRIC, FIVE_MINUTES),
                thresholds["server_errors_per_5_minutes"],
                "5XX responses other than 503 (handler errors, timeouts) in 2 of 3 "
                "5-minute periods",
                periods=2,
                of=3,
            ),
            alarm(
                "SynapseUnavailableAlarm",
                access_log_metric(AUTH_UNAVAILABLE_METRIC, FIVE_MINUTES),
                1,
                f"The authorizer has failed (503) for {thresholds['synapse_outage_minutes']} "
                "minutes: Synapse is down beyond its weekly maintenance, or the authorizer "
                "is misconfigured",
                periods=thresholds["synapse_outage_minutes"] // 5,
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
                    1,
                    f"GET {path} returned 429s for {thresholds['status_throttled_minutes']} "
                    "minutes: polling outgrew the status throttle (spec 001 scaling path 1)",
                    periods=thresholds["status_throttled_minutes"] // 5,
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
                title="429 per path, and 503 = Synapse down (from the access log)",
                left=[
                    throttled_metric(p).with_(label=f"429 {p}")
                    for p in sorted({p for p, _ in routes})
                ],
                right=[access_log_metric(AUTH_UNAVAILABLE_METRIC).with_(label="503")],
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
