# syntax=docker/dockerfile:1.7
# Sage Brain API (spec 001). ARM64 (Graviton Fargate, R-6), non-root.
# Build from the repo root — the image needs core/ and api/openapi.yaml too:
#   docker build --platform linux/arm64 -f app/Dockerfile -t sagebrain-api .
# The build context is filtered by app/Dockerfile.dockerignore (an allowlist).

ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed

# Swagger UI assets, served by the app at /api/static (FR-9: no CDN).
FROM --platform=linux/arm64 ${PYTHON_IMAGE} AS swagger-ui
ADD --checksum=sha256:b468ff5f49451f194a739bc245c85b28c7d3d33057a8409a9b2369da37e215b9 \
    https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.1.tgz /tmp/swagger-ui.tgz
RUN mkdir /swagger-ui \
    && tar -xzf /tmp/swagger-ui.tgz -C /swagger-ui --strip-components=1 \
       package/swagger-ui.css package/swagger-ui-bundle.js package/favicon-32x32.png \
       package/LICENSE package/NOTICE

FROM --platform=linux/arm64 ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

COPY app/requirements.txt app/requirements.txt
RUN pip install -r app/requirements.txt

# The shared query service. Its runtime deps (requests, botocore) are pinned above.
COPY core/ /tmp/core/
RUN pip install --no-deps /tmp/core && rm -rf /tmp/core

# Layout mirrors the repo: settings.DEFAULT_SPEC_PATH resolves to /srv/api/openapi.yaml.
COPY api/openapi.yaml api/openapi.yaml
COPY app/sagebrain_api app/sagebrain_api
COPY --from=swagger-ui /swagger-ui /opt/swagger-ui

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

ENV PYTHONPATH=/srv/app \
    SWAGGER_UI_DIR=/opt/swagger-ui \
    PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["python", "-m", "sagebrain_api"]
