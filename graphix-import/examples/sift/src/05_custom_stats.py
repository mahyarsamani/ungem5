# %% [markdown]
# # 5. Teaching graphix about your own stats
#
# graphix ships specs for stock gem5 only.
# SIFT adds its own stats to gem5, and this notebook teaches graphix about them without touching the package:
#
# 1. find stats whose names carry a dimension;
# 2. write a `StatSpec` for them;
# 3. subclass a stat class to add methods that only make sense for your stat;
# 4. use a converter that looks things up in the run's config.
#
# `tutorial_specs.py` and `useful_bytes.py` hold the finished versions.
# They are frozen examples: a project keeps its real specs in its own tree (SIFT's live in the SIFT project, `analysis/sift_stats/`), never in graphix.

# %%
import inspect
import sys
from pathlib import Path

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path.cwd().resolve().parents[1]))
import graphix as gx
from sift_labels import sift_labels

DATA = "../../example_data/*/*"
OWNERS = gx.OwnerScheme.from_json("owners.json")

# %% [markdown]
# ## 1. Without specs
#
# SIFT's caches count the used bytes of every evicted block per accessor label: `usefulBytes.<accessor>`.
# Without a spec, every accessor is a separate stat.

# %%
plain = gx.RunSet.from_dirs(DATA, labels=sift_labels, owners=OWNERS)
plain.catalog("usefulBytes*")[["name", "kind", "owner_class", "blocks"]]

# %% [markdown]
# `suggest_specs()` lists name families that differ only in their last token and share one description, which usually means a dimension.

# %%
for suggestion in plain.suggest_specs():
    print(suggestion)

# %% [markdown]
# ## 2. A spec
#
# A `StatSpec` matches the key: the part of a stat's name after its owner and before `::`.
# The pattern `usefulBytes.{accessor}` matches every accessor, names the family `usefulBytes`, and makes `accessor` a dim.
# `owner_class` limits the spec to the objects that print the stat, and `kind` says which class to build.

# %%
spec = gx.StatSpec(
    "usefulBytes.{accessor}",
    owner_class="gem5::ruby::CacheMemory",
    kind="histogram",
    unit="Byte",
)
runs = gx.RunSet.from_dirs(DATA, labels=sift_labels, owners=OWNERS, specs=[spec])
useful = runs["usefulBytes"]
print(useful.describe())
useful.dims("accessor")

# %% [markdown]
# Patterns have two placeholders:
#
# - `{x}` matches characters without a dot, and may sit inside a token: `indRel{role}AccLat.{relation}`;
# - `{x...}` matches the rest, dots included.
#
# A compiled regex with named groups works too, with `name="{group}"` to build the family name from a group.
# Captures that the name does not use become dims, and `dims={"x": converter}` converts them.
#
# ## 3. A subclass
#
# Some methods only make sense for one stat.
# Cache block utilization, the mean of used bytes over the block size, is one of them.
# `UsefulBytes` subclasses `Histogram` and adds it.

# %%
import useful_bytes

print(inspect.getsource(useful_bytes.UsefulBytes))

# %% [markdown]
# Passing the class as the spec's `kind` makes graphix build it.
# `TUTORIAL_SPECS` does that for all four useful-byte stats (`usefulBytes`, `readUsefulBytes`, `writeUsefulBytes`, `residentUsefulBytes`).

# %%
from tutorial_specs import TUTORIAL_SPECS

runs = gx.RunSet.from_dirs(DATA, labels=sift_labels, owners=OWNERS, specs=TUTORIAL_SPECS)
useful = runs["usefulBytes"]
type(useful).__name__, useful.plot_kinds

# %%
l1d = useful.select(owner="tile*.l1d.cache", np=1, input=["dim=64", "input=blake,region=gradzatz"])
cbu = l1d.merge(over=["owner", "accessor"]).cbu(line_size=64)
cbu.drop_labels("run").to_frame().pivot_table(index="sweep", columns="workload", values="value")

# %% [markdown]
# Per accessor, the same data shows which arrays waste most of each block.

# %%
by_accessor = useful.select(owner="tile*.l1d.cache", sweep="baseline", workload="ume").merge(over="owner")
by_accessor.cbu().to_frame().pivot_table(index="accessor", columns="input", values="value").round(2)

# %% [markdown]
# ## 4. A converter that reads the run
#
# `bytesTransferred` names its destination by gem5's MachineID, such as `Cache_12`: a controller type and version.
# A converter with two parameters receives the `Run` as well, so it can turn the version into that controller's owner name.

# %%
print(inspect.getsource(sys.modules["tutorial_specs"].controller_owner))
transfers = runs["bytesTransferred"]
print(transfers.describe())

# %%
from_l1d = transfers.select(owner="tile*.l1d.cache", sweep="baseline", workload="ume")
frame = from_l1d.to_frame()
frame.groupby(["label", "dst"])["value"].sum().sort_values(ascending=False).head(8)

# %% [markdown]
# The runs in `example_data/` predate the current name, `bytesTransferred.<type>.<accessor>.<dst>`.
# `TUTORIAL_SPECS` holds a spec for each form, and both build the same family.
#
# ## Several dims from one token
#
# The indirect-relation latencies name a relation as `<consumer>[<producer>]<instance>`.
# A regex spec splits it into three dims, and turns the role prefix (`Prod`, `Cons`, or none) into a fourth.

# %%
relations = runs["indRelAccLat"]
print(relations.describe())
summary = relations.select(sweep="baseline").merge(over="owner").to_frame("summary")
summary[["input", "role", "consumer", "producer", "instance", "samples", "p50", "p99"]].head(8)

# %% [markdown]
# These histograms have 64 buckets that grow with the largest sample, up to 16384 ticks wide here.
# A quantile interpolates inside its bucket, so treat medians as estimates at that resolution.

# %%
one_run = relations.select(sweep="baseline", input="input=blake,region=gradzatz_invert")
medians = one_run.merge(over="owner").quantile(0.5).to_frame()
table = medians.pivot_table(index=["consumer", "producer", "instance"], columns="role", values="value")
ax = table.plot.barh(figsize=(6, 3))
ax.set_xlabel(f"median latency ({relations.unit})")
plt.show()

# %% [markdown]
# ## Order and overrides
#
# User specs always come before graphix's built-in specs, so a user spec can override a built-in one.
# `Registry(specs, builtins=False)` uses only your specs, which helps when you want to see a stat exactly as gem5 printed it.
