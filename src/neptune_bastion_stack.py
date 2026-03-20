import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from constructs import Construct


class NeptuneBastionStack(cdk.Stack):
    """
    Bastion host for secure Neptune access from local development
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        neptune_security_group: ec2.SecurityGroup,
        bastion_config: dict,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # Security Group for Bastion Host
        # -------------------
        self.bastion_security_group = ec2.SecurityGroup(
            self,
            "BastionSecurityGroup",
            vpc=vpc,
            description="Security group for Neptune bastion host",
            allow_all_outbound=True,
        )

        # No inbound rules — access is via SSM only, no SSH needed

        # -------------------
        # Update Neptune Security Group
        # -------------------
        # Explicitly place the ingress rule in this (bastion) stack so that the
        # Neptune stack has no CloudFormation dependency on the bastion stack.
        ec2.CfnSecurityGroupIngress(
            self,
            "NeptuneIngressFromBastion",
            group_id=neptune_security_group.security_group_id,
            ip_protocol="tcp",
            from_port=8182,
            to_port=8182,
            source_security_group_id=self.bastion_security_group.security_group_id,
            description="Neptune access from bastion host",
        )

        # -------------------
        # IAM Role for Bastion Host
        # -------------------
        self.bastion_role = iam.Role(
            self,
            "BastionRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
            ],
        )

        # Add Neptune permissions
        self.bastion_role.add_to_policy(
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
                resources=["*"],
            )
        )

        # -------------------
        # Bastion EC2 Instance
        # -------------------
        self.bastion_instance = ec2.Instance(
            self,
            "BastionInstance",
            instance_type=ec2.InstanceType(
                bastion_config.get("instance_type", "t3.medium")
            ),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=self.bastion_security_group,
            role=self.bastion_role,
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        bastion_config.get("root_volume_size_gb", 500),
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                    ),
                )
            ],
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "BastionInstanceId",
            value=self.bastion_instance.instance_id,
            description="Instance ID of the bastion host",
        )

        cdk.CfnOutput(
            self,
            "SSMCommand",
            value=f"aws ssm start-session --target {self.bastion_instance.instance_id}",
            description="SSM command to connect to bastion host",
        )
