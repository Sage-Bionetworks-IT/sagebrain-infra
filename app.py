import aws_cdk as cdk

from src.neptune_agent_stack import NeptuneAgentStack
from src.network_stack import NetworkStack
from src.neptune_api_stack import NeptuneApiStack
from src.neptune_sagemaker_stack import NeptuneSageMakerStack
from src.neptune_stack import NeptuneStack
from src.monitoring_stack import MonitoringStack
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

# SageMaker Studio for team access to Neptune (skipped in envs where enabled=false)
if config["NEPTUNE_SAGEMAKER"].get("enabled", True):
    NeptuneSageMakerStack(
        scope=cdk_app,
        construct_id=f"{STACK_NAME_PREFIX}-neptune-sagemaker",
        vpc=network_stack.vpc,
        neptune_security_group=neptune_stack.neptune_security_group,
        neptune_cluster_resource_id=neptune_stack.neptune_cluster.attr_cluster_resource_id,
        sagemaker_config=config["NEPTUNE_SAGEMAKER"],
        data_bucket=neptune_stack.data_bucket,
        env=env,
    )

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

# Bedrock Strands AI agent: async POST /ask + GET /ask/{job_id}
neptune_agent_stack = NeptuneAgentStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-neptune-agent",
    vpc=network_stack.vpc,
    neptune_query_url=f"{neptune_api_stack.api.url}query",
    neptune_query_status_url=f"{neptune_api_stack.api.url}query",
    env=env,
)

monitoring_stack = MonitoringStack(
    scope=cdk_app,
    construct_id=f"{STACK_NAME_PREFIX}-monitoring",
    query_api=neptune_api_stack.api,
    query_submit_fn=neptune_api_stack.submit_fn,
    query_status_fn=neptune_api_stack.status_fn,
    query_worker_fn=neptune_api_stack.query_fn,
    query_job_queue=neptune_api_stack.job_queue,
    query_dlq=neptune_api_stack.dlq,
    query_job_table=neptune_api_stack.job_table,
    agent_api=neptune_agent_stack.api,
    agent_submit_fn=neptune_agent_stack.submit_fn,
    agent_status_fn=neptune_agent_stack.status_fn,
    agent_worker_fn=neptune_agent_stack.agent_fn,
    agent_job_queue=neptune_agent_stack.job_queue,
    agent_dlq=neptune_agent_stack.dlq,
    agent_job_table=neptune_agent_stack.job_table,
    neptune_cluster_id=neptune_stack.neptune_cluster.ref,
    env=env,
)

cdk_app.synth()
