"""A user stat class: useful-byte histograms with cache block utilization.

SIFT's caches sample, for every block they evict, how many of its bytes the
core used (`usefulBytes.<accessor>` in `CacheMemory`, one bucket per byte
count from 0 to the block size).
`UsefulBytes` is a `Histogram` with two methods that only make sense for
that stat; graphix itself knows nothing about it.

This copy is the tutorial's frozen example; SIFT's own lives in the SIFT
project, `analysis/sift_stats/useful_bytes.py`.
"""

import math
from collections import OrderedDict

import ungem5.stats as gx


class UsefulBytes(gx.Histogram):
    """Histogram of used bytes per evicted cache block."""

    plots = ("cdf", "pdf")

    def cbu(self, line_size: int = 64) -> gx.Scalar:
        """Cache block utilization: mean used bytes over the block size."""
        data = OrderedDict(
            (index, buckets.mean() / line_size) for index, buckets in self.items()
        )
        return gx.Scalar(f"cbu({self.name})", data, "Ratio", self.description)

    def fully_used(self, line_size: int = 64) -> gx.Scalar:
        """The fraction of evicted blocks whose every byte was used."""
        data = OrderedDict()
        for index, buckets in self.items():
            full = sum(c for lo, hi, c in buckets.buckets if lo <= line_size < hi)
            total = buckets.samples
            data[index] = full / total if total else math.nan
        return gx.Scalar(f"fully_used({self.name})", data, "Ratio", self.description)
