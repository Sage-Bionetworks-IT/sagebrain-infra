"""Runtime settings, read from the environment (ECS task definition / ECS secrets).

Limit values are not settings: they come from `sagebrain_core.limits` (== the spec's
x-sagebrain-limits), so they can't drift per deployment.
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from sagebrain_core.ratelimit import DEFAULT_FALLBACK_INSTANCES

# Repo layout, mirrored in the image: <root>/app/sagebrain_api, <root>/api/openapi.yaml.
DEFAULT_SPEC_PATH = Path(__file__).parents[2] / "api" / "openapi.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    synapse_team_id: str
    # Enables x-api-key auth when set. Arrives as an ECS secret, never in config YAML.
    machine_api_key: SecretStr | None = None

    query_job_table_name: str
    query_job_queue_url: str
    ask_job_table_name: str
    ask_job_queue_url: str

    # The shared DynamoDB token buckets. Unset: in-process buckets (tests, offline profile).
    rate_limit_table_name: str | None = None
    # Tasks sharing the buckets; each gets rate / this when DynamoDB is unreachable.
    rate_limit_fallback_instances: int = Field(DEFAULT_FALLBACK_INSTANCES, ge=1)

    # Written into `servers` of /api/openapi.json so "Try it out" targets this deployment.
    public_base_url: str = "http://localhost:8000"
    # Status returned when Synapse can't be reached (research.md F3).
    auth_transient_status: int = 500

    synapse_repo_api: str = "https://repo-prod.prod.sagebase.org/repo/v1"
    synapse_auth_api: str = "https://repo-prod.prod.sagebase.org/auth/v1"

    openapi_spec_path: Path = DEFAULT_SPEC_PATH
    # swagger-ui-dist, unpacked into the image by app/Dockerfile.
    swagger_ui_dir: Path = Path("/opt/swagger-ui")
