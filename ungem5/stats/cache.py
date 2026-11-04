"""A cache of parsed stats dumps.

Parsing a 20 to 30 MB dump takes about 0.3 s; reading its cached lines takes
about 0.1 s and 1 to 2 MB of disk (zlib level 1).
An entry holds the `StatLine`s of one dump and is keyed by the stats file's
resolved path, size and modification time, the dump index asked for, and
`PARSER_VERSION`, so a file still being written (a running simulation) or a
parser change never gives a stale hit.
The lines do not depend on specs or owner schemes, so changing those never
needs a re-parse.

The directory is `$UNGEM5_CACHE`, or `~/.cache/ungem5/stats`; set
`UNGEM5_CACHE=off` (or pass `cache=False`) to bypass it, and call `clear()`
to empty it.
"""

import hashlib
import os
import pickle
import zlib
from pathlib import Path
from typing import List, Optional

# Bump when parse.py changes what a StatLine holds or how a line is read.
PARSER_VERSION = 1


def cache_dir() -> Optional[Path]:
    """The cache directory, or None when the cache is off."""
    setting = os.environ.get("UNGEM5_CACHE", "")
    if setting.lower() in ("off", "0", "no", "false"):
        return None
    return Path(setting) if setting else Path.home() / ".cache" / "ungem5" / "stats"


def _entry(path: Path, dump: int) -> Optional[Path]:
    directory = cache_dir()
    if directory is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = f"{PARSER_VERSION}|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{dump}"
    digest = hashlib.sha1(key.encode()).hexdigest()
    return directory / digest[:2] / f"{digest}.pkl.z"


def load(path: Path, dump: int) -> Optional[List]:
    """The cached lines of a dump, or None on a miss or a bad entry."""
    entry = _entry(Path(path), dump)
    if entry is None or not entry.exists():
        return None
    try:
        return pickle.loads(zlib.decompress(entry.read_bytes()))
    except Exception:  # a truncated or foreign file: treat as a miss
        return None


def store(path: Path, dump: int, lines: List) -> None:
    """Write a dump's lines; a failure to write is not an error."""
    entry = _entry(Path(path), dump)
    if entry is None:
        return
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        temporary = entry.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(zlib.compress(pickle.dumps(lines, protocol=5), 1))
        temporary.replace(entry)
    except OSError:
        pass


def clear() -> int:
    """Remove every cache entry; return how many were removed."""
    directory = cache_dir()
    if directory is None or not directory.exists():
        return 0
    removed = 0
    for entry in directory.glob("*/*.pkl.z"):
        entry.unlink(missing_ok=True)
        removed += 1
    return removed
