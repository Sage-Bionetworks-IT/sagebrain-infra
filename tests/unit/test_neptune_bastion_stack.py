import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Template

from src.neptune_bastion_stack import NeptuneBastionStack


def make_stack(bastion_config):
    """Helper: create a full CDK app with VPC, Neptune SG, and bastion stack."""
    app = cdk.App()
    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)

    sg_stack = cdk.Stack(app, "TestSGStack")
    neptune_sg = ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune security group"
    )

    bastion_stack = NeptuneBastionStack(
        app,
        "TestBastionStack",
        vpc=vpc,
        neptune_security_group=neptune_sg,
        bastion_config=bastion_config,
    )
    return Template.from_stack(bastion_stack)


def test_bastion_stack_creation():
    """Test that bastion stack creates required resources"""
    template = make_stack({"instance_type": "t3.micro"})

    template.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t3.micro"})

    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"GroupDescription": "Security group for Neptune bastion host"},
    )

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


def test_bastion_no_ssh_ingress():
    """Test that bastion security group has no inbound SSH rules"""
    template = make_stack({"instance_type": "t3.micro"})

    ingress_rules = template.find_resources("AWS::EC2::SecurityGroupIngress")
    for rule_props in ingress_rules.values():
        assert (
            rule_props["Properties"].get("FromPort") != 22
        ), "SSH port 22 should not be open"


def test_bastion_no_key_pair():
    """Test that bastion has no SSH key pair configured"""
    template = make_stack({"instance_type": "t3.micro"})

    instances = template.find_resources("AWS::EC2::Instance")
    for instance in instances.values():
        assert "KeyName" not in instance.get(
            "Properties", {}
        ), "No key pair should be configured"


def test_bastion_no_proxy_port_exposed():
    """Test that bastion security group does not expose port 8182 as inbound.
    The bastion stack owns a CfnSecurityGroupIngress rule on port 8182, but it
    targets the Neptune SG (not the bastion SG), so the bastion SG itself must
    not have port 8182 open inbound."""
    template = make_stack({"instance_type": "t3.micro"})

    # Find the bastion SG logical ID so we can exclude it from the check
    bastion_sgs = template.find_resources(
        "AWS::EC2::SecurityGroup",
        {"Properties": {"GroupDescription": "Security group for Neptune bastion host"}},
    )
    bastion_sg_ids = set(bastion_sgs.keys())

    ingress_rules = template.find_resources("AWS::EC2::SecurityGroupIngress")
    for rule_props in ingress_rules.values():
        props = rule_props["Properties"]
        if props.get("FromPort") == 8182:
            # Port 8182 is allowed only when targeting the Neptune SG (not bastion SG)
            group_id = props.get("GroupId", {})
            ref = (
                group_id.get("Fn::GetAtt", [None])[0]
                if isinstance(group_id, dict)
                else None
            )
            assert (
                ref not in bastion_sg_ids
            ), "Neptune proxy port 8182 should not be exposed on the bastion SG"


def test_bastion_in_public_subnet():
    """Test that bastion instance is placed in a public subnet"""
    template = make_stack({"instance_type": "t3.micro"})

    instances = template.find_resources("AWS::EC2::Instance")
    assert len(instances) == 1
    instance = list(instances.values())[0]
    # CDK sets SubnetId directly on the instance; the exported name includes "Public"
    subnet_id = instance["Properties"].get("SubnetId", {})
    import_value = subnet_id.get("Fn::ImportValue", "")
    assert "Public" in import_value, f"Expected a public subnet, got: {import_value}"


def test_bastion_ebs_volume_encrypted():
    """Test that the root EBS volume is encrypted"""
    template = make_stack({"instance_type": "t3.micro"})

    template.has_resource_properties(
        "AWS::EC2::Instance",
        {
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {"Encrypted": True, "VolumeType": "gp3"},
                }
            ]
        },
    )


def test_bastion_imdsv2_required():
    """Test that IMDSv2 is enforced on the bastion instance"""
    template = make_stack({"instance_type": "t3.micro"})

    # CDK sets require_imdsv2 via a LaunchTemplate, not directly on the instance
    template.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {
            "LaunchTemplateData": {
                "MetadataOptions": {"HttpTokens": "required"},
            }
        },
    )


def test_bastion_outputs_exist():
    """Test that BastionInstanceId and SSMCommand outputs are present"""
    template = make_stack({"instance_type": "t3.micro"})

    template.has_output("BastionInstanceId", {})
    template.has_output("SSMCommand", {})


def test_bastion_iam_permissions():
    """Test that bastion has correct IAM permissions for Neptune"""
    template = make_stack({"instance_type": "t3.micro"})

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
                            "neptune-db:ResetDatabase",
                        ],
                        "Resource": "*",
                    }
                ]
            }
        },
    )

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
                },
                {
                    "Fn::Join": [
                        "",
                        [
                            "arn:",
                            {"Ref": "AWS::Partition"},
                            ":iam::aws:policy/CloudWatchAgentServerPolicy",
                        ],
                    ]
                },
            ]
        },
    )
