import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct


def sagebrain_core_layer(scope: Construct, construct_id: str = "SageBrainCoreLayer"):
    """Lambda layer carrying core/sagebrain_core (the shared server-side query service).

    Pure Python, so one layer serves the x86 query worker and the ARM64 agent worker.
    Its third-party deps (requests) are bundled by each function; botocore ships with the runtime.
    """
    return lambda_.LayerVersion(
        scope,
        construct_id,
        code=lambda_.Code.from_asset(
            "core",
            exclude=["__pycache__", "*.pyc", "*.egg-info"],
            bundling=cdk.BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                command=[
                    "bash",
                    "-c",
                    "mkdir -p /asset-output/python && cp -r sagebrain_core /asset-output/python/"
                    # bundling mounts the raw source dir, so `exclude` doesn't filter the copy
                    " && find /asset-output -name __pycache__ -prune -exec rm -rf {} +",
                ],
            ),
        ),
        compatible_runtimes=[lambda_.Runtime.PYTHON_3_11],
        compatible_architectures=[
            lambda_.Architecture.X86_64,
            lambda_.Architecture.ARM_64,
        ],
        description="sagebrain_core: limits, validation, Neptune execution, audit log",
    )


def scope_to_live_graphs(
    fn: lambda_.Function, *, registry_table_name: str, graph_scoping: bool
):
    """Let a query-service caller read the live registry, and turn scoping on or off (spec 004).

    The registry is the `_live` partition of the pipeline's tracking table. The ARN is built
    from its deterministic name, so no cross-stack export; the grant is `Query` on that one
    partition only.
    """
    stack = cdk.Stack.of(fn)
    fn.add_environment("LOAD_TABLE_NAME", registry_table_name)
    fn.add_environment("GRAPH_SCOPING", "true" if graph_scoping else "false")
    fn.add_to_role_policy(
        iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["dynamodb:Query"],
            resources=[
                stack.format_arn(
                    service="dynamodb",
                    resource="table",
                    resource_name=registry_table_name,
                )
            ],
            conditions={
                "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["_live"]}
            },
        )
    )
