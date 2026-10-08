"""T118 — image properties that review alone would miss. CI also builds and smoke-tests it."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]
# Backslash continuations joined, so each instruction is one line.
DOCKERFILE = re.sub(r"\\\n\s*", " ", (ROOT / "app" / "Dockerfile").read_text())
IGNORE = (ROOT / "app" / "Dockerfile.dockerignore").read_text().splitlines()
COMPOSE = yaml.safe_load((ROOT / "app" / "compose.yaml").read_text())


def _instructions(name):
    return re.findall(rf"^{name}\s+(.+)$", DOCKERFILE, flags=re.MULTILINE)


def test_every_stage_is_arm64_and_digest_pinned():
    stages = _instructions("FROM")
    assert stages
    for stage in stages:
        assert stage.startswith("--platform=linux/arm64 "), stage
    (base,) = set(re.findall(r"^ARG PYTHON_IMAGE=(.+)$", DOCKERFILE, re.MULTILINE))
    assert "@sha256:" in base


def test_runs_as_non_root():
    users = _instructions("USER")
    assert users and users[-1] not in ("root", "0")


def test_installs_sagebrain_core_rather_than_copying_it():
    assert any("pip install" in r and "core" in r for r in _instructions("RUN"))
    assert not any("sagebrain_core" in c for c in _instructions("COPY"))


def test_swagger_ui_is_checksum_pinned():
    (add,) = [a for a in _instructions("ADD") if "swagger-ui-dist" in a]
    assert re.search(r"--checksum=sha256:[0-9a-f]{64}", add)


def test_spec_is_shipped_where_settings_expects_it():
    # settings.DEFAULT_SPEC_PATH = <root>/api/openapi.yaml, with the package at <root>/app.
    assert any(c.split()[0] == "api/openapi.yaml" for c in _instructions("COPY"))


def test_build_context_is_an_allowlist():
    assert IGNORE[0] == "*"
    allowed = {line[1:].rstrip("/") for line in IGNORE if line.startswith("!")}
    assert allowed == {
        "app/requirements.txt",
        "app/sagebrain_api",
        "core",
        "api/openapi.yaml",
    }


def test_compose_runs_api_only():
    services = COMPOSE["services"]
    assert set(services) == {"api"}
    assert services["api"]["build"]["dockerfile"] == "app/Dockerfile"
    assert "depends_on" not in services["api"]
