import aws_cdk as cdk

from aws_cdk import aws_ec2 as ec2

from constructs import Construct


class NetworkStack(cdk.Stack):
    """
    Network for applications
    """

    def __init__(self, scope: Construct, construct_id: str, vpc_cidr, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------
        # create a VPC
        # -------------------
        self.vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, ip_addresses=ec2.IpAddresses.cidr(vpc_cidr)
        )

        # -------------------
        # VPC Endpoints
        # -------------------
        # Shared SG for interface endpoints — allows HTTPS from within the VPC only
        endpoint_sg = ec2.SecurityGroup(
            self,
            "VpcEndpointSG",
            vpc=self.vpc,
            description="Security group for VPC interface endpoints",
            allow_all_outbound=False,
        )
        endpoint_sg.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS from VPC",
        )

        private_subnets = ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        )

        # S3 gateway endpoint — free, route-table based, no SG needed
        ec2.GatewayVpcEndpoint(
            self,
            "S3Endpoint",
            vpc=self.vpc,
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # SageMaker API endpoint — required for Studio VpcOnly mode
        ec2.InterfaceVpcEndpoint(
            self,
            "SageMakerApiEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_API,
            subnets=private_subnets,
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

        # STS endpoint — required for IAM credential refresh in VpcOnly mode
        ec2.InterfaceVpcEndpoint(
            self,
            "StsEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.STS,
            subnets=private_subnets,
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )
