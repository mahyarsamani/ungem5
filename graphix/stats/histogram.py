"""Bucketed samples per index: gem5's Histogram and Distribution stats.

graphix reads the buckets (and a Distribution's underflow and overflow) and
keeps one summary, the sum of the sampled values from gem5's `samples` and
`mean` lines, so that the mean stays exact after merges; everything else
(`stdev`, quantiles, ...) it derives from the buckets.
gem5 labels a bucket `low-high` with both ends included, or `low` when the
bucket holds one value (`base/stats/text.cc`, `DistPrint`).
graphix stores a bucket as `[lo, hi)`, so `0-511` becomes `[0, 512)`.
A one-line histogram prints its counts without labels; their edges come from
`bucket_size`, `min_bucket` (0 when absent), and `max_bucket`.
"""

import math
import re
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from warnings import warn

from .base import Stat
from .scalar import Scalar

SUMMARY_FIELDS = {
    "samples", "mean", "gmean", "stdev", "total",
    "min_value", "max_value", "bucket_size", "min_bucket", "max_bucket",
}
_BUCKET_LABEL = re.compile(r"(-?[0-9.]+(?:e[+-]?\d+)?)(?:-(-?[0-9.]+(?:e[+-]?\d+)?))?")


@dataclass(frozen=True)
class Buckets:
    """Counts in half-open buckets `[lo, hi)`, sorted and non-overlapping.

    `exact` is False once a merge or re-bin had to split a bucket between
    target buckets, which assumes samples spread evenly inside a bucket.
    `value_sum` is the sum of the sampled values (gem5's `samples * mean`,
    over- and underflows included), or None when the dump did not print
    both; it survives merges and re-bins, so `mean()` stays exact however
    wide the buckets are.
    """

    buckets: Tuple[Tuple[float, float, float], ...] = ()
    underflow: float = 0.0
    overflow: float = 0.0
    exact: bool = True
    value_sum: Optional[float] = None

    @property
    def samples(self) -> float:
        return math.fsum(c for _, _, c in self.buckets) + self.underflow + self.overflow

    @property
    def mean_exact(self) -> bool:
        """Whether `mean()` comes from gem5's own sum rather than midpoints."""
        return self.value_sum is not None

    def mean(self, exact: bool = True) -> float:
        """The mean of the samples.

        With `exact` (the default) and a known `value_sum`, this is gem5's
        own mean, `value_sum / samples`, which merging keeps exact.
        Otherwise it is the mean of bucket midpoints weighted by count:
        gem5 samples integers, so the midpoint of `[lo, hi)` is
        `(lo + hi - 1) / 2`; under- and overflows are left out, and the
        error grows with the bucket width.
        """
        if exact and self.value_sum is not None:
            samples = self.samples
            return self.value_sum / samples if samples else math.nan
        total = math.fsum(c for _, _, c in self.buckets)
        if not total:
            return math.nan
        return math.fsum(c * _midpoint(lo, hi) for lo, hi, c in self.buckets) / total

    def _total(self, tails: bool) -> float:
        total = math.fsum(c for _, _, c in self.buckets)
        return total + self.underflow + self.overflow if tails else total

    def quantile(self, q: float, tails: bool = True) -> float:
        """The q-quantile, interpolating linearly inside a bucket.

        With `tails` (the default) a Distribution's underflow and overflow
        count as samples, and a quantile that falls in one returns `-inf` or
        `inf`: below or above every bucket, value unknown.
        Without, only the buckets count.
        """
        if not 0 <= q <= 1:
            raise ValueError("q must be within [0, 1].")
        total = self._total(tails)
        if not total:
            return math.nan
        target = q * total
        running = self.underflow if tails else 0.0
        if tails and self.underflow and target <= running:
            return -math.inf
        for lo, hi, count in self.buckets:
            if count and running + count >= target:
                return lo + (hi - lo) * (target - running) / count
            running += count
        if tails and self.overflow:
            return math.inf
        return self.buckets[-1][1] if self.buckets else math.nan

    def pdf(self, tails: bool = True) -> List[float]:
        """Each bucket's share of the samples (of every sample, tails
        included, with `tails`)."""
        total = self._total(tails)
        return [c / total if total else math.nan for _, _, c in self.buckets]

    def cdf(self, tails: bool = True) -> List[float]:
        """The share of samples up to each bucket's end; with `tails`, over
        every sample, so the underflow is counted from the start and the
        last bucket ends below 1 when there is an overflow."""
        total = self._total(tails)
        running, values = (self.underflow if tails else 0.0), []
        for _, _, count in self.buckets:
            running += count
            values.append(running / total if total else math.nan)
        return values

    def width(self) -> float:
        """The widest bucket, which is gem5's bucket size."""
        return max((hi - lo for lo, hi, _ in self.buckets), default=0.0)

    def rebin(self, edges: Sequence[float]) -> "Buckets":
        """Move counts onto new edges, splitting buckets evenly when needed."""
        edges = list(edges)
        counts = [0.0] * (len(edges) - 1)
        underflow, overflow, exact = self.underflow, self.overflow, self.exact
        for lo, hi, count in self.buckets:
            if not count:
                continue
            first = bisect_right(edges, lo) - 1
            last = bisect_right(edges, hi - 1e-9) - 1
            if first < 0 or last >= len(counts):
                if hi <= edges[0]:
                    underflow += count
                    continue
                if lo >= edges[-1]:
                    overflow += count
                    continue
                raise ValueError(f"Bucket [{lo}, {hi}) straddles the new edges' range.")
            if first == last:
                counts[first] += count
                continue
            exact = False
            for target in range(first, last + 1):
                overlap = min(hi, edges[target + 1]) - max(lo, edges[target])
                counts[target] += count * overlap / (hi - lo)
        buckets = tuple(
            (edges[i], edges[i + 1], counts[i]) for i in range(len(counts))
        )
        return Buckets(buckets, underflow, overflow, exact, self.value_sum)


def merge_buckets(parts: Sequence[Buckets]) -> Buckets:
    """Add histograms that may have different bucket sizes.

    The result uses the widest bucket size and starts at the lowest edge.
    gem5 grows a histogram's bucket size by doubling from the same start, so
    the narrower grids nest inside the wider one and the merge is exact;
    otherwise buckets are split and the result is marked inexact.
    """
    parts = [p for p in parts if p.buckets or p.underflow or p.overflow]
    if not parts:
        return Buckets()
    underflow = math.fsum(p.underflow for p in parts)
    overflow = math.fsum(p.overflow for p in parts)
    # The sum of values adds up exactly; it is unknown as soon as one part
    # lacks it.
    value_sum = (
        math.fsum(p.value_sum for p in parts)
        if all(p.value_sum is not None for p in parts)
        else None
    )
    with_buckets = [p for p in parts if p.buckets]
    if not with_buckets:
        return Buckets((), underflow, overflow, all(p.exact for p in parts), value_sum)
    width = max(p.width() for p in with_buckets)
    start = min(p.buckets[0][0] for p in with_buckets)
    end = max(p.buckets[-1][1] for p in with_buckets)
    count = max(1, math.ceil((end - start) / width))
    edges = [start + i * width for i in range(count + 1)]
    total = [0.0] * count
    exact = all(p.exact for p in parts)
    for part in with_buckets:
        rebinned = part.rebin(edges)
        exact = exact and rebinned.exact
        for i, (_, _, c) in enumerate(rebinned.buckets):
            total[i] += c
        underflow += rebinned.underflow - part.underflow
        overflow += rebinned.overflow - part.overflow
    buckets = tuple((edges[i], edges[i + 1], total[i]) for i in range(count))
    if not exact:
        warn("Merged histograms with non-nesting buckets; counts were split evenly.")
    return Buckets(buckets, underflow, overflow, exact, value_sum)


def _midpoint(lo: float, hi: float) -> float:
    return (lo + hi - 1) / 2 if hi - lo >= 1 else (lo + hi) / 2


def buckets_from_block(block) -> Buckets:
    fields = block.fields
    underflow = fields.get("underflows", (0.0,))[0]
    overflow = fields.get("overflows", (0.0,))[0]
    buckets: List[Tuple[float, float, float]] = []
    if None in fields:
        size = fields.get("bucket_size", (1.0,))[0]
        low = fields.get("min_bucket", (0.0,))[0]
        high = fields.get("max_bucket", (math.inf,))[0]
        for position, count in enumerate(fields[None]):
            lo = low + position * size
            hi = min(lo + size - 1, high) + 1
            buckets.append((lo, hi, count))
    for name, values in fields.items():
        if name is None or name in SUMMARY_FIELDS or name in ("underflows", "overflows"):
            continue
        match = _BUCKET_LABEL.fullmatch(name)
        if match is None:
            raise ValueError(
                f"'{block.name}' at '{block.owner}': unknown histogram field '{name}'."
            )
        lo = float(match.group(1))
        hi = float(match.group(2)) if match.group(2) is not None else lo
        buckets.append((lo, hi + 1, values[0]))
    buckets.sort()
    # gem5 prints the mean over every sample, overflows included, with six
    # decimals; samples * mean recovers the sum of values closely enough to
    # keep the mean exact to that precision.
    value_sum = None
    if "samples" in fields and "mean" in fields:
        samples, mean = fields["samples"][0], fields["mean"][0]
        if not (math.isnan(samples) or math.isnan(mean)):
            value_sum = samples * mean
    return Buckets(tuple(buckets), underflow, overflow, True, value_sum)


class Histogram(Stat):
    """Buckets per index; summaries are derived, never read from gem5."""

    kind = "histogram"
    plots = ("cdf", "pdf", "box", "ridge")
    reductions = ("merge", "sum")
    zero = Buckets()

    @classmethod
    def value_from_block(cls, block) -> Buckets:
        return buckets_from_block(block)

    def _reduce_values(self, how, values, weights):
        return merge_buckets(values)

    def merge(self, over="owner", label: Optional[str] = None) -> "Histogram":
        """Add the buckets of values that differ only in `over`."""
        return self.reduce("merge", over=over, label=label)

    def _scalar(self, name: str, function, unit: Optional[str]) -> Scalar:
        data = OrderedDict((index, function(value)) for index, value in self.items())
        return Scalar(f"{name}({self.name})", data, unit, self.description)

    def samples(self) -> Scalar:
        return self._scalar("samples", lambda b: b.samples, "Count")

    def mean(self, exact: bool = True) -> Scalar:
        """The mean per index: gem5's own when the dump printed it and
        `exact` is set, else the estimate from bucket midpoints."""
        return self._scalar("mean", lambda b: b.mean(exact), self.unit)

    def quantile(self, q: float, tails: bool = True) -> Scalar:
        """The q-quantile per index; `-inf` or `inf` when it falls in the
        underflow or overflow (see `Buckets.quantile`)."""
        return self._scalar(f"q{q:g}", lambda b: b.quantile(q, tails), self.unit)

    def scale(self, factor: float, unit: Optional[str] = None) -> "Histogram":
        """Multiply every bucket edge (and the exact sum of values) by
        `factor`, for example ticks to cycles with `1 / ticks_per_cycle`;
        counts stay as they are."""
        data = OrderedDict()
        for index, value in self.items():
            data[index] = Buckets(
                tuple((lo * factor, hi * factor, c) for lo, hi, c in value.buckets),
                value.underflow,
                value.overflow,
                value.exact,
                None if value.value_sum is None else value.value_sum * factor,
            )
        return self._new(data, unit=unit)

    def drop_empty(self) -> "Histogram":
        """Drop the indexes whose histogram has no sample."""
        data = OrderedDict((i, v) for i, v in self.items() if v.samples)
        return self._new(data)

    def rebin(self, edges: Sequence[float]) -> "Histogram":
        data = OrderedDict((index, value.rebin(edges)) for index, value in self.items())
        return self._new(data)

    def rows(self, kind: str = "buckets", tails: bool = False) -> List[Dict[str, Any]]:
        columns = self._index_columns()
        rows = []
        for index, value in self.items():
            base = self._index_row(index, columns)
            base["stat"] = self.name
            if kind == "summary":
                row = dict(base)
                row.update(
                    samples=value.samples,
                    mean=value.mean(),
                    mean_exact=value.mean_exact,
                    p50=value.quantile(0.5),
                    p90=value.quantile(0.9),
                    p99=value.quantile(0.99),
                    underflow=value.underflow,
                    overflow=value.overflow,
                    exact=value.exact,
                )
                rows.append(row)
                continue
            if kind != "buckets":
                raise ValueError("kind must be 'buckets' or 'summary'.")
            samples = value.samples
            share = lambda count: count / samples if samples else math.nan  # noqa: E731
            if tails and value.underflow:
                first = value.buckets[0][0] if value.buckets else math.inf
                row = dict(base)
                row.update(lo=-math.inf, hi=first, count=value.underflow,
                           pdf=share(value.underflow), cdf=share(value.underflow))
                rows.append(row)
            for (lo, hi, count), pdf, cdf in zip(value.buckets, value.pdf(), value.cdf()):
                row = dict(base)
                row.update(lo=lo, hi=hi, count=count, pdf=pdf, cdf=cdf)
                rows.append(row)
            if tails and value.overflow:
                last = value.buckets[-1][1] if value.buckets else -math.inf
                row = dict(base)
                row.update(lo=last, hi=math.inf, count=value.overflow,
                           pdf=share(value.overflow), cdf=1.0)
                rows.append(row)
        return rows

    def value_columns(self, kind: str = "buckets", tails: bool = False) -> List[str]:
        if kind == "summary":
            return ["stat", "samples", "mean", "mean_exact", "p50", "p90", "p99",
                    "underflow", "overflow", "exact"]
        return ["stat", "lo", "hi", "count", "pdf", "cdf"]

    def to_frame(self, kind: str = "buckets", tails: bool = False):
        """`kind="buckets"`: lo, hi, count, pdf, cdf per bucket, where pdf
        and cdf are shares of every sample (a Distribution's underflow and
        overflow included); `tails=True` adds the underflow and overflow as
        rows with an infinite edge.
        `kind="summary"`: samples, mean (and whether it is gem5's exact
        one, `mean_exact`), and quantiles over every sample per index (inf
        when one falls in a tail)."""
        return super().to_frame(kind=kind, tails=tails)
