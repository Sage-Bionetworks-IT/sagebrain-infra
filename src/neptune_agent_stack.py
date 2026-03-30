import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct


class NeptuneAgentStack(cdk.Stack):
    """
    Bedrock Strands AI agent: POST /ask takes a natural-language question,
    generates and runs SPARQL against Neptune, returns a plain-language answer
    plus the tool call steps for transparency.

    Phase 1: synchronous API Gateway + Lambda.
    Note: API Gateway has a hard 29s integration timeout. Typical agent loops
    (1-2 tool calls) complete in 10-20s. Migrate to WebSocket API (Phase 2)
    when real-time streaming of thinking loops is needed.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_query_url: str,
        bedrock_model_id: str = "us.anthropic.claude-sonnet-4-6",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # Lambda Security Group
        # -------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "NeptuneAgentFunctionSG",
            vpc=vpc,
            description="Security group for Neptune agent Lambda",
            allow_all_outbound=True,
        )

        # -------------------
        # Lambda Function
        # -------------------
        self.agent_fn = lambda_.Function(
            self,
            "NeptuneAgentFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="agent.handler",
            code=lambda_.Code.from_asset(
                "src/lambda_agent",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                    platform="linux/arm64",
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                ),
            ),
            architecture=lambda_.Architecture.ARM_64,
            vpc=vpc,
            security_groups=[self.agent_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                "NEPTUNE_QUERY_URL": neptune_query_url,
                "BEDROCK_MODEL_ID": bedrock_model_id,
            },
            # 60s to allow headroom beyond the API Gateway 29s limit
            # (useful if this Lambda is later invoked directly or via Function URL)
            timeout=cdk.Duration.seconds(60),
            memory_size=512,
        )

        # -------------------
        # IAM: Bedrock model invocation
        # -------------------
        self.agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    # Foundation models (any region — cross-region profiles route to multiple US regions)
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
                    # Cross-region inference profiles
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
            description="NL-to-SPARQL agent API for Neptune knowledge graph",
            cloud_watch_role=True,
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
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
                throttling_rate_limit=10,
                throttling_burst_limit=20,
            ),
        )

        ask_resource = self.api.root.add_resource("ask")
        ask_resource.add_method(
            "POST",
            apigw.LambdaIntegration(
                self.agent_fn,
                timeout=cdk.Duration.seconds(29),  # API Gateway hard limit
            ),
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "AgentApiUrl",
            value=f"{self.api.url}ask",
            description='Neptune NL agent API endpoint — POST {"question": "..."}',
        )
