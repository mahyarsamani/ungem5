# graphix

graphix reads gem5's text statistics into typed stats that you can select, reduce, and export to pandas.
It knows how stock gem5 prints its stats, including Ruby's, and you teach it about your own stats with specs instead of editing the package.

Start with the tutorial in `examples/sift/`: six notebooks that use the SIFT profiling runs in `example_data/`.

## Install

graphix needs Python 3.10 or later and pandas.
Install it in editable mode (the PhD venv has it):

```
pip install -e tools/graphix
```

## Three layers

| Layer | Knows | Lives |
|---|---|---|
| Core | gem5's text format, `config.json`, stock gem5 stats and their specs, the stat classes, owner schemes as a mechanism | `graphix/` |
| Run sources | how a tool lays out its run directories: `graphix.sources.helper` for `tools/experiment` (job status from `.job/exit.json` and `simulation_finished`, labels from `params.json`) | `graphix/sources/` |
| End user | a project's own stats, stat classes, owner names, labels and completion rules | the project (SIFT: `projects/sift/analysis/sift_stats/` of the PhD repo) |

Nothing in `graphix/` names a project, and a new project stat changes only the project.
The helper source imports experiment's own file names (`experiment.api.runner`), so it moves into experiment unchanged when graphix does.

## Use

```python
import graphix as gx

runs = gx.RunSet.from_experiment(
    ["<out>/se-v5-baseline-v5", "<out>/se-v5-hov-v5"],  # experiment directories
    done=my_done,                    # optional project completion rule (job dir -> bool)
    labels=my_labels,                # run directory -> label dict, on top of params.json
    owners=MY_OWNERS,                # optional canonical owner names (an OwnerScheme)
    specs=MY_SPECS,                  # optional specs for your own stats
    workers=8,                       # parse cache misses in 8 processes
)
runs.catalog()                     # every stat name, kind, owner class, and unit
ipc = runs["commitStats0.numInsts"] / runs["numCycles"]
l1d = runs["usefulBytes"].select(owner="tile*.l1d.cache").merge()
l1d.to_frame()                     # one row per bucket, with pdf and cdf
```

`RunSet.from_dirs(paths, ...)` does the same for plain run directories (a list or a glob), with no job status.

## Loading speed

A 20 to 30 MB dump parses in about 0.3 s and builds into stats in about 0.15 s.
`graphix.cache` keeps each dump's parsed lines (zlib-compressed, about 1.3 MB, under `~/.cache/graphix` or `$GRAPHIX_CACHE`), keyed by the file's path, size and modification time, so a rerun reads them in about 0.1 s and a changed file (a running simulation) is parsed again.
Specs and owner schemes apply after the cache, so changing them never needs a re-parse.
`cache=False` or `GRAPHIX_CACHE=off` bypasses it; `graphix.cache.clear()` empties it.
`workers=N` parses the dumps the cache misses in N processes.

## How it works

1. `parse.read_dump` reads one dump of a stats file: the `stats_dump_<N>.txt` with the largest N, or the last dump of `stats.txt`.
   `Run.dumps()` lists a run's dumps in order, and `from_dirs(..., dump=k)` reads the k-th instead of the last.
   A run that dumps its stats periodically (samples) has one dump per sample; `RunSet.from_samples(paths)` loads each as a run labelled `sample` (0, 1, ...) and `sim_seconds` (the dump's `simSeconds`), so a stat over those labels is a time series.
   The last dump is the whole region of interest when the samples are cumulative; `Run.resets()` lists the dumps that follow a stats reset (`simTicks` falling while `finalTick` rises), so that can be checked rather than assumed.
2. `config.ConfigTree` reads `config.json`; `names.split_name` splits each stat name into its owner SimObject, a key, and a `::` field.
3. `specs.Registry` matches each key against stat specs (yours first, then `gem5/specs.py`), which name the stat family, extract dims, choose the class, or skip the line.
4. `records.build_blocks` groups the lines of each stat instance into a block and infers its kind.
5. `run.RunSet` builds a stat across runs on request: it fills values that gem5 dropped as zero, applies the owner scheme, and converts dims.
6. The stat classes in `stats/` hold the values and define the operations that suit them.

| class | gem5 stats | operations | plot kinds |
|---|---|---|---|
| `Scalar` | Scalar, Value, Average | arithmetic with alignment and units, reductions, `relative_to` | bar, grouped_bar, line, normalized_bar, scatter |
| `Vector` | Vector, Vector2d rows (the row becomes a dim) | element-wise arithmetic, `element`, `total`, `share`, `matrix` | stacked_bar, share_bar, grouped_bar, heatmap, pie |
| `Histogram` | Histogram, Distribution | `merge`, `rebin`, `scale`, `samples`, `mean`, `quantile`, `drop_empty` | cdf, pdf, box, ridge |
| `ControllerCount` | Ruby's `<Machine>_Controller.<event>` and `.<state>.<event>` | as `Scalar`, plus `matrix` (state by event) | heatmap, stacked_bar, bar |

graphix does not read gem5 Formula stats, and of a histogram's summary lines it keeps only the sum of values (`samples` times `mean`), so that `mean()` stays exact across merges however wide the buckets are; `stdev`, `total`, and quantiles it derives from the buckets.
`graphix.gem5.derived` rebuilds the common Formulas from their counters (demand accesses, demand hit ratio, DRAM read row-hit rate).
Plotting is left to the user: every class exports a tidy DataFrame and lists the plot kinds that suit it; an empty export keeps its columns.

Semantics worth knowing:
- Quantiles, `pdf` and `cdf` count a Distribution's underflow and overflow as samples (`tails=False` for the buckets alone); a quantile that falls in a tail is `-inf` or `inf`, and `to_frame(tails=True)` adds the tails as rows.
- Arithmetic aligns strictly by default; `add`, `sub`, `mul` and `div` take `how="inner"` or `how="outer"` with a `fill_value` (zero by default) for counters gem5 left out.
- `scale(factor, unit)` changes units (ticks to cycles with `Run.ticks_per_cycle()`).
- A sum warns only for a known ratio or rate; gem5's `Unspecified` counters add up silently.

## Extend

- **A stat with dims in its name**: write a `StatSpec`, for example `StatSpec("usefulBytes.{accessor}", owner_class="gem5::ruby::CacheMemory", kind="histogram")`.
  `RunSet.suggest_specs()` lists candidates.
- **Methods for one stat**: subclass a stat class and pass the subclass as the spec's `kind`.
- **A dim that needs the run's config**: give the spec a converter with two parameters, `(value, run)`.
- **Different SimObject names for the same hardware**: an `OwnerScheme`.

A project's specs, stat classes and owner schemes live in the project, never in graphix: graphix knows stock gem5 only, and a new project stat changes only the project.
`examples/sift/tutorial_specs.py` and `examples/sift/useful_bytes.py` are the tutorial's frozen worked examples; SIFT's real ones are a package in the SIFT project (`analysis/sift_stats/`).

## Gem5 quirks graphix handles

- `config.json` zero-pads the names of SimObject vectors with ten or more elements (`system_links01`), but stats do not (`system_links1`).
- A one-line value wider than 12 characters is glued to the bar (`|712623289462`).
- Unnamed stats groups merge into their parent, so one controller can print the same name several times; graphix numbers them with `occurrence`.
- `nozero` stats vanish when zero; graphix fills them back in as zero for every owner of the same class (but never fills a histogram into a run that printed none of it).
- One name can mean different stats on different objects (`lookups` on a branch predictor, its BTB, and its indirect predictor); graphix raises `AmbiguousStatError` and you pick one with `get(name, owner_class=...)`.

## Tests

```
.venv/x86_64/bin/python -m unittest discover -s tools/graphix/tests
```

The tests use small handmade fixtures in `tests/fixtures/` and a temporary cache (`tests/gxtest.py`), never `~/.cache/graphix`.
When `example_data/` exists, they also parse every example run and execute every tutorial notebook.
After editing a notebook source in `examples/sift/src/`, rebuild the notebooks with `python make_notebooks.py`.
