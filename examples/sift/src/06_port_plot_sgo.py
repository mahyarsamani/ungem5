# %% [markdown]
# # 6. Porting `plot_sgo.ipynb`
#
# `plot_sgo_original.ipynb` (next to this notebook, outputs cleared) drew the L1D useful-byte CDFs with the old graphix API.
# This notebook redraws them with the new one.
#
# | old code | new code |
# |---|---|
# | `Node`, `parse_config_json`, `parse_stats_txt(index, root, path, -1)` | `RunSet.from_dirs(paths, labels=...)` |
# | parsing `constructor.py` lines by position | a label function (`sift_labels`) |
# | `search_children(root, {"dcache": r"...dcache.cache$"})` | `select(owner="tile*.l1d.cache")` with an `OwnerScheme` |
# | `stat["usefulBytes_<array>"]` | `runs["usefulBytes"].select(accessor=...)` with `TUTORIAL_SPECS` |
# | `filter_parents(these_parents=...)` then `aggregate_using(CombineAggregator())` | `select(...)` then `merge()` |
# | `get_buckets_for_parent` and `get_freqs_for_parent` | `to_frame()` with `lo`, `hi`, `count`, `pdf`, `cdf` |
#
# Two changes matter for the numbers:
#
# - the old notebook picked the latest `stats_dump_<N>.txt` itself; graphix now always does;
# - a bucket for one byte count `n` used to be drawn at `n + 0.5`; here the bucket is `[n, n + 1)`, and the CDF steps at `n`, so `CDF(n)` is the fraction of blocks with at most `n` used bytes.

# %%
import sys
from pathlib import Path

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path.cwd().resolve().parents[1]))
import graphix as gx
from sift_labels import sift_labels
from tutorial_specs import TUTORIAL_SPECS

runs = gx.RunSet.from_dirs(
    "../../example_data/*/*",
    labels=sift_labels,
    owners=gx.OwnerScheme.from_json("owners.json"),
    specs=TUTORIAL_SPECS,
)
useful = runs["usefulBytes"].select(owner="tile*.l1d.cache")

# %% [markdown]
# ## Useful bytes per data structure
#
# The first figure of the old notebook drew one CDF per array for the first run.
# Here the arrays are the accessor labels of one UME run, merged over the eight L1D caches.

# %%
first = useful.select(sweep="baseline", input="input=blake,region=gradzatz_invert")
per_array = first.merge(over="owner").drop(accessor=["other", "prefetch"])
accessors = per_array.dims("accessor")

nrows = -(-len(accessors) // 3)  # ceiling division
fig, axes = plt.subplots(nrows=nrows, ncols=3, figsize=(12, 3 * nrows), sharey=True, squeeze=False)
for ax, accessor in zip(axes.flat, accessors):
    rows = per_array.select(accessor=accessor).to_frame()
    ax.step(rows["lo"], rows["cdf"], where="post")
    ax.set_title(f"data structure: {accessor}")
    ax.set_xlim(0, 65)
for ax in axes.flat[len(accessors):]:
    ax.set_visible(False)
fig.tight_layout()
plt.show()

# %% [markdown]
# ## L1D useful bytes per run
#
# The second figure drew one CDF per run of a sweep, over every accessor, with a red line just past the block size.
# `merge(over=["owner", "accessor"])` pools all L1D caches and accessors of a run, and the block size comes from each run's `config.json`.

# %%
no_pf = useful.select(sweep="no_pf").merge(over=["owner", "accessor"])
frame = no_pf.to_frame()
groups = list(frame.groupby(["workload", "input"]))

fig, axes = plt.subplots(nrows=1, ncols=len(groups), figsize=(4 * len(groups), 3.5), sharey=True)
for ax, ((workload, run_input), rows) in zip(axes, groups):
    run = runs.select(sweep="no_pf", workload=workload, input=run_input).runs[0]
    line_size = run.tree.root.board.cache_line_size
    ax.step(rows["lo"], rows["cdf"], where="post")
    ax.axvline(line_size + 1, color="red", linestyle="--")
    ax.set_xlim(0, 65)
    ax.set_xticks(range(0, 65, 8))
    ax.set_title(f"{workload.upper()}, {run_input}", fontsize=8)
axes[0].set_ylabel("Cumulative frequency")
fig.text(0.5, -0.02, "Bytes of the cache block that were accessed", ha="center")
plt.show()

# %% [markdown]
# ## The same numbers as a table
#
# The figure above is also a single scalar per run: cache block utilization, from the `UsefulBytes` class of notebook 5.

# %%
no_pf.cbu().to_frame()[["workload", "input", "value"]].round(3)
