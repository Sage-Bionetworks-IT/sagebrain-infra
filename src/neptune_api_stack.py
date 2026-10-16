import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from src.core_layer import sagebrain_core_layer

_BUNDLING = cdk.BundlingOptions(
    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
    command=[
        "bash",
        "-c",
        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
    ],
)


class NeptuneApiStack(cdk.Stack):
    """
    Query job backend for the unified API's /query routes (app-*-api, spec 001).

    Flow:
      POST /query (app-*-api)  →  SQS  →  worker Lambda (query.py)  →  DynamoDB
      GET /query/{job_id} (app-*-api)  →  DynamoDB

    Decouples HTTP from SPARQL execution so Neptune's 40s+ complex queries
    don't hit API Gateway's 29s hard timeout.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_read_endpoint: str,
        neptune_cluster_resource_id: str,
        neptune_security_group: ec2.SecurityGroup,
        query_worker_concurrency: int,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # DynamoDB — query job store
        # -------------------
        self.job_table = dynamodb.Table(
            self,
            "QueryJobTable",
            table_name=f"{construct_id}-query-jobs",
            partition_key=dynamodb.Attribute(
                name="job_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # -------------------
        # SQS — query job queue + DLQ
        # -------------------
        self.dlq = sqs.Queue(
            self,
            "QueryJobDLQ",
            retention_period=cdk.Duration.days(14),
        )
        self.job_queue = sqs.Queue(
            self,
            "QueryJobQueue",
            visibility_timeout=cdk.Duration.seconds(90),  # > worker Lambda timeout
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=2, queue=self.dlq),
        )

        # -------------------
        # Lambda security group (worker needs VPC to reach Neptune)
        # -------------------
        self.lambda_sg = ec2.SecurityGroup(
            self,
            "NeptuneQueryFunctionSG",
            vpc=vpc,
            description="Security group for Neptune query Lambda",
            allow_all_outbound=True,
        )

        ec2.CfnSecurityGroupIngress(
            self,
            "LambdaToNeptuneIngress",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.lambda_sg.security_group_id,
        )

        # -------------------
        # Worker Lambda — SQS-triggered SPARQL executor (needs VPC)
        # -------------------
        self.core_layer = sagebrain_core_layer(self)
        self.query_fn = lambda_.Function(
            self,
            "NeptuneQueryFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="query.handler",
            code=lambda_.Code.from_asset("src/lambda", bundling=_BUNDLING),
            layers=[self.core_layer],
            vpc=vpc,
            security_groups=[self.lambda_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                "NEPTUNE_ENDPOINT": neptune_read_endpoint,
                "JOB_TABLE_NAME": self.job_table.table_name,
            },
            timeout=cdk.Duration.seconds(75),  # Neptune complex queries can take 40s+
            memory_size=512,
            # Per-env cap on concurrent Neptune queries; extra jobs wait in SQS (spec 001 D-14)
            reserved_concurrent_executions=query_worker_concurrency,
        )
        self.job_table.grant_read_write_data(self.query_fn)
        self.query_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.job_queue,
                batch_size=1,
            )
        )

        # IAM: read-only Neptune access scoped to this cluster
        self.query_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "neptune-db:ReadDataViaQuery",
                    "neptune-db:GetEngineStatus",
                    "neptune-db:GetQueryStatus",
                ],
                resources=[
                    f"arn:{self.partition}:neptune-db:{self.region}:{self.account}:{neptune_cluster_resource_id}/*"
                ],
            )
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "QueryJobTableName",
            value=self.job_table.table_name,
            description="DynamoDB table storing query job results",
        )
