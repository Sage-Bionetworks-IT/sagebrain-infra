"""An Errors alarm on every Lambda function the app deploys.

Vanta's "Serverless function error rate monitored (AWS)" test passes a function when an alarm
watches `AWS/Lambda` `Errors` with a `FunctionName` dimension equal to its name. It reads the
alarm's top-level `MetricName`, so the metric has no `label` (that makes it a math alarm, see
docs/memory/gotchas.md).

Every function in the given stacks is covered, CDK's own (bucket notifications, the VPC's
default-SG custom resource, log retention) included, so a new one gets an alarm without a code
change. The alarms live in the calling stack (the monitoring stack): it's the one that can
reach the alarm topic, which the network, Neptune and worker stacks can't import from the API
stack without a cycle.
"""

import re
from collections.abc import Iterable, Iterator, Mapping

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns

FIVE_MINUTES = cdk.Duration.minutes(5)


def lambda_functions(
    stacks: Iterable[cdk.Stack],
) -> Iterator[tuple[cdk.Stack, cdk.CfnResource]]:
    """Every `AWS::Lambda::Function` in `stacks`, L1 or L2, with its stack."""
    for stack in stacks:
        for node in stack.node.find_all():
            if (
                cdk.CfnResource.is_cfn_resource(node)
                and node.cfn_resource_type == "AWS::Lambda::Function"
            ):
                yield stack, node


def function_path(stack: cdk.Stack, fn: cdk.CfnResource) -> str:
    """The function's construct path inside its stack, e.g. `QuerySubmitFunction`."""
    path = fn.node.path.removeprefix(f"{stack.node.path}/")
    return path.removesuffix("/Resource")


def readable_name(stack: cdk.Stack, path: str) -> str:
    """`app-dev-neptune-BucketNotificationsHandler`: no CDK singleton hash, no `::` or `/`."""
    path = re.sub(r"[0-9a-f]{32}(?=/|$)", "", path)
    return re.sub(r"[^A-Za-z0-9]+", "-", f"{stack.stack_name}-{path}").strip("-")


def add_lambda_error_alarms(
    scope: cdk.Stack,
    stacks: Iterable[cdk.Stack],
    errors_per_5_minutes: int,
    sustained_minutes: Mapping[str, int] | None = None,
    alarm_topic: sns.ITopic | None = None,
) -> list[cw.Alarm]:
    """One alarm per function in `stacks`, created in `scope`.

    By default an alarm fires on `errors_per_5_minutes` errors in 5 minutes. A function whose
    construct path (`node.path`, e.g. `app-dev-api/SynapseAuthorizerFunction`) is in
    `sustained_minutes` instead alarms only when it errors in every 5 minutes for that long.
    """
    sustained_minutes = sustained_minutes or {}
    alarms = []
    for stack, fn in lambda_functions(stacks):
        if stack is scope:
            continue
        path = function_path(stack, fn)
        name = readable_name(stack, path)
        minutes = sustained_minutes.get(f"{stack.node.path}/{path}")
        if minutes:
            periods, threshold = minutes // 5, 1
            when = f"errored in every 5 minutes for {minutes} minutes"
        else:
            periods, threshold = 1, errors_per_5_minutes
            when = f"had {threshold}+ errors in 5 minutes"
        alarm = cw.Alarm(
            scope,
            f"LambdaErrors-{name}",
            alarm_name=f"{name}-errors",
            metric=cw.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": fn.ref},
                statistic="Sum",
                period=FIVE_MINUTES,
            ),
            threshold=threshold,
            evaluation_periods=periods,
            datapoints_to_alarm=periods,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description=(
                f"Lambda {stack.stack_name}/{path} {when}. Its logs: /aws/lambda/{fn.ref}"
            ),
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        if alarm_topic:
            alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))
        alarms.append(alarm)
    return alarms
