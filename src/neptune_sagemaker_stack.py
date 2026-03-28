import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sagemaker as sagemaker
from constructs import Construct


class NeptuneSageMakerStack(cdk.Stack):
    """
    SageMaker Studio domain for team access to the Neptune knowledge graph.

    Replaces the EC2 bastion host with a managed Jupyter environment accessible
    via the AWS Console. Team members can load data and run SPARQL queries
    without needing to SSH into an EC2 instance.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_security_group: ec2.SecurityGroup,
        neptune_cluster_resource_id: str,
        sagemaker_config: dict,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # Security Group for SageMaker Studio
        # -------------------
        self.studio_security_group = ec2.SecurityGroup(
            self,
            "StudioSecurityGroup",
            vpc=vpc,
            description="Security group for SageMaker Studio (Neptune access)",
            allow_all_outbound=True,
        )

        # SageMaker Studio requires self-referencing rules for intra-domain traffic:
        # EFS mounts (NFS on port 2049) and inter-instance communication (8192–65535).
        self.studio_security_group.add_ingress_rule(
            peer=self.studio_security_group,
            connection=ec2.Port.tcp(2049),
            description="NFS for SageMaker Studio EFS",
        )
        self.studio_security_group.add_ingress_rule(
            peer=self.studio_security_group,
            connection=ec2.Port.tcp_range(8192, 65535),
            description="SageMaker Studio inter-instance communication",
        )

        # -------------------
        # Neptune ingress from SageMaker Studio
        # -------------------
        # Rule lives in this stack (not the Neptune stack) to avoid cross-stack cyclic refs.
        ec2.CfnSecurityGroupIngress(
            self,
            "NeptuneIngressFromStudio",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.studio_security_group.security_group_id,
            description="Neptune access from SageMaker Studio",
        )

        # -------------------
        # IAM Execution Role
        # -------------------
        self.execution_role = iam.Role(
            self,
            "StudioExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSageMakerFullAccess"
                ),
            ],
        )

        # Full Neptune access (read + write + delete) scoped to this cluster
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "neptune-db:ReadDataViaQuery",
                    "neptune-db:WriteDataViaQuery",
                    "neptune-db:DeleteDataViaQuery",
                    "neptune-db:GetEngineStatus",
                    "neptune-db:GetQueryStatus",
                    "neptune-db:CancelQuery",
                    "neptune-db:ResetDatabase",
                ],
                resources=[
                    f"arn:{self.partition}:neptune-db:{self.region}:{self.account}:{neptune_cluster_resource_id}/*"
                ],
            )
        )

        # -------------------
        # SageMaker Studio Domain
        # -------------------
        private_subnets = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        )

        self.domain = sagemaker.CfnDomain(
            self,
            "StudioDomain",
            auth_mode="IAM",
            default_user_settings=sagemaker.CfnDomain.UserSettingsProperty(
                execution_role=self.execution_role.role_arn,
                security_groups=[self.studio_security_group.security_group_id],
            ),
            domain_name=sagemaker_config.get("domain_name", "sage-brain-studio"),
            subnet_ids=private_subnets.subnet_ids,
            vpc_id=vpc.vpc_id,
            # PublicInternetOnly: Studio instances are in private subnets and reach
            # Neptune via VPC. AWS API calls (S3, CloudWatch, etc.) use the NAT gateway.
            # Switch to VpcOnly + VPC interface endpoints for full network isolation.
            app_network_access_type="PublicInternetOnly",
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "StudioDomainId",
            value=self.domain.attr_domain_id,
            description="SageMaker Studio domain ID",
        )

        cdk.CfnOutput(
            self,
            "StudioUrl",
            value=self.domain.attr_url,
            description="SageMaker Studio URL (open via AWS Console → SageMaker → Studio)",
        )

        cdk.CfnOutput(
            self,
            "StudioExecutionRoleArn",
            value=self.execution_role.role_arn,
            description="ARN of the SageMaker Studio execution role",
        )
