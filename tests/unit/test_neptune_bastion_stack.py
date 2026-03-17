import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Template

from src.neptune_bastion_stack import NeptuneBastionStack


@pytest.fixture
def vpc():
    """Create a test VPC"""

    class MockApp(cdk.App):
        pass

    app = MockApp()
    vpc_stack = cdk.Stack(app, "TestVpcStack")
    return ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)


@pytest.fixture
def neptune_security_group(vpc):
    """Create a mock Neptune security group"""

    class MockApp(cdk.App):
        pass

    app = MockApp()
    sg_stack = cdk.Stack(app, "TestSGStack")
    return ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune security group"
    )


@pytest.fixture
def bastion_config():
    """Default bastion configuration for testing"""
    return {
        "instance_type": "t3.micro",
        "enable_neptune_proxy": True,
        "allowed_ssh_cidrs": ["10.0.0.0/8"],
        "key_pair_name": "test-keypair",
    }


def test_bastion_stack_creation(vpc, neptune_security_group, bastion_config):
    """Test that bastion stack creates required resources"""
    app = cdk.App()
    stack = NeptuneBastionStack(
        app,
        "TestBastionStack",
        vpc=vpc,
        neptune_security_group=neptune_security_group,
        bastion_config=bastion_config,
    )
    template = Template.from_stack(stack)

    # Check that EC2 instance is created
    template.has_resource_properties(
        "AWS::EC2::Instance", {"InstanceType": "t3.micro", "KeyName": "test-keypair"}
    )

    # Check that bastion security group is created
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"GroupDescription": "Security group for Neptune bastion host"},
    )

    # Check that security group allows SSH access
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "CidrIp": "10.0.0.0/8"},
    )

    # Check that IAM role is created
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    {
                        "Action": "sts:AssumeRole",
                        "Effect": "Allow",
                        "Principal": {"Service": "ec2.amazonaws.com"},
                    }
                ]
            }
        },
    )


def test_bastion_stack_with_proxy_disabled(vpc, neptune_security_group):
    """Test bastion stack with Neptune proxy disabled"""
    bastion_config = {
        "instance_type": "t3.micro",
        "enable_neptune_proxy": False,
        "allowed_ssh_cidrs": ["192.168.1.0/24"],
    }

    app = cdk.App()
    stack = NeptuneBastionStack(
        app,
        "TestBastionStackNoProxy",
        vpc=vpc,
        neptune_security_group=neptune_security_group,
        bastion_config=bastion_config,
    )
    template = Template.from_stack(stack)

    # Should not have Neptune proxy port (8182) ingress rule
    # Only SSH port (22) should be allowed
    ingress_rules = template.find_resources("AWS::EC2::SecurityGroupIngress")

    # Count ingress rules - should only have SSH (port 22), not Neptune proxy (port 8182)
    ssh_rules = 0
    proxy_rules = 0

    for rule_id, rule_props in ingress_rules.items():
        props = rule_props["Properties"]
        if props.get("FromPort") == 22:
            ssh_rules += 1
        elif props.get("FromPort") == 8182:
            proxy_rules += 1

    assert ssh_rules >= 1, "Should have SSH access rule"
    assert proxy_rules == 0, "Should not have Neptune proxy rule when disabled"


def test_bastion_stack_multiple_cidrs(vpc, neptune_security_group):
    """Test bastion stack with multiple allowed CIDR blocks"""
    bastion_config = {
        "instance_type": "t3.small",
        "enable_neptune_proxy": True,
        "allowed_ssh_cidrs": ["10.0.0.0/8", "192.168.1.0/24", "172.16.0.0/12"],
    }

    app = cdk.App()
    stack = NeptuneBastionStack(
        app,
        "TestBastionStackMultipleCIDRs",
        vpc=vpc,
        neptune_security_group=neptune_security_group,
        bastion_config=bastion_config,
    )
    template = Template.from_stack(stack)

    # Should have multiple ingress rules for different CIDRs
    ingress_rules = template.find_resources("AWS::EC2::SecurityGroupIngress")

    # Count unique CIDRs in ingress rules
    cidrs_found = set()
    for rule_id, rule_props in ingress_rules.items():
        props = rule_props["Properties"]
        if "CidrIp" in props:
            cidrs_found.add(props["CidrIp"])

    expected_cidrs = {"10.0.0.0/8", "192.168.1.0/24", "172.16.0.0/12"}
    assert expected_cidrs.issubset(
        cidrs_found
    ), f"Expected CIDRs {expected_cidrs} not all found in {cidrs_found}"


def test_bastion_iam_permissions(vpc, neptune_security_group, bastion_config):
    """Test that bastion has correct IAM permissions for Neptune"""
    app = cdk.App()
    stack = NeptuneBastionStack(
        app,
        "TestBastionIAM",
        vpc=vpc,
        neptune_security_group=neptune_security_group,
        bastion_config=bastion_config,
    )
    template = Template.from_stack(stack)

    # Check for Neptune permissions policy
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "neptune-db:ReadDataViaQuery",
                            "neptune-db:WriteDataViaQuery",
                            "neptune-db:DeleteDataViaQuery",
                            "neptune-db:GetEngineStatus",
                            "neptune-db:GetQueryStatus",
                            "neptune-db:CancelQuery",
                        ],
                        "Resource": "*",
                    }
                ]
            }
        },
    )

    # Check for Systems Manager permissions (for session access)
    # This comes from the managed policy AmazonSSMManagedInstanceCore
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "ManagedPolicyArns": [
                {
                    "Fn::Join": [
                        "",
                        [
                            "arn:",
                            {"Ref": "AWS::Partition"},
                            ":iam::aws:policy/AmazonSSMManagedInstanceCore",
                        ],
                    ]
                }
            ]
        },
    )
