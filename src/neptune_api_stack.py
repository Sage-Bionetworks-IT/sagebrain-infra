import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct


class NeptuneApiStack(cdk.Stack):
    """
    Public read-only API for Neptune via API Gateway + Lambda
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_read_endpoint: str,
        neptune_cluster_resource_id: str,
        neptune_security_group: ec2.SecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # Lambda Function
        # -------------------
        self.query_fn = lambda_.Function(
            self,
            "NeptuneQueryFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="query.handler",
            code=lambda_.Code.from_asset(
                "src/lambda",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                ),
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            environment={
                "NEPTUNE_ENDPOINT": neptune_read_endpoint,
            },
            timeout=cdk.Duration.seconds(30),
        )

        # -------------------
        # IAM: read-only Neptune access scoped to this cluster
        # -------------------
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

        # Allow Lambda to reach Neptune on port 8182.
        # Use CfnSecurityGroupIngress (owned by this stack) to avoid a cross-stack cyclic reference.
        ec2.CfnSecurityGroupIngress(
            self,
            "LambdaToNeptuneIngress",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.query_fn.connections.security_groups[
                0
            ].security_group_id,
        )

        # -------------------
        # API Gateway
        # -------------------
        self.api = apigw.RestApi(
            self,
            "NeptuneApi",
            rest_api_name="neptune-query-api",
            description="Read-only public API for Neptune graph queries",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "OPTIONS"],
            ),
        )

        query_resource = self.api.root.add_resource("query")
        query_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.query_fn),
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "ApiUrl",
            value=f"{self.api.url}query",
            description="Neptune read-only query API endpoint",
        )
