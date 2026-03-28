import aws_cdk as cdk

from src.network_stack import NetworkStack
from src.neptune_api_stack import NeptuneApiStack
from src.neptune_sagemaker_stack import NeptuneSageMakerStack
from src.neptune_stack import NeptuneStack
from src.utils import load_context_config

cdk_app = cdk.App()
env_name = cdk_app.node.try_get_context("env") or "dev"
config = load_context_config(env_name=env_name)
STACK_NAME_PREFIX = f"app-{env_name}"
TAGS = config["TAGS"]

# Define the deployment environment
env = cdk.Environment(account="620117233256", region="us-east-1")  # sagebrain account

# recursively apply tags to all stack resources
if TAGS:
    for key, value in TAGS.items():
        cdk.Tags.of(cdk_app).add(key, value)

network_stack = NetworkStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-network",
    vpc_cidr=config["VPC_CIDR"],
    env=env,
)

neptune_stack = NeptuneStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-neptune",
    vpc=network_stack.vpc,
    neptune_config=config["NEPTUNE"],
    env=env,
)
neptune_stack.add_dependency(network_stack)

# SageMaker Studio for team access to Neptune
neptune_sagemaker_stack = NeptuneSageMakerStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-neptune-sagemaker",
    vpc=network_stack.vpc,
    neptune_security_group=neptune_stack.neptune_security_group,
    neptune_cluster_resource_id=neptune_stack.neptune_cluster.attr_cluster_resource_id,
    sagemaker_config=config["NEPTUNE_SAGEMAKER"],
    env=env,
)
# Note: No explicit dependency needed as the direct references create implicit dependencies

# Public read-only API for Neptune
neptune_api_stack = NeptuneApiStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-neptune-api",
    vpc=network_stack.vpc,
    neptune_read_endpoint=neptune_stack.neptune_cluster.attr_read_endpoint,
    neptune_cluster_resource_id=neptune_stack.neptune_cluster.attr_cluster_resource_id,
    neptune_security_group=neptune_stack.neptune_security_group,
    env=env,
)
# Note: No explicit dependency needed as the direct references create implicit dependencies

cdk_app.synth()
