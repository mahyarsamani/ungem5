"""Shared test setup: graphix on the path, its cache in a temporary directory.

Every test module imports this first, so no test reads or fills the user's
`~/.cache/graphix`.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["GRAPHIX_CACHE"] = os.environ.get("GRAPHIX_TEST_CACHE") or tempfile.mkdtemp(
    prefix="graphix-test-cache-"
)
os.environ["GRAPHIX_TEST_CACHE"] = os.environ["GRAPHIX_CACHE"]
