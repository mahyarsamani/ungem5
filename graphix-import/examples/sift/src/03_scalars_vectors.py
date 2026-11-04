# %% [markdown]
# # 3. Scalars, vectors, and Ruby controller counts
#
# This notebook loads all runs with the labels from notebook 1 and the owner scheme from notebook 2.
# It then computes with scalars, reads vectors, and looks at Ruby's per-controller counts.

# %%
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path.cwd().resolve().parents[1]))
import graphix as gx
from sift_labels import sift_labels

runs = gx.RunSet.from_dirs(
    "../../example_data/*/*",
    labels=sift_labels,
    owners=gx.OwnerScheme.from_json("owners.json"),
)

# %% [markdown]
# ## Arithmetic
#
# Scalars support `+ - * / // % **`, with numbers on either side.
# Two stats pair up by index: same run, owner, dims, and occurrence.
# The result carries a generated name and a derived unit.

# %%
insts = runs["commitStats0.numInsts"]
cycles = runs["numCycles"]
ipc = (insts / cycles).rename("ipc")
print(ipc.describe())
ipc.select(sweep="baseline", workload="ume").to_frame().head()

# %% [markdown]
# Division by zero gives `inf` or `nan`, never an exception.
#
# When the right-hand stat has exactly one value per run, it applies to every index of that run.
# `simInsts` belongs to the root, so it divides every cache's misses at once.

# %%
mpki = (runs["m_demand_misses"].select(owner="tile*.l1d.cache") / runs["simInsts"] * 1000).rename("l1d_mpki")
mpki.select(sweep="baseline").to_frame(wide=True).iloc[:, :4]

# %% [markdown]
# Operators require the two index sets to be equal; a mismatch is an error that says which indexes differ.
# The methods `add`, `sub`, `mul`, and `div` take `how="inner"` to keep the shared indexes instead.
# Units are checked too: adding a `Count` to a `Cycle` is an error.

# %%
core0 = insts.select(owner="core0")
try:
    core0 / cycles
except ValueError as error:
    print(error)
len(core0.div(cycles, how="inner"))

# %% [markdown]
# ## Reductions
#
# `reduce(how, over=...)` combines the values that differ only in `over`: the owner (default), a dim, a label, or `occurrence`.
# Reducing over owners gives the result a new owner name, so per-owner and reduced values can share one table.

# %%
run_ipc = (insts.reduce("sum") / cycles.reduce("max")).rename("ipc")
run_ipc.to_frame().head(3)

# %% [markdown]
# `sum` warns when the unit is a ratio or a rate, where `mean` or `weighted_mean(weights=...)` is usually right.
#
# ## Comparing sweeps
#
# Every sweep ran UME blake `gradzatz` and HPCG with `dim=64` on one process.
# `relative_to(sweep="baseline")` divides each value by the run that matches it in every other label, with `sweep` set to `baseline`.
# The `run` label is unique per directory, so drop it first.

# %%
matched = run_ipc.select(np=1, input=["input=blake,region=gradzatz", "dim=64"]).drop_labels("run")
speedup = matched.relative_to(sweep="baseline")
table = speedup.to_frame().pivot_table(index="workload", columns="sweep", values="value")
table

# %% [markdown]
# Each stat class lists the plot kinds it suits; a spec can change them per family.
# Plotting stays in your hands: export, then draw.

# %%
print(speedup.plot_kinds)
ax = table.plot.bar(figsize=(8, 3), rot=0)
ax.axhline(1.0, color="black", linewidth=0.8)
ax.set_ylabel("IPC relative to baseline")
plt.show()

# %% [markdown]
# `gx.table(...)` joins several scalars into one wide DataFrame, which suits scatter plots.

# %%
l1d_misses = runs["m_demand_misses"].select(owner="tile*.l1d.cache").reduce("sum").rename("l1d_misses")
points = gx.table(l1d_misses, cycles.reduce("max").rename("cycles"))
points.plot.scatter(x="l1d_misses", y="cycles", figsize=(4, 3))
plt.show()

# %% [markdown]
# ## Vectors
#
# A vector holds named elements per index.
# Elements that gem5 left out (zero under `nozero`) read as zero.

# %%
fu_busy = runs["statFuBusy"].select(sweep="baseline", owner="core0")
print(fu_busy.describe())
fu_busy.matrix(rows=["workload", "input"]).loc[:, lambda frame: frame.sum() > 0]

# %% [markdown]
# `element(name)` and `total()` return scalars, and `share()` divides each element by its total.
#
# gem5's Vector2d stats print each row as `<name>_<row>::<element>`.
# graphix turns the row into a dim, so the branch predictor's per-thread `lookups_0` becomes `lookups` with `thread=0`.
# The spec also names the element column: `branch_type`.
#
# That name is shared, though: the BTB prints a `lookups` vector and the indirect predictor a `lookups` scalar.
# graphix refuses to merge different stats under one name, and says which C++ classes print each one.

# %%
try:
    runs["lookups"]
except gx.AmbiguousStatError as error:
    print(error)

# %% [markdown]
# `get(name, owner_class=...)` picks one family by the owners' C++ class (a glob or a list of globs).
# The baseline sweep uses a bimodal predictor and the other sweeps TAGE.

# %%
lookups = runs.get("lookups", owner_class=["*BiModeBP", "*TAGE_SC_L_64KB"])
lookups = lookups.select(sweep="baseline", workload="ume")
print(lookups.describe())
shares = lookups.reduce("sum").share()
shares.to_frame()[["input", "thread", "branch_type", "value"]].head(8)

# %% [markdown]
# ## Ruby controller counts
#
# Ruby prints each controller type's event and transition counts once, under `ruby_system`, as vectors with one element per controller.
# graphix moves each element to its controller using the controller's `version` from `config.json`.
# The result is a `ControllerCount`: one number per controller, with `event` (and `state`) as dims.

# %%
events = runs["Cache_Controller.events"].select(sweep="baseline", workload="ume")
print(events.describe())
events.select(owner="tile*.l1d", event=["Load", "Store"]).reduce("sum").to_frame()[["input", "owner", "event", "value"]]

# %% [markdown]
# `matrix()` turns transition counts into a state-by-event table, for one controller or summed over a selection.

# %%
transitions = runs["Cache_Controller.transitions"].select(sweep="baseline", input="input=blake,region=gradzatz")
counts = transitions.matrix(owner="tile0.cluster0.l1d")
counts = counts.loc[counts.sum(axis=1) > 0, counts.sum(axis=0) > 0]
fig, ax = plt.subplots(figsize=(10, 3))
ax.imshow(np.log10(counts.clip(lower=1)), aspect="auto")
ax.set_yticks(range(len(counts.index)), counts.index)
ax.set_xticks(range(len(counts.columns)), counts.columns, rotation=90, fontsize=6)
ax.set_title("tile0.cluster0.l1d transitions (log10 count)")
plt.show()
