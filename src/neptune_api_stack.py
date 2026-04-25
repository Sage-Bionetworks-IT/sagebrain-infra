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
    Public read-only API for Neptune via async job pattern.

    Flow:
      POST /query  →  submit Lambda  →  SQS  →  worker Lambda (query.py)  →  DynamoDB
      GET /query/{job_id}  →  status Lambda  →  DynamoDB

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
        synapse_team_id: str,
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
        # Submit Lambda — POST /query (no VPC needed)
        # -------------------
        self.submit_fn = lambda_.Function(
            self,
            "NeptuneQuerySubmitFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="submit.handler",
            code=lambda_.Code.from_asset("src/lambda", bundling=_BUNDLING),
            environment={
                "JOB_TABLE_NAME": self.job_table.table_name,
                "JOB_QUEUE_URL": self.job_queue.queue_url,
            },
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
        )
        self.job_table.grant_write_data(self.submit_fn)
        self.job_queue.grant_send_messages(self.submit_fn)

        # -------------------
        # Status Lambda — GET /query/{job_id} (no VPC needed)
        # -------------------
        self.status_fn = lambda_.Function(
            self,
            "NeptuneQueryStatusFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="status.handler",
            code=lambda_.Code.from_asset("src/lambda", bundling=_BUNDLING),
            environment={
                "JOB_TABLE_NAME": self.job_table.table_name,
            },
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
        )
        self.job_table.grant_read_data(self.status_fn)

        # -------------------
        # Worker Lambda — SQS-triggered SPARQL executor (needs VPC)
        # -------------------
        self.query_fn = lambda_.Function(
            self,
            "NeptuneQueryFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="query.handler",
            code=lambda_.Code.from_asset("src/lambda", bundling=_BUNDLING),
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
        # Synapse token authorizer Lambda (no VPC — calls Synapse public API)
        # -------------------
        authorizer_fn = lambda_.Function(
            self,
            "SynapseAuthorizerFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="authorizer.handler",
            code=lambda_.Code.from_asset("src/lambda_authorizer"),
            environment={"SYNAPSE_TEAM_ID": synapse_team_id},
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
        )

        token_authorizer = apigw.TokenAuthorizer(
            self,
            "SynapseTokenAuthorizer",
            handler=authorizer_fn,
            results_cache_ttl=cdk.Duration.minutes(5),
        )

        # -------------------
        # API Gateway
        # -------------------
        access_log_group = logs.LogGroup(
            self,
            "NeptuneApiAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        self.api = apigw.RestApi(
            self,
            "NeptuneApi",
            rest_api_name="neptune-query-api",
            description="Async read-only API for Neptune graph queries",
            cloud_watch_role=True,
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "GET", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization", "X-Source"],
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

        # Remap 403 (Deny policy from token authorizer) → 401 so clients get a
        # consistent Unauthorized response. CORS header included so browser
        # clients can read the status code.
        self.api.add_gateway_response(
            "AccessDeniedAs401",
            type=apigw.ResponseType.ACCESS_DENIED,
            status_code="401",
            response_headers={"Access-Control-Allow-Origin": "'*'"},
        )
        # Missing/empty Authorization header → API GW returns UNAUTHORIZED before
        # running the authorizer, which by default omits CORS headers. Browser
        # clients need the header to read the 401 status.
        self.api.add_gateway_response(
            "UnauthorizedWithCors",
            type=apigw.ResponseType.UNAUTHORIZED,
            response_headers={"Access-Control-Allow-Origin": "'*'"},
        )

        query_resource = self.api.root.add_resource("query")
        query_resource.add_method(
            "POST",
            apigw.LambdaIntegration(self.submit_fn, timeout=cdk.Duration.seconds(10)),
            authorizer=token_authorizer,
        )

        query_job_resource = query_resource.add_resource("{job_id}")
        query_job_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.status_fn, timeout=cdk.Duration.seconds(10)),
            authorizer=token_authorizer,
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "ApiUrl",
            value=f"{self.api.url}query",
            description='Submit endpoint — POST {"query": "..."}  →  {"job_id": "...", "status": "pending"}',
        )
        cdk.CfnOutput(
            self,
            "QueryJobStatusUrl",
            value=f"{self.api.url}query/{{job_id}}",
            description="Poll endpoint — GET /query/{job_id}",
        )
        cdk.CfnOutput(
            self,
            "QueryJobTableName",
            value=self.job_table.table_name,
            description="DynamoDB table storing query job results",
        )
