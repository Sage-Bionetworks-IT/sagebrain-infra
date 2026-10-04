import sys
from pathlib import Path

# Shared server-side package (core/sagebrain_core). In Lambda it ships as a layer; in the
# FastAPI image it's pip-installed. Tests import it from source.
CORE_DIR = str(Path(__file__).parents[1] / "core")
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)
