"""Quantities gem5 prints as Formulas, rebuilt from their counters.

graphix skips gem5's Formula stats (`gem5/specs.py`); these functions compute
the common ones from the counters they are made of, aligned by index, with a
counter gem5 left out (zero under `nozero`) read as zero.
Each takes a `RunSet` and an optional owner glob, and returns a `Scalar`.
"""

from typing import Optional

from ..stats import Scalar

RUBY_CACHE = "gem5::ruby::CacheMemory"
DRAM = "gem5::memory::DRAMInterface"


def _counter(runs, name: str, owner_class: str, owner: Optional[str]) -> Scalar:
    stat = runs.get(name, owner_class=owner_class)
    stat = stat.select(owner=owner) if owner else stat
    # gem5 prints Ruby's counters without a unit; they are counts.
    return stat.with_unit("Count")


def demand_accesses(runs, owner: Optional[str] = None) -> Scalar:
    """A Ruby cache's demand accesses: `m_demand_hits + m_demand_misses`
    (gem5's `m_demand_accesses`)."""
    hits = _counter(runs, "m_demand_hits", RUBY_CACHE, owner)
    misses = _counter(runs, "m_demand_misses", RUBY_CACHE, owner)
    return hits.add(misses, how="outer").rename("demand_accesses")


def demand_hit_ratio(runs, owner: Optional[str] = None) -> Scalar:
    """A Ruby cache's demand hits over its demand accesses (NaN with none)."""
    hits = _counter(runs, "m_demand_hits", RUBY_CACHE, owner)
    accesses = demand_accesses(runs, owner)
    return hits.div(accesses, how="outer").rename("demand_hit_ratio")


def dram_read_row_hit_rate(runs, owner: Optional[str] = None) -> Scalar:
    """A DRAM interface's read row-buffer hits over its read bursts (gem5's
    `readRowHitRate`, as a ratio rather than a percentage)."""
    hits = _counter(runs, "readRowHits", DRAM, owner)
    bursts = _counter(runs, "readBursts", DRAM, owner)
    return hits.div(bursts, how="outer").rename("dram_read_row_hit_rate")
