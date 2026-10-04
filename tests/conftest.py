import sys
from pathlib import Path

# Shared server-side package (core/sagebrain_core). In Lambda it ships as a layer; in the
# FastAPI image it's pip-installed. Tests import it from source.
# The FastAPI app (app/sagebrain_api) is imported from source the same way.
ROOT = Path(__file__).parents[1]
for _dir in (ROOT / "core", ROOT / "app"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
