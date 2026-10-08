import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct


class RateLimitStack(cdk.Stack):
    """
    Shared token buckets for `sagebrain_core.ratelimit.DynamoLimiter` (spec 001 Phase 1d).

    One item per bucket key (`{rl:<api>}:global`, `{rl:<api>}:u:<principal>`). Every
    consumer (the agent worker now, the FastAPI task in Phase 3) charges the same items, so
    the 50 rps / 100 burst per-API and per-principal limits hold across all instances.

    It's its own stack, depending on nothing, so both consumers can reference it without a
    cycle and the data stacks (Neptune, jobs) never redeploy for it.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = dynamodb.Table(
            self,
            "RateLimitTable",
            table_name=construct_id,
            partition_key=dynamodb.Attribute(
                name="key", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Idle per-principal buckets expire; the limiter never relies on TTL deletion.
            time_to_live_attribute="expires_at",
            # Buckets are ephemeral: losing them only refills every bucket once.
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=False
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
