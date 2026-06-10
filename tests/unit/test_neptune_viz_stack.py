import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Match, Template

from src.neptune_viz_stack import NeptuneVizStack

ALLOWED_CIDR = "203.0.113.5/32"


@pytest.fixture(scope="module")
def template():
    app = cdk.App(context={"@aws-cdk/core:bundlingStacks": []})

    vpc_stack = cdk.Stack(app, "TestVpcStack")
    vpc = ec2.Vpc(vpc_stack, "TestVpc", max_azs=2)

    sg_stack = cdk.Stack(app, "TestSGStack")
    neptune_sg = ec2.SecurityGroup(
        sg_stack, "TestNeptuneSG", vpc=vpc, description="Test Neptune SG"
    )

    stack = NeptuneVizStack(
        app,
        "TestNeptuneVizStack",
        vpc=vpc,
        neptune_security_group=neptune_sg,
        neptune_cluster_resource_id="cluster-ABCDEFGHIJKLMNOP",
        neptune_endpoint="test-neptune.cluster-ro.us-east-1.neptune.amazonaws.com",
        viz_config={"enabled": True, "allowed_cidrs": [ALLOWED_CIDR]},
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Load balancer (internet-facing, IP-restricted, HTTP)
# ---------------------------------------------------------------------------


def test_alb_is_internet_facing(template):
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"Scheme": "internet-facing", "Type": "application"},
    )


def test_listener_is_http_on_port_80(template):
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Port": 80, "Protocol": "HTTP"},
    )


def test_alb_ingress_restricted_to_allowed_cidr(template):
    # The ALB SG must admit only the configured CIDR on port 80.
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "GroupDescription": Match.string_like_regexp("Graph Explorer ALB.*"),
            "SecurityGroupIngress": Match.array_with(
                [
                    Match.object_like(
                        {
                            "CidrIp": ALLOWED_CIDR,
                            "FromPort": 80,
                            "ToPort": 80,
                            "IpProtocol": "tcp",
                        }
                    )
                ]
            ),
        },
    )


def test_no_ingress_open_to_the_world(template):
    # Graph Explorer has no auth — the IP allow-list IS the access control,
    # so nothing may admit 0.0.0.0/0 on ingress. (Egress to 0.0.0.0/0 is fine.)
    # Inline SecurityGroup ingress rules.
    for sg in template.find_resources("AWS::EC2::SecurityGroup").values():
        for rule in sg["Properties"].get("SecurityGroupIngress", []):
            assert rule.get("CidrIp") != "0.0.0.0/0"
            assert rule.get("CidrIpv6") != "::/0"
    # Standalone ingress rule resources.
    for rule in template.find_resources("AWS::EC2::SecurityGroupIngress").values():
        props = rule["Properties"]
        assert props.get("CidrIp") != "0.0.0.0/0"
        assert props.get("CidrIpv6") != "::/0"


# ---------------------------------------------------------------------------
# Task role — read-only Neptune only
# ---------------------------------------------------------------------------


def test_task_role_read_only_neptune_actions(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Action": Match.array_equals(
                                    [
                                        "neptune-db:ReadDataViaQuery",
                                        "neptune-db:GetEngineStatus",
                                        "neptune-db:GetQueryStatus",
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_task_role_has_no_write_or_wildcard_neptune_actions(template):
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            assert "neptune-db:*" not in actions
            assert "neptune-db:WriteDataViaQuery" not in actions
            assert "neptune-db:DeleteDataViaQuery" not in actions


def test_task_role_assumed_by_ecs_tasks(template):
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                            }
                        )
                    ]
                )
            }
        },
    )


# ---------------------------------------------------------------------------
# Neptune security group ingress
# ---------------------------------------------------------------------------


def test_neptune_ingress_rule_on_port_8182(template):
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {
            "FromPort": 8182,
            "ToPort": 8182,
            "IpProtocol": "tcp",
            "Description": "Neptune access from Graph Explorer",
        },
    )


# ---------------------------------------------------------------------------
# Fargate task / service
# ---------------------------------------------------------------------------


def test_task_definition_is_x86_64_fargate(template):
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "RequiresCompatibilities": ["FARGATE"],
            "RuntimePlatform": {
                "CpuArchitecture": "X86_64",
                "OperatingSystemFamily": "LINUX",
            },
        },
    )


def test_container_uses_graph_explorer_image(template):
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "ContainerDefinitions": Match.array_with(
                [
                    Match.object_like(
                        {"Image": "public.ecr.aws/neptune/graph-explorer:latest"}
                    )
                ]
            )
        },
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("IAM", "true"),  # SigV4 signing
        ("SERVICE_TYPE", "neptune-db"),
        ("GRAPH_TYPE", "sparql"),
        ("USING_PROXY_SERVER", "true"),
    ],
)
def test_container_env_var(template, name, value):
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "ContainerDefinitions": Match.array_with(
                [
                    Match.object_like(
                        {
                            "Environment": Match.array_with(
                                [{"Name": name, "Value": value}]
                            )
                        }
                    )
                ]
            )
        },
    )


def test_service_does_not_assign_public_ip(template):
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "NetworkConfiguration": {
                "AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "DISABLED"})
            }
        },
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_graph_explorer_url_output_exists(template):
    template.has_output("GraphExplorerUrl", {})
