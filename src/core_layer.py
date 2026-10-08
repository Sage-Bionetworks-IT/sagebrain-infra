import aws_cdk as cdk
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
        description="sagebrain_core: limits, validation, rate limiting, Neptune execution, audit log",
    )
