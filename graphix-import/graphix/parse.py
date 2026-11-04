"""Read one statistics dump from a gem5 text stats file.

gem5 writes every stat line with `Text::visit` in `src/base/stats/text.cc`.
A line has one of two shapes:

    <name> <value> [<pdf>% <cdf>%] [# <description>] [(<unit>)]
    <name> | <v> <pdf>% <cdf>% | <v> <pdf>% <cdf>% ... [# <description>] [(<unit>)]

The second shape is the `oneline` flag, used by Ruby's collated controller
vectors and by most Ruby histograms.
Values are printed in fixed notation, or as `nan` or `inf`.
"""

import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union
from warnings import warn

from . import cache as _cache

BEGIN_MARKER = "Begin Simulation Statistics"
END_MARKER = "End Simulation Statistics"

_DUMP_FILE = re.compile(r"stats_dump_(\d+)\.txt")


@dataclass(frozen=True)
class StatLine:
    """One parsed line of a stats dump.

    `values` has one entry for a plain line and one entry per bucket or
    element for a one-line (`|`) line.
    `occurrence` numbers lines that repeat the same full name within one dump
    (for example, the `avg_size` stats of several unnamed `TBEStorage`
    groups merged into the same controller); it is 0 for the first one.
    """

    name: str
    values: Tuple[float, ...]
    oneline: bool
    description: Optional[str]
    unit: Optional[str]
    lineno: int
    occurrence: int = 0


class StatsParseError(ValueError):
    """A line of a stats file could not be parsed."""

    def __init__(self, path: Path, lineno: int, line: str, reason: str):
        super().__init__(f"{path}:{lineno}: {reason}: {line.rstrip()!r}")
        self.path = path
        self.lineno = lineno


def find_dump_file(run_dir: Union[str, Path]) -> Path:
    """Return the stats file to read for one run directory.

    This is the `stats_dump_<N>.txt` with the largest N.
    If there is none, it falls back to `stats.txt`, and `read_dump` then takes
    its last dump.
    """
    run_dir = Path(run_dir)
    dumps = []
    for path in run_dir.iterdir():
        match = _DUMP_FILE.fullmatch(path.name)
        if match:
            dumps.append((int(match.group(1)), path))
    if dumps:
        return max(dumps)[1]
    stats_txt = run_dir / "stats.txt"
    if stats_txt.exists():
        return stats_txt
    raise FileNotFoundError(
        f"No stats_dump_<N>.txt or stats.txt in {run_dir}."
    )


def count_dumps(path: Union[str, Path]) -> int:
    """The number of complete dumps (with an end marker) in a stats file.

    It only scans for the markers, so it is cheap next to `read_dump`.
    """
    count = 0
    with open(path, "r") as stats_file:
        for line in stats_file:
            if line.startswith("-") and END_MARKER in line:
                count += 1
    return count


def dump_locations(run_dir: Union[str, Path]) -> List[Tuple[Path, int]]:
    """Every dump of one run directory, in order, as (file, dump in file).

    These are its `stats_dump_<N>.txt` in order of N (one dump each) when
    there are any, as `find_dump_file` prefers them; otherwise the dumps of
    `stats.txt`.
    A periodically sampled run lists one entry per sample, the last being
    the dump `find_dump_file` and `read_dump` read by default.
    """
    run_dir = Path(run_dir)
    dumps = []
    for path in run_dir.iterdir():
        match = _DUMP_FILE.fullmatch(path.name)
        if match:
            dumps.append((int(match.group(1)), path))
    if dumps:
        return [(path, -1) for _, path in sorted(dumps)]
    stats_txt = run_dir / "stats.txt"
    if stats_txt.exists():
        return [(stats_txt, index) for index in range(count_dumps(stats_txt))]
    raise FileNotFoundError(
        f"No stats_dump_<N>.txt or stats.txt in {run_dir}."
    )


def read_dump(
    path: Union[str, Path], dump: int = -1, cache: bool = True
) -> List[StatLine]:
    """Parse one dump of a gem5 text stats file.

    `dump` selects the dump by position, counting from 0; negative values
    count from the end, so the default reads the last dump.
    Only that dump is kept in memory, and reading stops at it, so a dump
    still being written after it (a running simulation) does no harm.
    Duplicated names are kept and numbered by `StatLine.occurrence`;
    one warning per dump lists them.
    With `cache` (the default), the parsed lines are kept in graphix's cache
    (`graphix.cache`) and read from it while the file is unchanged.
    """
    path = Path(path)
    if cache:
        cached = _cache.load(path, dump)
        if cached is not None:
            _warn_duplicates(path, {l.name for l in cached if l.occurrence})
            return cached
        parsed = read_dump(path, dump, cache=False)
        _cache.store(path, dump, parsed)
        return parsed
    index = dump
    if dump < 0:
        total = count_dumps(path)
        if total == 0:
            raise StatsParseError(path, 0, "", f"no '{BEGIN_MARKER}' marker")
        index = total + dump
    lines = None
    if index >= 0:
        for position, block in enumerate(_split_dumps(path)):
            if position == index:
                lines = block
                break
    if lines is None:
        raise IndexError(
            f"{path} has {count_dumps(path)} dump(s); dump {dump} does not "
            "exist."
        )

    seen = Counter()
    parsed = []
    for lineno, line in lines:
        stat_line = _parse_line(path, lineno, line)
        if stat_line is None:
            continue
        occurrence = seen[stat_line.name]
        seen[stat_line.name] += 1
        if occurrence:
            stat_line = StatLine(
                stat_line.name,
                stat_line.values,
                stat_line.oneline,
                stat_line.description,
                stat_line.unit,
                stat_line.lineno,
                occurrence,
            )
        parsed.append(stat_line)

    _warn_duplicates(path, {name for name, count in seen.items() if count > 1})
    return parsed


def dump_scalars(
    path: Union[str, Path], dump: int, names
) -> dict:
    """The values of a few root stats (`simTicks`, `finalTick`, ...) of one
    dump, found by name without parsing the dump's other lines."""
    path = Path(path)
    index = dump if dump >= 0 else count_dumps(path) + dump
    wanted = {f"{name} ": name for name in names}
    values = {}
    for position, block in enumerate(_split_dumps(path)):
        if position != index:
            continue
        for _, line in block:
            for prefix, name in wanted.items():
                if line.startswith(prefix):
                    try:
                        values[name] = float(line.split()[1])
                    except (IndexError, ValueError):
                        pass
        break
    return values


def _warn_duplicates(path: Path, names) -> None:
    duplicated = sorted(names)
    if duplicated:
        warn(
            f"{path}: {len(duplicated)} stat name(s) appear more than once in "
            f"one dump; they are kept and numbered by 'occurrence'. "
            f"First: {duplicated[0]!r}."
        )


def _split_dumps(path: Path) -> Iterator[List[Tuple[int, str]]]:
    current = None
    lineno = 0
    with open(path, "r") as stats_file:
        for lineno, line in enumerate(stats_file, start=1):
            if line.startswith("-"):
                if BEGIN_MARKER in line:
                    current = []
                    continue
                if END_MARKER in line:
                    if current is None:
                        raise StatsParseError(
                            path, lineno, line, "end marker without a begin"
                        )
                    yield current
                    current = None
                    continue
            if current is not None:
                current.append((lineno, line))
    if current is not None:
        raise StatsParseError(path, lineno, "", "last dump has no end marker")


def _parse_line(path: Path, lineno: int, line: str) -> Optional[StatLine]:
    text = line.strip()
    if not text:
        return None

    text, unit = _strip_unit(text)
    description = None
    marker = text.find(" # ")
    if marker != -1:
        description = text[marker + 3 :].strip() or None
        text = text[:marker].rstrip()
    # Thousands of lines share a description and a unit: one string each
    # keeps memory and the cache small.
    if description is not None:
        description = sys.intern(description)
    if unit is not None:
        unit = sys.intern(unit)

    tokens = text.split()
    name = tokens[0]
    if len(tokens) < 2:
        raise StatsParseError(path, lineno, line, "no value")

    # A one-line value wider than gem5's 12-character field is glued to the
    # bar (`|712623289462`), so test the first character, not the token.
    if tokens[1].startswith("|"):
        segments = text[len(name) :].split("|")[1:]
        try:
            values = tuple(float(seg.split()[0]) for seg in segments)
        except (IndexError, ValueError):
            raise StatsParseError(
                path, lineno, line, "bad one-line value"
            ) from None
        return StatLine(name, values, True, description, unit, lineno)

    try:
        value = float(tokens[1])
    except ValueError:
        raise StatsParseError(path, lineno, line, "bad value") from None
    for extra in tokens[2:]:
        if not extra.endswith("%"):
            raise StatsParseError(
                path, lineno, line, f"unexpected token {extra!r}"
            )
    return StatLine(name, (value,), False, description, unit, lineno)


def _strip_unit(text: str) -> Tuple[str, Optional[str]]:
    """Split a trailing `(unit)` group off a line.

    gem5 wraps the unit string in parentheses, and compound units are
    themselves parenthesised, as in `((Tick/Second))`.
    """
    if not text.endswith(")"):
        return text, None
    # Fast path: a simple unit, `(Count)`, with no parenthesis inside.
    head, separator, unit = text[:-1].rpartition(" (")
    if separator and "(" not in unit and ")" not in unit:
        return head.rstrip(), unit
    depth = 0
    for index in range(len(text) - 1, -1, -1):
        if text[index] == ")":
            depth += 1
        elif text[index] == "(":
            depth -= 1
            if depth == 0:
                break
    else:
        return text, None
    if index == 0 or not text[index - 1].isspace():
        return text, None
    unit = text[index + 1 : -1]
    if unit.startswith("(") and unit.endswith(")"):
        unit = unit[1:-1]
    return text[:index].rstrip(), unit
