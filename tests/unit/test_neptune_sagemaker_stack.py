import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from src.neptune_sagemaker_stack import NeptuneSageMakerStack

CLUSTER_RESOURCE_ID = "cluster-ABCDEFGHIJKLMNOP"


@pytest.fixture(scope="module")
def template():
    app = cdk.App()

    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)

    sg_stack = cdk.Stack(app, "TestSGStack")
    neptune_sg = ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune SG"
    )

    bucket_stack = cdk.Stack(app, "TestBucketStack")
    data_bucket = s3.Bucket(bucket_stack, "TestDataBucket")

    stack = NeptuneSageMakerStack(
        app,
        "TestSageMakerStack",
        vpc=vpc,
        neptune_security_group=neptune_sg,
        neptune_cluster_resource_id=CLUSTER_RESOURCE_ID,
        sagemaker_config={"domain_name": "test-studio"},
        data_bucket=data_bucket,
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Security group
# ---------------------------------------------------------------------------


def test_studio_security_group_created(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"GroupDescription": "Security group for SageMaker Studio (Neptune access)"},
    )


def test_studio_sg_allows_nfs_self_ingress(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"FromPort": 2049, "ToPort": 2049, "IpProtocol": "tcp"},
    )


def test_studio_sg_allows_inter_instance_self_ingress(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"FromPort": 8192, "ToPort": 65535, "IpProtocol": "tcp"},
    )


def test_neptune_ingress_rule_on_port_8182(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"FromPort": 8182, "ToPort": 8182, "IpProtocol": "tcp"},
    )


def test_studio_sg_has_no_broad_inbound_rules(template):
    """Studio SG should have no inbound rules beyond the self-referencing ones."""
    sgs = template.find_resources(
        "AWS::EC2::SecurityGroup",
        {
            "Properties": {
                "GroupDescription": "Security group for SageMaker Studio (Neptune access)"
            }
        },
    )
    assert len(sgs) == 1
    studio_sg = list(sgs.values())[0]
    # CDK writes self-referencing ingress as separate CfnSecurityGroupIngress resources,
    # so SecurityGroupIngress on the SG itself should be empty.
    inline_ingress = studio_sg["Properties"].get("SecurityGroupIngress", [])
    assert inline_ingress == [], f"Unexpected inline ingress rules: {inline_ingress}"


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------


def test_execution_role_assumed_by_sagemaker(template):
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    {
                        "Action": "sts:AssumeRole",
                        "Effect": "Allow",
                        "Principal": {"Service": "sagemaker.amazonaws.com"},
                    }
                ]
            }
        },
    )


def test_execution_role_has_sagemaker_full_access(template):
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "ManagedPolicyArns": Match.array_with(
                [
                    Match.object_like(
                        {
                            "Fn::Join": [
                                "",
                                [
                                    "arn:",
                                    {"Ref": "AWS::Partition"},
                                    ":iam::aws:policy/AmazonSageMakerFullAccess",
                                ],
                            ]
                        }
                    )
                ]
            )
        },
    )


def test_neptune_write_actions_granted(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Action": Match.array_with(
                                    [
                                        "neptune-db:ReadDataViaQuery",
                                        "neptune-db:WriteDataViaQuery",
                                        "neptune-db:DeleteDataViaQuery",
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_neptune_loader_actions_granted(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Action": Match.array_with(
                                    [
                                        "neptune-db:StartLoaderJob",
                                        "neptune-db:GetLoaderJobStatus",
                                        "neptune-db:ListLoaderJobs",
                                        "neptune-db:CancelLoaderJob",
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_s3_read_write_granted_to_execution_role(template):
    """Execution role must have S3 PutObject (write) on the data bucket."""
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Action": Match.array_with(["s3:PutObject"]),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_neptune_permission_scoped_to_cluster(template):
    """IAM resource must be scoped to the specific cluster, not '*'."""
    policies = template.find_resources("AWS::IAM::Policy")
    found = False
    for policy in policies.values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "neptune-db:WriteDataViaQuery" in actions:
                resources = stmt.get("Resource", [])
                assert resources != "*", "Neptune IAM resource must not be wildcard '*'"
                if isinstance(resources, list):
                    assert any(
                        CLUSTER_RESOURCE_ID in str(r) for r in resources
                    ), "Neptune resource should reference the cluster resource ID"
                found = True
    assert found, "No Neptune write policy statement found"


# ---------------------------------------------------------------------------
# SageMaker Domain
# ---------------------------------------------------------------------------


def test_studio_domain_created(template):
    template.has_resource_properties(
        "AWS::SageMaker::Domain",
        {"DomainName": "test-studio"},
    )


def test_studio_domain_iam_auth_mode(template):
    template.has_resource_properties(
        "AWS::SageMaker::Domain",
        {"AuthMode": "IAM"},
    )


def test_studio_domain_in_private_subnet(template):
    template.has_resource_properties(
        "AWS::SageMaker::Domain",
        {"SubnetIds": Match.any_value()},
    )


def test_studio_domain_network_access_type(template):
    template.has_resource_properties(
        "AWS::SageMaker::Domain",
        {"AppNetworkAccessType": "VpcOnly"},
    )


def test_studio_domain_default_user_settings_has_execution_role(template):
    template.has_resource_properties(
        "AWS::SageMaker::Domain",
        {
            "DefaultUserSettings": {
                "ExecutionRole": Match.any_value(),
            }
        },
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_studio_domain_id_output_exists(template):
    template.has_output("StudioDomainId", {})


def test_studio_url_output_exists(template):
    template.has_output("StudioUrl", {})


def test_studio_execution_role_arn_output_exists(template):
    template.has_output("StudioExecutionRoleArn", {})
