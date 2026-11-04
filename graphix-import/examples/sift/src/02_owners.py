# %% [markdown]
# # 2. Owner names
#
# Notebook 1 used raw SimObject paths as owners, such as `board.cache_hierarchy.core_tiles0.core_clusters0.dcache`.
# Raw paths are long, and different configurations can name the same hardware differently.
# An `OwnerScheme` maps raw paths to short canonical names that stay the same across configurations.

# %%
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd().resolve().parents[1]))
import graphix as gx
from sift_labels import sift_labels

# %% [markdown]
# ## Two naming conventions
#
# All example sweeps name the private caches `dcache`, `icache`, and `l2cache`.
# `make_renamed_run.py` copies one baseline run into `data/profiling_renamed/` and renames them to `l1dcache`, `l1icache`, and `l2`.
# It stands in for a configuration written by someone else.

# %%
from make_renamed_run import make_renamed_run

renamed = make_renamed_run()
paths = sorted(Path("../../example_data/profiling").iterdir())[:1] + [renamed]
runs = gx.RunSet.from_dirs(paths, labels=sift_labels)
runs.labels()[["sweep", "workload", "input"]]

# %% [markdown]
# Without a scheme, the L1 data caches of the two runs are different owners, so nothing lines up across them.

# %%
misses = runs["m_demand_misses"]
misses.select(owner="*core_tiles0.core_clusters0.*d*cache.cache").owners()

# %% [markdown]
# ## A scheme
#
# A scheme maps each canonical name template to the raw path templates that mean the same thing.
#
# - `{x}` captures a run of characters without a dot, so it can sit inside a token (`core_tiles{t}`).
# - A raw template matches a whole path or a prefix of it, and the longest match wins.
# - With `keep_suffix=True` (the default), the rest of the path is kept: `...dcache.cache` becomes `tile0.cluster0.l1d.cache`.
# - A capture that the canonical name does not use becomes a dim, so no information is lost.

# %%
scheme = gx.OwnerScheme(
    {
        "tile{t}.cluster{c}.l1d": [
            "board.cache_hierarchy.core_tiles{t}.core_clusters{c}.dcache",
            "board.cache_hierarchy.core_tiles{t}.core_clusters{c}.l1dcache",
        ],
    }
)
print(scheme.map("board.cache_hierarchy.core_tiles2.core_clusters1.dcache"))
print(scheme.map("board.cache_hierarchy.core_tiles2.core_clusters1.l1dcache.cache"))
print(scheme.map("board.processor.cores0.core"))  # not covered: kept as is

# %%
by_cluster = gx.OwnerScheme(
    {"cluster{c}.l1d": ["board.cache_hierarchy.core_tiles{t}.core_clusters{c}.dcache"]}
)
by_cluster.map("board.cache_hierarchy.core_tiles2.core_clusters1.dcache.cache")

# %% [markdown]
# `owners.json` holds the scheme for the whole SIFT hierarchy: L1s, L2s, cores, system-cache slices, directories, and memory controllers.
# Saving a scheme as JSON lets every notebook of a study share it.

# %%
print(Path("owners.json").read_text())

# %%
scheme = gx.OwnerScheme.from_json("owners.json")
runs = gx.RunSet.from_dirs(paths, labels=sift_labels, owners=scheme)
misses = runs["m_demand_misses"]
misses.select(owner="tile0.cluster*.l1d.cache").to_frame()[["sweep", "owner", "path", "value"]]

# %% [markdown]
# Both runs now report the same owners, and the `path` column keeps the raw name.
# The two runs are the same simulation, so the values match too.
#
# ## Selecting owners
#
# `select(owner=...)` takes a glob, or a regex wrapped in `re.compile`.
# `select(path=...)` filters on the raw path instead.

# %%
l1d = misses.select(owner=re.compile(r"tile[01]\.cluster0\.l1d\.cache"))
l1d.owners()

# %%
misses.select(path="*l1dcache*").labels("sweep")

# %% [markdown]
# Every other keyword of `select` names a label or a dim, and takes a value, a list of values, or a predicate.

# %%
len(misses.select(sweep=["baseline", "renamed"], owner="slc*"))

# %% [markdown]
# ## Collisions are errors
#
# Two raw paths of one run must never share a canonical owner, because their values would silently merge.
# graphix checks every SimObject of every run when it loads, and names both paths.

# %%
clash = gx.OwnerScheme(
    {
        "l1": [
            "board.cache_hierarchy.core_tiles0.core_clusters0.l1dcache",
            "board.cache_hierarchy.core_tiles0.core_clusters1.l1dcache",
        ]
    }
)
try:
    gx.RunSet.from_dirs([renamed], owners=clash)
except ValueError as error:
    print(error)
