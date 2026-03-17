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

        # Allow SSH access from specified CIDR blocks
        allowed_cidrs = bastion_config.get("allowed_ssh_cidrs", ["0.0.0.0/0"])
        for cidr in allowed_cidrs:
            self.bastion_security_group.add_ingress_rule(
                peer=ec2.Peer.ipv4(cidr),
                connection=ec2.Port.tcp(22),
                description=f"SSH access from {cidr}",
            )

        # Allow Neptune proxy port if enabled
        if bastion_config.get("enable_neptune_proxy", True):
            for cidr in allowed_cidrs:
                self.bastion_security_group.add_ingress_rule(
                    peer=ec2.Peer.ipv4(cidr),
                    connection=ec2.Port.tcp(8182),
                    description=f"Neptune proxy port from {cidr}",
                )

        # -------------------
        # Update Neptune Security Group
        # -------------------
        # Allow Neptune access from bastion host
        neptune_security_group.add_ingress_rule(
            peer=ec2.Peer.security_group_id(
                self.bastion_security_group.security_group_id
            ),
            connection=ec2.Port.tcp(8182),
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
                ],
                resources=["*"],
            )
        )

        # -------------------
        # Instance Profile
        # -------------------
        self.instance_profile = iam.CfnInstanceProfile(
            self,
            "BastionInstanceProfile",
            roles=[self.bastion_role.role_name],
        )

        # -------------------
        # User Data Script
        # -------------------
        user_data_script = ec2.UserData.for_linux()

        # Basic system setup
        user_data_script.add_commands(
            "dnf update -y",
            "dnf install -y amazon-cloudwatch-agent",
            "dnf install -y python3 python3-pip git",
        )

        # Install Neptune tools
        user_data_script.add_commands(
            "pip3 install --user gremlinpython boto3 requests",
            "pip3 install --user awscli",
        )

        # Setup Neptune proxy script if enabled
        if bastion_config.get("enable_neptune_proxy", True):
            user_data_script.add_commands(
                "mkdir -p /home/ec2-user/neptune-proxy",
                "chown ec2-user:ec2-user /home/ec2-user/neptune-proxy",
            )

            # Create a simple Neptune WebSocket proxy
            proxy_script = """#!/usr/bin/env python3
import asyncio
import websockets
import ssl
import os
import sys
from urllib.parse import urlparse

async def proxy_handler(websocket, path):
    neptune_endpoint = os.environ.get('NEPTUNE_ENDPOINT')
    if not neptune_endpoint:
        print("NEPTUNE_ENDPOINT environment variable not set")
        return

    neptune_url = f"wss://{neptune_endpoint}:8182{path}"
    print(f"Proxying to: {neptune_url}")

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        async with websockets.connect(neptune_url, ssl=ssl_context) as neptune_ws:
            async def forward_to_neptune():
                async for message in websocket:
                    await neptune_ws.send(message)

            async def forward_to_client():
                async for message in neptune_ws:
                    await websocket.send(message)

            await asyncio.gather(
                forward_to_neptune(),
                forward_to_client()
            )
    except Exception as e:
        print(f"Proxy error: {e}")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8182
    print(f"Starting Neptune proxy on port {port}")
    start_server = websockets.serve(proxy_handler, "0.0.0.0", port)
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()
"""

            user_data_script.add_commands(
                f'cat > /home/ec2-user/neptune-proxy/proxy.py << "EOF"\n{proxy_script}\nEOF',
                "chown ec2-user:ec2-user /home/ec2-user/neptune-proxy/proxy.py",
                "chmod +x /home/ec2-user/neptune-proxy/proxy.py",
                "pip3 install --user websockets",
            )

        # -------------------
        # Key Pair (if specified)
        # -------------------
        key_pair = None
        if bastion_config.get("key_pair_name"):
            key_pair = bastion_config["key_pair_name"]

        # -------------------
        # Bastion EC2 Instance
        # -------------------
        self.bastion_instance = ec2.Instance(
            self,
            "BastionInstance",
            instance_type=ec2.InstanceType(bastion_config.get("instance_type", "t3.medium")),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=self.bastion_security_group,
            user_data=user_data_script,
            key_name=key_pair,
            role=self.bastion_role,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        bastion_config.get("root_volume_size_gb", 500),
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                    ),
                )
            ],
        )

        # -------------------
        # Outputs
        # -------------------
        cdk.CfnOutput(
            self,
            "BastionPublicIP",
            value=self.bastion_instance.instance_public_ip,
            description="Public IP address of the bastion host",
        )

        cdk.CfnOutput(
            self,
            "BastionInstanceId",
            value=self.bastion_instance.instance_id,
            description="Instance ID of the bastion host",
        )

        cdk.CfnOutput(
            self,
            "SSHCommand",
            value=f"ssh -i your-key.pem ec2-user@{self.bastion_instance.instance_public_ip}",
            description="SSH command to connect to bastion host",
        )

        if bastion_config.get("enable_neptune_proxy", True):
            cdk.CfnOutput(
                self,
                "NeptuneProxyEndpoint",
                value=f"{self.bastion_instance.instance_public_ip}:8182",
                description="Neptune proxy endpoint for local connections",
            )
