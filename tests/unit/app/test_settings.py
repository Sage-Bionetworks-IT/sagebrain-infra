"""T111 — app skeleton: settings from the environment, pinned requirements."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from sagebrain_api.settings import Settings

APP_DIR = Path(__file__).parents[3] / "app"

REQUIRED_ENV = {
    "SYNAPSE_TEAM_ID": "3605470",
    "QUERY_JOB_TABLE_NAME": "query-jobs",
    "QUERY_JOB_QUEUE_URL": "https://sqs.example/query",
    "ASK_JOB_TABLE_NAME": "ask-jobs",
    "ASK_JOB_QUEUE_URL": "https://sqs.example/ask",
}


@pytest.fixture
def env(monkeypatch):
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_settings_from_environment(env):
    env.setenv("MACHINE_API_KEY", "k-123")
    env.setenv("PUBLIC_BASE_URL", "https://api.dev.example")
    settings = Settings()
    assert settings.synapse_team_id == "3605470"
    assert settings.query_job_table_name == "query-jobs"
    assert settings.ask_job_queue_url == "https://sqs.example/ask"
    assert settings.machine_api_key.get_secret_value() == "k-123"
    assert settings.public_base_url == "https://api.dev.example"


def test_auth_transient_status_defaults_to_500(env):
    # research.md F3: API Gateway answers an authorizer error with AUTHORIZER_FAILURE (500).
    assert Settings().auth_transient_status == 500
    env.setenv("AUTH_TRANSIENT_STATUS", "503")
    assert Settings().auth_transient_status == 503


def test_machine_api_key_is_optional_and_never_shown(env):
    assert Settings().machine_api_key is None
    env.setenv("MACHINE_API_KEY", "super-secret")
    settings = Settings()
    assert "super-secret" not in repr(settings)
    assert "super-secret" not in str(settings.model_dump())


def test_missing_required_setting_fails(env):
    env.delenv("SYNAPSE_TEAM_ID")
    with pytest.raises(ValidationError):
        Settings()


def test_runtime_requirements_are_pinned():
    lines = [
        line.split("#")[0].strip()
        for line in (APP_DIR / "requirements.txt").read_text().splitlines()
    ]
    reqs = [line for line in lines if line]
    assert reqs, "app/requirements.txt is empty"
    for req in reqs:
        assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[0-9][\w.]*", req), req
