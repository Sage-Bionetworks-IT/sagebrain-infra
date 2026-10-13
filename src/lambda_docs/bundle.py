"""Build step for the docs Lambda asset. Runs inside the CDK bundling container.

Copies the contract into the asset, and fetches a pinned Swagger UI from the npm registry,
checking it against the registry's published sha512. The Lambda then serves Swagger UI
itself rather than loading it from a CDN (spec 001 FR-8).

Usage: python bundle.py <path to openapi.yaml> <asset output dir>
"""

import base64
import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

# To upgrade: take `version` and `dist.integrity` from
# https://registry.npmjs.org/swagger-ui-dist/latest (Swagger UI 5+ renders OpenAPI 3.1).
SWAGGER_UI_VERSION = "5.33.2"
SWAGGER_UI_INTEGRITY = "sha512-MyrWX06zkJRYPnIau8Qj8F6KfzS+Qj8D9AR36vCWkkC9ts1r1DYF84apgCvZBzO2Wzm7G7loCd5VS+4JLWoyLQ=="
SWAGGER_UI_URL = f"https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-{SWAGGER_UI_VERSION}.tgz"
SWAGGER_UI_FILES = ("swagger-ui-bundle.js", "swagger-ui.css", "LICENSE", "NOTICE")


def fetch_swagger_ui(out_dir: Path) -> None:
    with urllib.request.urlopen(SWAGGER_UI_URL, timeout=60) as resp:
        tarball = resp.read()
    digest = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    if digest != SWAGGER_UI_INTEGRITY:
        raise SystemExit(f"swagger-ui-dist {SWAGGER_UI_VERSION}: integrity mismatch")

    target = out_dir / "swagger-ui"
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for name in SWAGGER_UI_FILES:
            member = tar.extractfile(f"package/{name}")
            (target / name).write_bytes(member.read())


def main(spec_path: str, out_dir: str) -> None:
    out = Path(out_dir)
    shutil.copy(spec_path, out / "openapi.yaml")
    fetch_swagger_ui(out)


if __name__ == "__main__":
    main(*sys.argv[1:])
