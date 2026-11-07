import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

# A DLQ message nobody has redriven or purged for an hour. The DLQ's count alarm emails when it
# lands; this emails once more if it is still there. Vanta's "Messaging queue message age
# monitored" test wants an ApproximateAgeOfOldestMessage alarm on every queue, at most 5000 s
DLQ_UNHANDLED_SECONDS = 3600


def dlq_age_alarm(
    scope: Construct,
    construct_id: str,
    dlq: sqs.IQueue,
    what: str,
    alarm_topic: sns.ITopic | None,
) -> cw.Alarm:
    alarm = cw.Alarm(
        scope,
        construct_id,
        metric=dlq.metric_approximate_age_of_oldest_message(
            statistic="Maximum", period=cdk.Duration.minutes(5)
        ),
        threshold=DLQ_UNHANDLED_SECONDS,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        alarm_description=f"{what} has sat in the DLQ for an hour: redrive or purge it",
    )
    if alarm_topic:
        alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))
    return alarm
