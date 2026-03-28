import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_stack import NeptuneStack


@pytest.fixture
def app_and_vpc():
    """Create a test app with VPC"""
    app = cdk.App()
    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)
    return app, vpc


@pytest.fixture
def neptune_config():
    """Default Neptune configuration for testing"""
    return {
        "engine_version": "1.3.2.1",
        "instance_count": 1,
        "instance_class": "db.t3.medium",
        "backup_retention_days": 7,
        "preferred_backup_window": "03:00-04:00",
        "preferred_maintenance_window": "sun:04:00-sun:05:00",
        "deletion_protection": False,
        "cloudwatch_logs_exports": ["audit", "slowquery"],
        "iam_auth_enabled": True,
        "storage_encrypted": True,
        "auto_minor_version_upgrade": True,
        "create_parameter_group": False,
        "parameter_group_parameters": {},
    }


def test_neptune_stack_creation(app_and_vpc, neptune_config):
    """Test that Neptune stack creates required resources"""
    app, vpc = app_and_vpc
    stack = NeptuneStack(
        app, "TestNeptuneStack", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    # Check that Neptune cluster is created
    template.has_resource_properties(
        "AWS::Neptune::DBCluster",
        {
            "EngineVersion": "1.3.2.1",
            "BackupRetentionPeriod": 7,
            "DeletionProtection": False,
            "StorageEncrypted": True,
            "IamAuthEnabled": True,
        },
    )

    # Check that Neptune instance is created
    template.has_resource_properties(
        "AWS::Neptune::DBInstance",
        {
            "DBInstanceClass": "db.t3.medium",
        },
    )

    # Check that security group is created
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "Security group for Neptune cluster",
        },
    )

    # Check that subnet group is created
    template.has_resource_properties(
        "AWS::Neptune::DBSubnetGroup",
        {
            "DBSubnetGroupDescription": "Subnet group for Neptune cluster",
        },
    )


def test_neptune_stack_with_parameter_group(app_and_vpc):
    """Test Neptune stack with custom parameter group"""
    app, vpc = app_and_vpc
    neptune_config = {
        "engine_version": "1.3.2.1",
        "instance_count": 1,
        "instance_class": "db.t3.medium",
        "backup_retention_days": 7,
        "preferred_backup_window": "03:00-04:00",
        "preferred_maintenance_window": "sun:04:00-sun:05:00",
        "deletion_protection": False,
        "cloudwatch_logs_exports": ["audit"],
        "iam_auth_enabled": True,
        "storage_encrypted": True,
        "auto_minor_version_upgrade": True,
        "create_parameter_group": True,
        "parameter_group_parameters": {"neptune_enable_audit_log": "1"},
    }

    stack = NeptuneStack(
        app, "TestNeptuneStackWithParams", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    # Check that parameter group is created
    template.has_resource_properties(
        "AWS::Neptune::DBParameterGroup",
        {"Family": "neptune1.3", "Parameters": {"neptune_enable_audit_log": "1"}},
    )


def test_data_bucket_created(app_and_vpc, neptune_config):
    """Neptune stack must create an S3 bucket for bulk data loading."""
    app, vpc = app_and_vpc
    stack = NeptuneStack(
        app, "TestNeptuneBucket", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            "BucketEncryption": Match.any_value(),
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )


def test_data_bucket_retained_on_delete(app_and_vpc, neptune_config):
    """Data bucket must have RETAIN removal policy to protect data."""
    app, vpc = app_and_vpc
    stack = NeptuneStack(
        app, "TestNeptuneBucketRetain", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    buckets = template.find_resources(
        "AWS::S3::Bucket",
        {"DeletionPolicy": "Retain"},
    )
    assert len(buckets) == 1, "Data bucket must have DeletionPolicy: Retain"


def test_neptune_load_role_trusted_by_rds(app_and_vpc, neptune_config):
    """Neptune load role must be assumable by rds.amazonaws.com."""
    app, vpc = app_and_vpc
    stack = NeptuneStack(
        app, "TestNeptuneLoadRole", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    {
                        "Action": "sts:AssumeRole",
                        "Effect": "Allow",
                        "Principal": {"Service": "rds.amazonaws.com"},
                    }
                ]
            }
        },
    )


def test_neptune_cluster_has_associated_load_role(app_and_vpc, neptune_config):
    """Neptune cluster must have the load role associated for bulk loading."""
    app, vpc = app_and_vpc
    stack = NeptuneStack(
        app, "TestNeptuneAssocRole", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::Neptune::DBCluster",
        {
            "AssociatedRoles": Match.array_with(
                [Match.object_like({"RoleArn": Match.any_value()})]
            )
        },
    )


def test_neptune_sg_allows_https_egress(app_and_vpc, neptune_config):
    """Neptune SG must allow HTTPS egress for bulk loader to reach S3."""
    app, vpc = app_and_vpc
    stack = NeptuneStack(
        app, "TestNeptuneSGEgress", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "Security group for Neptune cluster",
            "SecurityGroupEgress": Match.array_with(
                [
                    Match.object_like(
                        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443}
                    )
                ]
            ),
        },
    )


def test_neptune_stack_multiple_instances(app_and_vpc):
    """Test Neptune stack with multiple instances"""
    app, vpc = app_and_vpc
    neptune_config = {
        "engine_version": "1.3.2.1",
        "instance_count": 2,
        "instance_class": "db.r5.large",
        "backup_retention_days": 30,
        "preferred_backup_window": "03:00-04:00",
        "preferred_maintenance_window": "sun:04:00-sun:05:00",
        "deletion_protection": True,
        "cloudwatch_logs_exports": ["audit", "slowquery"],
        "iam_auth_enabled": True,
        "storage_encrypted": True,
        "auto_minor_version_upgrade": True,
        "create_parameter_group": False,
        "parameter_group_parameters": {},
    }

    stack = NeptuneStack(
        app, "TestNeptuneStackMultiple", vpc=vpc, neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    # Check that multiple instances are created
    template.resource_count_is("AWS::Neptune::DBInstance", 2)

    # Check cluster configuration for production
    template.has_resource_properties(
        "AWS::Neptune::DBCluster",
        {
            "BackupRetentionPeriod": 30,
            "DeletionProtection": True,
        },
    )
