"""
Temporary Neptune admin box: an SSM-managed EC2 in Neptune's VPC that can run SPARQL
reads and deletes against the writer endpoint. Deploy it, use it, destroy it — never
leave it running. Drive it with ``bastion.sh`` (next to this file), not directly.

Why a CDK stack and not plain CLI calls: the SSO ``Developer`` role cannot create IAM
roles or instance profiles, but CloudFormation can (it runs as the CDK bootstrap exec role).

Everything environment-specific is looked up from the account's single Neptune cluster at
synth time, so the same app works in dev and prod — pick the account with AWS_PROFILE.
"""

import aws_cdk as cdk
import boto3
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam

STACK_NAME = "sagebrain-neptune-bastion"

session = boto3.Session()
region = session.region_name or "us-east-1"
account = session.client("sts").get_caller_identity()["Account"]
neptune = session.client("neptune", region_name=region)

clusters = [
    c for c in neptune.describe_db_clusters()["DBClusters"] if c["Engine"] == "neptune"
]
if len(clusters) != 1:
    raise SystemExit(f"Expected exactly one Neptune cluster, found {len(clusters)}")
cluster = clusters[0]
# The subnet group is the VPC's private subnets, which have NAT egress (SSM needs it).
subnet_group = neptune.describe_db_subnet_groups(
    DBSubnetGroupName=cluster["DBSubnetGroup"]
)["DBSubnetGroups"][0]

app = cdk.App()
stack = cdk.Stack(app, STACK_NAME, env=cdk.Environment(account=account, region=region))

sg = ec2.CfnSecurityGroup(
    stack,
    "SG",
    vpc_id=subnet_group["VpcId"],
    group_description="Temporary Neptune admin box (sagebrain-neptune-bastion)",
)
ec2.CfnSecurityGroupIngress(
    stack,
    "ToNeptune",
    group_id=cluster["VpcSecurityGroups"][0]["VpcSecurityGroupId"],
    ip_protocol="tcp",
    from_port=8182,
    to_port=8182,
    source_security_group_id=sg.attr_group_id,
    description="temp: sagebrain-neptune-bastion",
)

role = iam.Role(
    stack,
    "Role",
    assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
    managed_policies=[
        iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
    ],
)
# Read + delete only — no bulk-loader or write-via-INSERT actions.
role.add_to_policy(
    iam.PolicyStatement(
        actions=[
            "neptune-db:ReadDataViaQuery",
            "neptune-db:DeleteDataViaQuery",
            "neptune-db:GetQueryStatus",
            "neptune-db:CancelQuery",
        ],
        resources=[
            f"arn:aws:neptune-db:{region}:{account}:{cluster['DbClusterResourceId']}/*"
        ],
    )
)
profile = iam.CfnInstanceProfile(stack, "Profile", roles=[role.role_name])

instance = ec2.CfnInstance(
    stack,
    "Instance",
    image_id=ec2.MachineImage.latest_amazon_linux2023().get_image(stack).image_id,
    instance_type="t3.micro",
    subnet_id=subnet_group["Subnets"][0]["SubnetIdentifier"],
    security_group_ids=[sg.attr_group_id],
    iam_instance_profile=profile.ref,
    metadata_options=ec2.CfnInstance.MetadataOptionsProperty(http_tokens="required"),
    tags=[cdk.CfnTag(key="Name", value=STACK_NAME)],
)

cdk.CfnOutput(stack, "InstanceId", value=instance.ref)
cdk.CfnOutput(stack, "NeptuneEndpoint", value=cluster["Endpoint"])
app.synth()
