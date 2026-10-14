import sys
from pathlib import Path

# Shared server-side package (core/sagebrain_core). In Lambda it ships as a layer; tests
# import it from source.
ROOT = Path(__file__).parents[1]
for _dir in (ROOT / "core",):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
