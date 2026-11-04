"""Shared test setup for the stats tests: the repo on the path, the stats
cache in a temporary directory.

Every stats test module imports this first, so no test reads or fills the
user's `~/.cache/ungem5/stats`.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["UNGEM5_CACHE"] = os.environ.get("UNGEM5_TEST_CACHE") or tempfile.mkdtemp(
    prefix="ungem5-test-cache-"
)
os.environ["UNGEM5_TEST_CACHE"] = os.environ["UNGEM5_CACHE"]
