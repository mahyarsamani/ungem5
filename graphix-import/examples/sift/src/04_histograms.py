# %% [markdown]
# # 4. Histograms
#
# gem5's Histogram and Distribution stats become `Histogram` objects: buckets per index.
# graphix reads only the buckets (and a Distribution's underflow and overflow).
# Of gem5's summary lines it keeps only the sum of values (`samples` times `mean`), so the mean stays exact after merges; `stdev` and the quantiles it derives from the buckets.

# %%
import sys
from pathlib import Path

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path.cwd().resolve().parents[1]))
import graphix as gx
from sift_labels import sift_labels

runs = gx.RunSet.from_dirs(
    "../../example_data/*/*",
    labels=sift_labels,
    owners=gx.OwnerScheme.from_json("owners.json"),
)

# %% [markdown]
# ## Buckets
#
# A bucket is stored half-open: gem5's `0-511` becomes `[0, 512)`.
# Ruby's transaction-latency histograms print all their counts on one line; graphix rebuilds the edges from `bucket_size` and `max_bucket`.
# The event is a dim, taken from the name `outTransLatHist.<event>`.

# %%
latency = runs["outTransLatHist"]
print(latency.describe())
print(latency.dims("event"))

# %%
l1d_reads = latency.select(owner="tile*.l1d", event="SendReadShared")
l1d_reads.select(sweep="baseline", owner="tile0.cluster0.l1d").to_frame()[["input", "lo", "hi", "count", "cdf"]]

# %% [markdown]
# ## Merging
#
# `merge(over=...)` adds the buckets of values that differ only in `over`; the default is the owner.
# gem5 grows a histogram's bucket size by doubling, so two caches of one run can have different bucket sizes.
# Their grids nest, which keeps the merge exact; graphix re-bins onto the widest bucket size and marks any merge that had to split a bucket.

# %%
widths = {}
for index, buckets in l1d_reads:
    widths.setdefault((index.label("sweep"), index.label("input")), set()).add(buckets.width())
{run: sorted(sizes) for run, sizes in widths.items() if len(sizes) > 1}

# %%
merged = l1d_reads.merge()
merged.to_frame("summary")[["sweep", "workload", "input", "owner", "samples", "mean", "p50", "p90", "p99", "exact"]].head(8)

# %% [markdown]
# `samples()`, `mean()`, and `quantile(q)` return scalars, so they work with everything from notebook 3.
# The mean is gem5's own (`mean_exact` in the summary frame) when the dump printed `samples` and `mean`; `mean(exact=False)` gives the estimate from bucket midpoints, whose error grows with the bucket size.
# Quantiles always interpolate inside a bucket, so they are estimates at the bucket resolution.

# %%
p90 = merged.quantile(0.9).select(np=1, input="dim=64").drop_labels("run")
p90.relative_to(sweep="baseline").to_frame()[["sweep", "value"]]

# %% [markdown]
# ## Plotting a CDF
#
# The bucket export has `pdf` and `cdf` columns, ready to plot.
# Ruby's transaction histograms are too coarse for a useful CDF here: nearly every ReadShared falls in the first 512-cycle bucket.
# The load-to-use distribution of the O3 load-store queue has 10-cycle buckets.
# Joining `(lo of the first bucket, 0)` to each `(hi, cdf)` draws the same linear interpolation that `quantile` uses.

# %%
load_to_use = runs["lsq0.loadToUse"].select(np=1, input="dim=64").merge()
fig, ax = plt.subplots(figsize=(6, 3))
for sweep, rows in load_to_use.to_frame().groupby("sweep"):
    ax.plot([rows["lo"].iloc[0], *rows["hi"]], [0, *rows["cdf"]], label=sweep)
ax.set_xlim(0, 60)
ax.set_xlabel("load-to-use latency")
ax.set_ylabel("CDF")
ax.legend(fontsize=7)
plt.show()

# %% [markdown]
# ## Distributions
#
# A Distribution has fixed buckets and counts what falls outside them as underflow and overflow.
# `numCommittedDist` counts instructions committed per cycle, one bucket per count.

# %%
committed = runs["commit.numCommittedDist"].select(sweep="baseline", workload="ume", owner="core0")
rows = committed.to_frame()
fig, ax = plt.subplots(figsize=(6, 3))
for run, group in rows.groupby("input"):
    ax.plot(group["lo"], group["pdf"], marker="o", label=run)
ax.set_xlabel("instructions committed in a cycle")
ax.set_ylabel("fraction of cycles")
ax.legend(fontsize=7)
plt.show()

# %% [markdown]
# `rebin(edges)` moves counts onto new edges, for example to compare histograms on one grid.

# %%
coarse = committed.rebin([0, 1, 4, 9])
coarse.to_frame()[["input", "lo", "hi", "count"]]

# %% [markdown]
# Histograms have no arithmetic: adding two latency histograms has no single meaning.
# Use `merge` to pool samples, or compare derived scalars such as `mean()` and `quantile()`.
