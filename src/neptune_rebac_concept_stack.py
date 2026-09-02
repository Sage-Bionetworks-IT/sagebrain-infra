import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

_BUNDLING = cdk.BundlingOptions(
    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
    command=[
        "bash",
        "-c",
        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
    ],
)


class NeptuneRebacConceptStack(cdk.Stack):
    """Concept API: governance-graph-aware ReBAC authorization via Verified Permissions."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_read_endpoint: str,
        neptune_cluster_resource_id: str,
        neptune_security_group: ec2.SecurityGroup,
        synapse_team_id: str,
        rebac_config: dict,
        machine_api_key: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        policy_store_id = (rebac_config.get("policy_store_id") or "").strip()
        if not policy_store_id:
            raise ValueError(
                "NEPTUNE_REBAC_CONCEPT.policy_store_id is required when ReBAC concept is enabled"
            )

        self.lambda_sg = ec2.SecurityGroup(
            self,
            "RebacAuthorizerFunctionSG",
            vpc=vpc,
            description="Security group for governance ReBAC authorizer Lambda",
            allow_all_outbound=True,
        )

        ec2.CfnSecurityGroupIngress(
            self,
            "RebacLambdaToNeptuneIngress",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.lambda_sg.security_group_id,
        )

        self.authorize_fn = lambda_.Function(
            self,
            "GovernanceRebacAuthorizerFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="authorize.handler",
            code=lambda_.Code.from_asset("src/lambda_rebac", bundling=_BUNDLING),
            vpc=vpc,
            security_groups=[self.lambda_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                "NEPTUNE_ENDPOINT": neptune_read_endpoint,
                "AVP_POLICY_STORE_ID": policy_store_id,
                "AVP_NAMESPACE": rebac_config.get("namespace", "SageBrain"),
                "INFERRED_EDGE_MODE": rebac_config.get(
                    "inferred_edge_mode", "intersection"
                ),
            },
            timeout=cdk.Duration.seconds(30),
            memory_size=512,
        )

        self.authorize_fn.add_to_role_policy(
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
        self.authorize_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["verifiedpermissions:IsAuthorized"],
                resources=[
                    (
                        f"arn:{self.partition}:verifiedpermissions:{self.region}:"
                        f"{self.account}:policy-store/{policy_store_id}"
                    )
                ],
            )
        )

        authorizer_env = {"SYNAPSE_TEAM_ID": synapse_team_id}
        if machine_api_key:
            authorizer_env["MACHINE_API_KEY"] = machine_api_key

        auth_fn = lambda_.Function(
            self,
            "SynapseAuthorizerFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="authorizer.handler",
            code=lambda_.Code.from_asset("src/lambda_authorizer"),
            environment=authorizer_env,
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
        )

        request_authorizer = apigw.RequestAuthorizer(
            self,
            "SynapseRequestAuthorizer",
            handler=auth_fn,
            identity_sources=[apigw.IdentitySource.header("Authorization")],
            results_cache_ttl=cdk.Duration.seconds(0),
        )

        access_log_group = logs.LogGroup(
            self,
            "GovernanceRebacApiAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        self.api = apigw.RestApi(
            self,
            "GovernanceRebacApi",
            rest_api_name="governance-rebac-concept-api",
            description="Concept governance-graph authorization endpoint backed by Verified Permissions",
            cloud_watch_role=True,
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization", "x-api-key"],
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

        self.api.add_gateway_response(
            "AccessDeniedAs401",
            type=apigw.ResponseType.ACCESS_DENIED,
            status_code="401",
            response_headers={"Access-Control-Allow-Origin": "'*'"},
        )
        self.api.add_gateway_response(
            "UnauthorizedWithCors",
            type=apigw.ResponseType.UNAUTHORIZED,
            response_headers={"Access-Control-Allow-Origin": "'*'"},
        )

        authorize_resource = self.api.root.add_resource("authorize")
        authorize_resource.add_method(
            "POST",
            apigw.LambdaIntegration(
                self.authorize_fn, timeout=cdk.Duration.seconds(29)
            ),
            authorizer=request_authorizer,
        )

        cdk.CfnOutput(
            self,
            "GovernanceRebacAuthorizeUrl",
            value=f"{self.api.url}authorize",
            description="Concept ReBAC authorize endpoint — POST principal/action/resource",
        )
