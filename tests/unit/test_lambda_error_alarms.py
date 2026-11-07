import aws_cdk as cdk
import pytest

from src.lambda_error_alarms import readable_name


@pytest.mark.parametrize(
    "stack_name, path, expected",
    [
        ("app-dev-api", "QuerySubmitFunction", "app-dev-api-QuerySubmitFunction"),
        (
            "app-dev-neptune",
            "BucketNotificationsHandler050a0587b7544547bf325f094a3db834",
            "app-dev-neptune-BucketNotificationsHandler",
        ),
        (
            "app-dev-network",
            "Custom::VpcRestrictDefaultSGCustomResourceProvider/Handler",
            "app-dev-network-Custom-VpcRestrictDefaultSGCustomResourceProvider-Handler",
        ),
    ],
)
def test_readable_name(stack_name, path, expected):
    stack = cdk.Stack(cdk.App(), stack_name)
    assert readable_name(stack, path) == expected
