import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct


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
    Bedrock Strands AI agent: async POST /ask + GET /ask/{job_id} poll pattern.

    Flow:
      POST /ask  →  submit Lambda  →  SQS  →  worker Lambda (agent)  →  DynamoDB
      GET /ask/{job_id}  →  status Lambda  →  DynamoDB

    This decouples HTTP from execution, absorbs traffic spikes, and eliminates
    the API Gateway 29s hard limit that blocked synchronous agent responses.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_query_url: str,
        neptune_query_status_url: str,
        bedrock_model_id: str = "us.anthropic.claude-sonnet-4-6",
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
        dlq = sqs.Queue(
            self,
            "AgentJobDLQ",
            retention_period=cdk.Duration.days(14),
        )
        self.job_queue = sqs.Queue(
            self,
            "AgentJobQueue",
            visibility_timeout=cdk.Duration.seconds(360),  # > worker Lambda timeout
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=2, queue=dlq),
        )

        # -------------------
        # Security group (shared by all agent Lambdas needing VPC egress)
        # -------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "NeptuneAgentFunctionSG",
            vpc=vpc,
            description="Security group for Neptune agent Lambdas",
            allow_all_outbound=True,
        )

        vpc_kwargs = dict(
            vpc=vpc,
            security_groups=[self.agent_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )

        # -------------------
        # Submit Lambda — POST /ask
        # -------------------
        self.submit_fn = lambda_.Function(
            self,
            "NeptuneAgentSubmitFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="submit.handler",
            code=lambda_.Code.from_asset(
                "src/lambda_agent",
                bundling=_bundling(lambda_.Runtime.PYTHON_3_11),
            ),
            architecture=lambda_.Architecture.ARM_64,
            environment={
                "JOB_TABLE_NAME": self.job_table.table_name,
                "JOB_QUEUE_URL": self.job_queue.queue_url,
            },
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
            # submit Lambda doesn't need VPC (no Neptune/Bedrock calls)
        )
        self.job_table.grant_write_data(self.submit_fn)
        self.job_queue.grant_send_messages(self.submit_fn)

        # -------------------
        # Status Lambda — GET /ask/{job_id}
        # -------------------
        self.status_fn = lambda_.Function(
            self,
            "NeptuneAgentStatusFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="status.handler",
            code=lambda_.Code.from_asset(
                "src/lambda_agent",
                bundling=_bundling(lambda_.Runtime.PYTHON_3_11),
            ),
            architecture=lambda_.Architecture.ARM_64,
            environment={
                "JOB_TABLE_NAME": self.job_table.table_name,
            },
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
            # status Lambda doesn't need VPC either
        )
        self.job_table.grant_read_data(self.status_fn)

        # -------------------
        # Worker Lambda — SQS-triggered agent
        # -------------------
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
            **vpc_kwargs,
            environment={
                "NEPTUNE_QUERY_URL": neptune_query_url,
                "NEPTUNE_QUERY_STATUS_URL": neptune_query_status_url,
                "BEDROCK_MODEL_ID": bedrock_model_id,
                "JOB_TABLE_NAME": self.job_table.table_name,
            },
            timeout=cdk.Duration.seconds(
                300
            ),  # up to 3 Neptune queries × 80s poll each + Bedrock overhead
            memory_size=512,
            # Cap concurrency to avoid overwhelming Bedrock/Neptune under burst traffic
            reserved_concurrent_executions=10,
        )
        self.job_table.grant_read_write_data(self.agent_fn)
        self.agent_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.job_queue,
                batch_size=1,  # one job per invocation
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
        # API Gateway
        # -------------------
        access_log_group = logs.LogGroup(
            self,
            "NeptuneAgentAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        self.api = apigw.RestApi(
            self,
            "NeptuneAgentApi",
            rest_api_name="neptune-agent-api",
            description="Async NL-to-SPARQL agent API for Neptune knowledge graph",
            cloud_watch_role=True,
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "GET", "OPTIONS"],
            ),
            deploy_options=apigw.StageOptions(
                access_log_destination=apigw.LogGroupLogDestination(access_log_group),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                logging_level=apigw.MethodLoggingLevel.ERROR,
                metrics_enabled=True,
                throttling_rate_limit=50,
                throttling_burst_limit=100,
            ),
        )

        ask_resource = self.api.root.add_resource("ask")
        ask_resource.add_method(
            "POST",
            apigw.LambdaIntegration(self.submit_fn, timeout=cdk.Duration.seconds(10)),
        )

        ask_job_resource = ask_resource.add_resource("{job_id}")
        ask_job_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.status_fn, timeout=cdk.Duration.seconds(10)),
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "AgentApiUrl",
            value=f"{self.api.url}ask",
            description='Submit endpoint — POST {"question": "..."}  →  {"job_id": "...", "status": "pending"}',
        )
        cdk.CfnOutput(
            self,
            "AgentJobStatusUrl",
            value=f"{self.api.url}ask/{{job_id}}",
            description="Poll endpoint — GET /ask/{job_id}",
        )
        cdk.CfnOutput(
            self,
            "AgentJobTableName",
            value=self.job_table.table_name,
            description="DynamoDB table storing agent job results",
        )
