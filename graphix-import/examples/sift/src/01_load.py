# %% [markdown]
# # 1. Loading runs
#
# graphix turns gem5's text statistics into typed stats that you can select, reduce, and export to pandas.
# This tutorial uses the SIFT profiling runs from January 2026, and each notebook builds on the previous one:
#
# 1. loading runs and seeing what graphix found (this notebook);
# 2. giving owners consistent names across configurations;
# 3. scalars, vectors, and Ruby controller counts;
# 4. histograms;
# 5. teaching graphix about your own stats;
# 6. porting the old `plot_sgo.ipynb`.
#
# Run the notebooks from this directory (`examples/sift/`).

# %%
import sys
from pathlib import Path

# graphix lives two directories up; put it on the path instead of installing it.
sys.path.insert(0, str(Path.cwd().resolve().parents[1]))
import graphix as gx

DATA = "../../example_data/*/*"

# %% [markdown]
# ## The data
#
# `example_data/` holds 23 runs from six sweeps (`profiling`, `profiling_no_pf`, `profiling_imp`, ...).
# Each run directory has three files:
#
# - `config.json`: the SimObject tree that gem5 wrote;
# - `constructor.py`: the `helper` call that created the job;
# - `stats_dump_<N>.txt`: one statistics dump.
#
# graphix reads only the `stats_dump_<N>.txt` with the largest N.
# Without any, it reads the last dump in `stats.txt`.

# %%
example = sorted(Path("../../example_data/profiling").iterdir())[0]
print(sorted(p.name for p in example.iterdir()))
print((example / "constructor.py").read_text())

# %% [markdown]
# ## Labels
#
# A run is known by its labels, a dict such as `{"sweep": "no_pf", "workload": "hpcg"}`.
# You supply them as a function from the run directory to that dict.
# graphix adds one label of its own, `run`: the directory name, which keeps every run unique.
#
# The function below is the smallest useful one: the sweep from the parent directory and the workload from `constructor.py`.

# %%
import ast


def tiny_labels(run_dir):
    call = ast.parse((run_dir / "constructor.py").read_text()).body[0].value
    workload = next(arg.value for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
    return {"sweep": run_dir.parent.name, "workload": workload}


tiny_labels(example)

# %% [markdown]
# `sift_labels.py` holds the version the rest of the tutorial uses.
# It also turns the workload's arguments into an `input` label and adds the process count `np`.

# %%
from sift_labels import sift_labels

runs = gx.RunSet.from_dirs(DATA, labels=sift_labels)
runs

# %%
runs.labels().sort_values(["workload", "input", "sweep"]).head(10)

# %% [markdown]
# `RunSet.select` keeps the runs whose labels match, which is handy to load once and work on a subset.

# %%
runs.select(workload="hpcg", np=1).labels()[["sweep", "input"]]

# %% [markdown]
# ## What graphix found
#
# `catalog()` lists every stat name with its kind, unit, dims, and how many blocks (stat instances) the runs hold.
# There are four kinds:
#
# | kind | gem5 stats | graphix class |
# |---|---|---|
# | `scalar` | Scalar, Value, Average | `Scalar` |
# | `vector` | Vector, Vector2d rows | `Vector` |
# | `histogram` | Histogram, Distribution | `Histogram` |
# | `ruby_controller` | Ruby's `<Machine>_Controller.<event>` and `.<state>.<event>` | `ControllerCount` |

# %%
catalog = runs.catalog()
catalog["kind"].value_counts()

# %% [markdown]
# A glob narrows the catalog.

# %%
runs.catalog("*Latency*")

# %% [markdown]
# Some lines are skipped on purpose.
# gem5 Formula stats (`ipc`, bandwidths, rates) are recomputable from the counters, so graphix does not read them.
# Stats that print only summaries, such as `mem_ctrl.avgPriority_*`, are skipped too.

# %%
runs.skipped()

# %% [markdown]
# ## A first stat
#
# Indexing a `RunSet` by name builds that stat across every run.
# `describe()` says what it is and what you can do with it.

# %%
cycles = runs["numCycles"]
print(cycles.describe())

# %% [markdown]
# Every number sits under an index with four parts:
#
# - the run's labels;
# - `owner`, the SimObject that printed it (its raw path until notebook 2 adds canonical names);
# - dims, values taken from the stat name (none for `numCycles`);
# - `occurrence`, which numbers names gem5 printed more than once.
#
# `to_frame()` exports one row per number.

# %%
cycles.to_frame().head()

# %% [markdown]
# `to_frame(wide=True)` puts runs in rows and owners in columns.

# %%
cycles.select(sweep="baseline").to_frame(wide=True)

# %% [markdown]
# ## Values that gem5 did not print
#
# gem5 drops a stat flagged `nozero` when its value is zero, so the set of printed lines changes from run to run.
# graphix fills those gaps with zero for every owner of the same C++ class in the same run.
# `get(name, fill=False)` returns only what gem5 printed.
#
# The `no_pf` sweep has no prefetcher, for example, so its runs print no `m_prefetch_hits` at all.
# Without the fill, a mean over runs would quietly leave them out.

# %%
printed = runs.get("m_prefetch_hits", fill=False)
filled = runs["m_prefetch_hits"]
len(printed), len(filled)

# %% [markdown]
# ## Names that gem5 printed twice
#
# Ruby merges every unnamed `TBEStorage` group into its controller, so one L1 controller of these runs prints `avg_size` five times.
# graphix keeps them all and numbers them with `occurrence`.

# %%
runs["avg_size"].select(
    owner="*core_tiles0.core_clusters0.dcache", sweep="baseline", workload="ume"
).to_frame()[["run", "owner", "occurrence", "value"]]
