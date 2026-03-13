#!/usr/bin/env bash

# Safer bash scripts
set -euxo pipefail

# Install Node.js dependencies
npm install -g aws-cdk@2.1031.2

# Install Python dependencies
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

# Install git hooks
git config --global --add safe.directory "$PWD"
pre-commit install --install-hooks
