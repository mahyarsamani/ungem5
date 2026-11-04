"""DataFrames from several stats at once."""

from typing import List

from .stats import Scalar, Stat


def concat(*stats: Stat, **kwargs):
    """Stack the long exports of several stats (one `stat` column)."""
    import pandas as pd

    frames = [stat.to_frame(**kwargs) for stat in stats]
    return pd.concat(frames, ignore_index=True, sort=False)


def table(*stats: Scalar):
    """One column per Scalar, one row per shared index.

    Use it for scatter plots and for formulas pandas should compute:
    `table(misses, accesses).eval("misses / accesses")`.
    Column names are the stats' names; rename stats first to shorten them.
    """
    import pandas as pd

    series: List = []
    for stat in stats:
        if not isinstance(stat, Scalar):
            raise TypeError(f"table() takes Scalars; '{stat.name}' is a {type(stat).__name__}.")
        series.append(stat.to_series())
    return pd.concat(series, axis=1, join="inner")
