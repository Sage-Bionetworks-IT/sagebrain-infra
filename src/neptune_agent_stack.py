import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from src.core_layer import sagebrain_core_layer

# Caps Bedrock/Neptune load under burst traffic. Also the rate bound on the agent's in-process
# queries, which skip API Gateway; equals the contract's agent_worker_concurrency (stack test).
WORKER_RESERVED_CONCURRENCY = 10


def _bundling(runtime):
    return cdk.BundlingOptions(
        image=runtime.bundling_image,
        platform="linux/arm64",
        command=[
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
        ],
    )


class NeptuneAgentStack(cdk.Stack):
    """
    Bedrock Strands AI agent: the job backend for the unified API's /ask routes (app-*-api).

    Flow:
      POST /ask (app-*-api)  →  SQS  →  worker Lambda (agent)  →  DynamoDB
      GET /ask/{job_id} (app-*-api)  →  DynamoDB

    This decouples HTTP from execution, absorbs traffic spikes, and eliminates
    the API Gateway 29s hard limit that blocked synchronous agent responses.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_read_endpoint: str,
        neptune_cluster_resource_id: str,
        neptune_security_group: ec2.SecurityGroup,
        bedrock_model_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # DynamoDB — job store
        # -------------------
        self.job_table = dynamodb.Table(
            self,
            "AgentJobTable",
            table_name=f"{construct_id}-jobs",
            partition_key=dynamodb.Attribute(
                name="job_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # -------------------
        # SQS — job queue + DLQ
        # -------------------
        self.dlq = sqs.Queue(
            self,
            "AgentJobDLQ",
            retention_period=cdk.Duration.days(14),
        )
        self.job_queue = sqs.Queue(
            self,
            "AgentJobQueue",
            visibility_timeout=cdk.Duration.seconds(360),  # > worker Lambda timeout
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=2, queue=self.dlq),
        )

        # -------------------
        # Security group (the worker needs VPC egress to Neptune and Bedrock)
        # -------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "NeptuneAgentFunctionSG",
            vpc=vpc,
            description="Security group for Neptune agent Lambdas",
            allow_all_outbound=True,
        )

        # The worker calls Neptune in-process via sagebrain_core.run_query (spec 001 Phase 1b, D-9)
        ec2.CfnSecurityGroupIngress(
            self,
            "AgentToNeptuneIngress",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.agent_sg.security_group_id,
        )

        vpc_kwargs = dict(
            vpc=vpc,
            security_groups=[self.agent_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )

        # -------------------
        # Worker Lambda — SQS-triggered agent
        # -------------------
        self.core_layer = sagebrain_core_layer(self)
        self.agent_fn = lambda_.Function(
            self,
            "NeptuneAgentWorkerFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="agent.handler",
            code=lambda_.Code.from_asset(
                "src/lambda_agent",
                bundling=_bundling(lambda_.Runtime.PYTHON_3_11),
            ),
            architecture=lambda_.Architecture.ARM_64,
            layers=[self.core_layer],
            **vpc_kwargs,
            environment={
                # AWS_REGION (also read by NeptuneConfig.from_env) is set by the Lambda runtime
                "NEPTUNE_ENDPOINT": neptune_read_endpoint,
                "BEDROCK_MODEL_ID": bedrock_model_id,
                "JOB_TABLE_NAME": self.job_table.table_name,
            },
            timeout=cdk.Duration.seconds(
                300
            ),  # several 60s Neptune queries + Bedrock overhead
            memory_size=512,
            # Cap concurrency to avoid overwhelming Bedrock/Neptune under burst traffic
            reserved_concurrent_executions=WORKER_RESERVED_CONCURRENCY,
        )
        self.job_table.grant_read_write_data(self.agent_fn)
        self.agent_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.job_queue,
                batch_size=1,  # one job per invocation
            )
        )

        # IAM: read-only Neptune access scoped to this cluster (same set as the query worker)
        self.agent_fn.add_to_role_policy(
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

        # IAM: Bedrock model invocation (worker only)
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/us.anthropic.claude-*",
                ],
            )
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "AgentJobTableName",
            value=self.job_table.table_name,
            description="DynamoDB table storing agent job results",
        )
