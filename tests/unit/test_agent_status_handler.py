import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

LAMBDA_AGENT_DIR = str(Path(__file__).parents[2] / "src" / "lambda_agent")
if LAMBDA_AGENT_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_AGENT_DIR)


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("JOB_TABLE_NAME", "test-job-table")


@pytest.fixture
def mock_table():
    table = MagicMock()
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        yield table


@pytest.fixture
def status_module(mock_table):
    spec = importlib.util.spec_from_file_location(
        "lambda_agent_status",
        Path(LAMBDA_AGENT_DIR) / "status.py",
    )
    assert spec and spec.loader
    status_handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(status_handler)
    status_handler._dynamodb.Table.return_value = mock_table
    return status_handler


def test_error_response_is_sanitized(status_module, mock_table):
    job_id = "job-123"
    mock_table.get_item.return_value = {
        "Item": {
            "job_id": job_id,
            "status": "error",
            "error": "ServiceUnavailableException: request id abc-123",
        }
    }

    response = status_module.handler({"pathParameters": {"job_id": job_id}}, {})
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["job_id"] == job_id
    assert body["status"] == "error"
    assert body["error"] == status_module.GENERIC_ERROR_MESSAGE
    assert body["correlation_id"] == job_id
    assert "ServiceUnavailableException" not in body["error"]


def test_complete_response_returns_answer(status_module, mock_table):
    job_id = "job-456"
    mock_table.get_item.return_value = {
        "Item": {"job_id": job_id, "status": "complete", "answer": "ok"}
    }

    response = status_module.handler({"pathParameters": {"job_id": job_id}}, {})
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body == {"job_id": job_id, "status": "complete", "answer": "ok"}
