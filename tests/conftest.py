import sys
from pathlib import Path

import pytest

# Shared server-side package (core/sagebrain_core). In Lambda it ships as a layer; in the
# FastAPI image it's pip-installed. Tests import it from source.
# The FastAPI app (app/sagebrain_api) is imported from source the same way.
ROOT = Path(__file__).parents[1]
for _dir in (ROOT / "core", ROOT / "app"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

RATE_LIMIT_TABLE = "test-rate-limits"


@pytest.fixture
def rate_limit_table(monkeypatch):
    """A moto `rate-limits` table (PK `key`), with fake credentials and no network.

    Yields a botocore DynamoDB client bound to the mock. `sagebrain_core.ratelimit`'s lazy
    module-level client is reset so code under test creates its own inside the mock.
    """
    from moto import mock_aws
    from sagebrain_core import ratelimit

    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(ratelimit, "_client", None)
    with mock_aws():
        client = ratelimit._dynamodb_client()
        client.create_table(
            TableName=RATE_LIMIT_TABLE,
            AttributeDefinitions=[{"AttributeName": "key", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client
