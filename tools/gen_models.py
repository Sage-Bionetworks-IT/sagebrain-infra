#!/usr/bin/env python
"""Generate app/sagebrain_api/models/_generated.py from api/openapi.yaml (constitution, article I).

    python tools/gen_models.py           # regenerate
    python tools/gen_models.py --check   # exit 1 if the committed file has drifted from the spec

Needs datamodel-code-generator, which can't share an env with aws-cdk-lib (its `inflect`
dependency wants typeguard>=4; jsii pins 2.13.3). Run it through pre-commit, which builds an
isolated env with the pinned versions:

    pre-commit run openapi-models --all-files
"""

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = ROOT / "api" / "openapi.yaml"
OUTPUT = ROOT / "app" / "sagebrain_api" / "models" / "_generated.py"
HEADER = (
    "# GENERATED from api/openapi.yaml by tools/gen_models.py. Do not edit by hand:\n"
    "# change the spec, then run `pre-commit run openapi-models --all-files`."
)

ARGS = [
    "--input-file-type=openapi",
    "--output-model-type=pydantic_v2.BaseModel",
    "--target-python-version=3.13",
    "--disable-timestamp",
    "--use-annotated",
    "--field-constraints",
    "--use-standard-collections",
    "--use-union-operator",
    "--use-double-quotes",
    "--enum-field-as-literal=all",
    "--collapse-root-models",
    "--formatters",
    "black",
    "isort",
]


def generate(output: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            f"--input={SPEC}",
            f"--output={output}",
            f"--custom-file-header={HEADER}",
            *ARGS,
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on drift")
    args = parser.parse_args()

    if not args.check:
        generate(OUTPUT)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "_generated.py"
        generate(fresh)
        expected = fresh.read_text()
    actual = OUTPUT.read_text() if OUTPUT.exists() else ""
    if actual == expected:
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(
            actual.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            str(OUTPUT.relative_to(ROOT)),
            "generated from api/openapi.yaml",
        )
    )
    print("\nModels drifted from the spec: run `python tools/gen_models.py`.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
