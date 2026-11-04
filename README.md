# graphix

graphix reads gem5's text statistics into typed stats that you can select, reduce, and export to pandas.
It knows how stock gem5 prints its stats, including Ruby's, and you teach it about your own stats with specs instead of editing the package.

Start with the tutorial in `examples/sift/`: six notebooks that use the SIFT profiling runs in `example_data/`.

## Install

graphix needs Python 3.10 or later and pandas.
Either put this directory on `sys.path`, as the tutorial does, or install it in editable mode:

```
pip install -e tools/graphix
```

## Use

```python
import graphix as gx

runs = gx.RunSet.from_dirs(
    "example_data/*/*",                              # run directories (with config.json)
    labels=my_labels,                                # run directory -> label dict
    owners=gx.OwnerScheme.from_json("owners.json"),  # optional canonical owner names
    specs=MY_SPECS,                                  # optional specs for your own stats
)
runs.catalog()                     # every stat name, kind, owner class, and unit
ipc = runs["commitStats0.numInsts"] / runs["numCycles"]
l1d = runs["usefulBytes"].select(owner="tile*.l1d.cache").merge()
l1d.to_frame()                     # one row per bucket, with pdf and cdf
```

## How it works

1. `parse.read_dump` reads one dump of a stats file: the `stats_dump_<N>.txt` with the largest N, or the last dump of `stats.txt`.
   A run that dumps its stats periodically (samples) has one dump per sample, listed in order by `parse.dump_locations`; `RunSet.from_samples(paths)` loads each as a run labelled `sample` (0, 1, ...) and `sim_seconds` (the dump's `simSeconds`), so a stat over those labels is a time series.
   `RunSet.from_dirs` keeps reading only the last dump, which is the whole region of interest when the samples are cumulative.
2. `config.ConfigTree` reads `config.json`; `names.split_name` splits each stat name into its owner SimObject, a key, and a `::` field.
3. `specs.Registry` matches each key against stat specs (yours first, then `gem5/specs.py`), which name the stat family, extract dims, choose the class, or skip the line.
4. `records.build_blocks` groups the lines of each stat instance into a block and infers its kind.
5. `run.RunSet` builds a stat across runs on request: it fills values that gem5 dropped as zero, applies the owner scheme, and converts dims.
6. The stat classes in `stats/` hold the values and define the operations that suit them.

| class | gem5 stats | operations | plot kinds |
|---|---|---|---|
| `Scalar` | Scalar, Value, Average | arithmetic with alignment and units, reductions, `relative_to` | bar, grouped_bar, line, normalized_bar, scatter |
| `Vector` | Vector, Vector2d rows (the row becomes a dim) | element-wise arithmetic, `element`, `total`, `share`, `matrix` | stacked_bar, share_bar, grouped_bar, heatmap, pie |
| `Histogram` | Histogram, Distribution | `merge`, `rebin`, `samples`, `mean`, `quantile` | cdf, pdf, box, ridge |
| `ControllerCount` | Ruby's `<Machine>_Controller.<event>` and `.<state>.<event>` | as `Scalar`, plus `matrix` (state by event) | heatmap, stacked_bar, bar |

graphix does not read gem5 Formula stats, and of a histogram's summary lines it keeps only the sum of values (`samples` times `mean`), so that `mean()` stays exact across merges however wide the buckets are; `stdev`, `total`, and quantiles it derives from the buckets.
Plotting is left to the user: every class exports a tidy DataFrame and lists the plot kinds that suit it.

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
- `nozero` stats vanish when zero; graphix fills them back in as zero.
- One name can mean different stats on different objects (`lookups` on a branch predictor, its BTB, and its indirect predictor); graphix raises `AmbiguousStatError` and you pick one with `get(name, owner_class=...)`.

## Tests

```
.venv/x86_64/bin/python -m unittest discover -s tools/graphix/tests
```

The tests use small handmade fixtures in `tests/fixtures/`.
When `example_data/` exists, they also parse every example run and execute every tutorial notebook.
After editing a notebook source in `examples/sift/src/`, rebuild the notebooks with `python make_notebooks.py`.
