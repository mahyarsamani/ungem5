"""Make a run whose caches are named differently, for the owners tutorial.

All example sweeps name the L1 and L2 caches `dcache`, `icache`, and
`l2cache`.
To show an `OwnerScheme` merging two naming conventions, this script copies
one example run into `data/profiling_renamed/` and renames those caches to
`l1dcache`, `l1icache`, and `l2` in both `config.json` and the stats dump.
The copy is generated (and gitignored), never edited by hand.
"""

import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / "example_data" / "profiling"
TARGET = HERE / "data" / "profiling_renamed"

RENAMES = {"dcache": "l1dcache", "icache": "l1icache", "l2cache": "l2"}
_PATTERN = re.compile(r"\b(" + "|".join(RENAMES) + r")\b")


def make_renamed_run(source_run: str = None) -> Path:
    """Create the renamed copy (once) and return its directory."""
    runs = sorted(p for p in SOURCE.iterdir() if (p / "config.json").exists())
    source = SOURCE / source_run if source_run else runs[0]
    target = TARGET / source.name
    if target.exists():
        return target
    target.mkdir(parents=True)
    for path in source.iterdir():
        if path.name == "constructor.py":
            shutil.copy2(path, target / path.name)
            continue
        text = path.read_text()
        renamed = _PATTERN.sub(lambda m: RENAMES[m.group(1)], text)
        (target / path.name).write_text(renamed)
    return target


if __name__ == "__main__":
    print(make_renamed_run())
