import aws_cdk as cdk

from src.ecs_stack import EcsStack
from src.load_balancer_stack import LoadBalancerStack
from src.network_stack import NetworkStack
from src.service_props import ServiceProps
from src.service_stack import LoadBalancedServiceStack
from src.utils import load_context_config

cdk_app = cdk.App()
env_name = cdk_app.node.try_get_context("env") or "dev"
config = load_context_config(env_name=env_name)
STACK_NAME_PREFIX = f"app-{env_name}"
FQDN = config["FQDN"]
TAGS = config["TAGS"]
APP_VERSION = "latest"

# recursively apply tags to all stack resources
if TAGS:
    for key, value in TAGS.items():
        cdk.Tags.of(cdk_app).add(key, value)

network_stack = NetworkStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-network",
    vpc_cidr=config["VPC_CIDR"],
)

ecs_stack = EcsStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-ecs",
    vpc=network_stack.vpc,
    namespace=FQDN,
)

# From AWS docs https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect-concepts-deploy.html
# The public discovery and reachability should be created last by AWS CloudFormation, including the frontend
# client service. The services need to be created in this order to prevent an time period when the frontend
# client service is running and available the public, but a backend isn't.
load_balancer_stack = LoadBalancerStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-load-balancer",
    vpc=network_stack.vpc,
)
load_balancer_stack.add_dependency(ecs_stack)

app_props = ServiceProps(
    ecs_task_cpu=256,
    ecs_task_memory=512,
    container_name="my-app",
    # can also reference github with 'ghcr.io/sage-bionetworks/my-app:{APP_VERSION}'
    container_location=f"nginx:{APP_VERSION}",
    container_port=80,
    container_env_vars={
        "APP_VERSION": f"{APP_VERSION}",
    },
)
app_stack = LoadBalancedServiceStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-app",
    vpc=network_stack.vpc,
    cluster=ecs_stack.cluster,
    props=app_props,
    load_balancer=load_balancer_stack.alb,
)

cdk_app.synth()
