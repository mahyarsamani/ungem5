"""What a job asks for and what a host offers: cores and memory."""

import re

from dataclasses import dataclass

_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:I?B)?\s*$", re.I)


def parse_size(size: "int | float | str | None") -> int | None:
    """Bytes in `size`: an int is bytes, a string like "16G", "512MiB" or
    "1.5T" uses binary units. None stays None."""
    if size is None:
        return None
    if isinstance(size, bool):
        raise TypeError(f"not a size: {size!r}")
    if isinstance(size, (int, float)):
        if size < 0:
            raise ValueError(f"a size can't be negative: {size!r}")
        return int(size)
    match = _SIZE.match(str(size))
    if match is None:
        raise ValueError(f"not a size: {size!r} (try e.g. 16G or 512M)")
    number, unit = match.groups()
    return int(float(number) * _UNITS[unit.upper()])


def format_size(size: int | None) -> str:
    """"12.5G", "800M"; "—" for None."""
    if size is None:
        return "—"
    for unit in ("T", "G", "M", "K"):
        if size >= _UNITS[unit]:
            value = size / _UNITS[unit]
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
    return f"{size}B"


@dataclass(frozen=True)
class Resources:
    """What a job needs while it runs.

    :param cores: cores it keeps busy; 1 for a gem5 simulation, which is
        single-threaded.
    :param memory: your guess of its peak memory, in bytes or as "16G".
        The scheduler checks it against what similar jobs actually used
        and reserves its own estimate; see `experiment.api.memory`.
    """

    cores: int = 1
    memory: int | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cores, int) or self.cores < 1:
            raise ValueError(f"cores must be a positive int, not {self.cores!r}")
        # NOTE: Frozen, so normalize through object.__setattr__.
        object.__setattr__(self, "memory", parse_size(self.memory))

    @classmethod
    def of(cls, resources: "Resources | None") -> "Resources":
        if resources is None:
            return cls()
        if not isinstance(resources, Resources):
            raise TypeError(
                f"jobs take Resources(cores, memory) or None, not {resources!r}"
            )
        return resources

    def to_dict(self) -> dict:
        return {"cores": self.cores, "memory": self.memory}

    def __str__(self) -> str:
        return f"{self.cores} core(s), {format_size(self.memory)}"
