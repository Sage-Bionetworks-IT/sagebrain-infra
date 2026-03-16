import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Template

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
        "parameter_group_parameters": {}
    }


def test_neptune_stack_creation(app_and_vpc, neptune_config):
    """Test that Neptune stack creates required resources"""
    app, vpc = app_and_vpc
    stack = NeptuneStack(
        app, 
        "TestNeptuneStack", 
        vpc=vpc, 
        neptune_config=neptune_config
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
        }
    )

    # Check that Neptune instance is created
    template.has_resource_properties(
        "AWS::Neptune::DBInstance",
        {
            "DBInstanceClass": "db.t3.medium",
        }
    )

    # Check that security group is created
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": "Security group for Neptune cluster",
        }
    )

    # Check that subnet group is created
    template.has_resource_properties(
        "AWS::Neptune::DBSubnetGroup",
        {
            "DBSubnetGroupDescription": "Subnet group for Neptune cluster",
        }
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
        "parameter_group_parameters": {
            "neptune_enable_audit_log": "1"
        }
    }

    stack = NeptuneStack(
        app, 
        "TestNeptuneStackWithParams", 
        vpc=vpc, 
        neptune_config=neptune_config
    )
    template = Template.from_stack(stack)

    # Check that parameter group is created
    template.has_resource_properties(
        "AWS::Neptune::DBParameterGroup",
        {
            "Family": "neptune1.3",
            "Parameters": {
                "neptune_enable_audit_log": "1"
            }
        }
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
        "parameter_group_parameters": {}
    }

    stack = NeptuneStack(
        app, 
        "TestNeptuneStackMultiple", 
        vpc=vpc, 
        neptune_config=neptune_config
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
        }
    )