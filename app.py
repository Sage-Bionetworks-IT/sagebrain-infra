import aws_cdk as cdk

from src.network_stack import NetworkStack
from src.neptune_stack import NeptuneStack
from src.neptune_bastion_stack import NeptuneBastionStack
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

# Neptune bastion host for local development access
neptune_bastion_stack = NeptuneBastionStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-neptune-bastion",
    vpc=network_stack.vpc,
    neptune_security_group=neptune_stack.neptune_security_group,
    bastion_config=config["NEPTUNE_BASTION"],
    env=env,
)
# Note: No explicit dependency needed as the security group reference creates the implicit dependency

cdk_app.synth()
