# ungem5

In a processor, the uncore is everything around the cores: the caches, the interconnect, the memory controllers.
ungem5 plays the same role for gem5: it is everything architecture research needs that is not the simulator.
It sets up a gem5 project, builds gem5, runs simulations by hand or as jobs on a lab of hosts, and reads the statistics those runs print into typed stats that export to pandas.
It merges two earlier packages: `experiment`, the `helper` CLI and its scheduler, and `graphix`, the typed gem5 statistics.

## Install

ungem5 needs Python 3.12 or later.

```bash
pip install -e ".[stats]"            # the CLI, the scheduler, and the stats (pandas)
pip install -e ".[dev,stats,model]"  # plus pytest and the learned memory model (scikit-learn)
```

The `stats` extra adds pandas, which only the analysis side needs, so a worker can be installed without it.
The `model` extra gives the scheduler a learned memory estimate.
Install the same version on every host: workers, the scheduler and consoles exchange compiled scripts and must agree.

## The `helper` command

| Command | What it does |
|---|---|
| `helper initialize` | Writes `project_config.json` and copies `scripts/util/decorators.py` into a project. |
| `helper build <name> ...` | Builds gem5 for the nearest project into its binary directory. |
| `helper run <name> <script> -- <args>` | Runs one simulation in the foreground, optionally under gdb. |
| `helper work --name <host>` | Runs a worker that the scheduler launches jobs on. |
| `helper schedule` | Starts (or attaches to) the scheduler and opens its console. |
| `helper console` | Reattaches a console to the running scheduler. |
| `helper certs init` / `issue` | Manages the certificates for mutual TLS between scheduler and workers. |
| `helper fetch <experiment\|job>... --into DIR [--match GLOB] [--lab]` | Pulls finished jobs' result files through the running scheduler into `DIR/<experiment>/<job>/` (the last two parts of each job's outdir), moving only files whose sha256 changed and keeping their mtimes; by default the files `ungem5.results` and `ungem5.stats` read, and `--match` adds more. |

`helper` finds `project_config.json` by walking up from the current directory.
A load script defines `Host`s and `Experiment`s; the console compiles it into plain data, so the scheduler never runs user code.
Every job writes its records to `<outdir>/.job/` (`launch.json`, `exit.json`, `index.json`, and others), so a worker can restart while its jobs run.
Lab deployment, with root workers and one lab scheduler, is in `deploy/README.md`.

## Statistics

`ungem5.stats` reads gem5's text statistics into typed stats that you can select, reduce, and export to pandas.
It knows how stock gem5 prints its stats, including Ruby's, and you teach it about your own stats with specs instead of editing the package.
The tutorial in `examples/sift/` is six notebooks that use SIFT profiling runs in `example_data/`, which git does not track.

```python
import ungem5.stats as gx

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

`RunSet.from_experiment` keeps the jobs that exited cleanly and left `simulation_finished` (`ungem5.results.jobs.job_status`), and labels each run with `experiment` and its `params.json`.
`RunSet.from_dirs(paths, ...)` does the same for plain run directories (a list or a glob), with no job status.
On a machine without the outdir, `helper fetch <experiment> --into DIR` first, then `RunSet.from_experiment(DIR/<experiment>)` reads the copy as it reads the outdir.

### How the stats are built

1. `parse.read_dump` reads one dump of a stats file: the `stats_dump_<N>.txt` with the largest N, or the last dump of `stats.txt`.
   `Run.dumps()` lists a run's dumps in order, and `from_dirs(..., dump=k)` reads the k-th instead.
   `RunSet.from_samples(paths)` loads each dump of a periodically sampled run as a run labelled `sample` and `sim_seconds`, so a stat over those labels is a time series.
   `Run.resets()` lists the dumps that follow a stats reset (`simTicks` falling while `finalTick` rises), so a cumulative last dump can be checked rather than assumed.
2. `config.ConfigTree` reads `config.json`; `names.split_name` splits each stat name into its owner SimObject, a key, and a `::` field.
3. `specs.Registry` matches each key against stat specs (yours first, then `gem5/specs.py`), which name the stat family, extract dims, choose the class, or skip the line.
4. `records.build_blocks` groups the lines of each stat instance into a block and infers its kind.
5. `run.RunSet` builds a stat across runs on request: it fills values that gem5 dropped as zero, applies the owner scheme, and converts dims.
6. The stat classes in `stats/` hold the values and define the operations that suit them.

| Class | gem5 stats | Operations | Plot kinds |
|---|---|---|---|
| `Scalar` | Scalar, Value, Average | arithmetic with alignment and units, reductions, `relative_to` | bar, grouped_bar, line, normalized_bar, scatter |
| `Vector` | Vector, Vector2d rows (the row becomes a dim) | element-wise arithmetic, `element`, `total`, `share`, `matrix` | stacked_bar, share_bar, grouped_bar, heatmap, pie |
| `Histogram` | Histogram, Distribution | `merge`, `rebin`, `scale`, `samples`, `mean`, `quantile`, `drop_empty` | cdf, pdf, box, ridge |
| `ControllerCount` | Ruby's `<Machine>_Controller.<event>` and `.<state>.<event>` | as `Scalar`, plus `matrix` (state by event) | heatmap, stacked_bar, bar |

The stats core does not read gem5 Formula stats; `ungem5.stats.gem5.derived` rebuilds the common ones from their counters (demand accesses, demand hit ratio, DRAM read row-hit rate).
Of a histogram's summary lines it keeps only the sum of values, so that `mean()` stays exact across merges however wide the buckets are; `stdev`, `total`, and quantiles come from the buckets.
Plotting is left to the user: every class exports a tidy DataFrame and lists the plot kinds that suit it, and an empty export keeps its columns.

Semantics worth knowing:
- Quantiles, `pdf` and `cdf` count a Distribution's underflow and overflow as samples (`tails=False` for the buckets alone); a quantile that falls in a tail is `-inf` or `inf`, and `to_frame(tails=True)` adds the tails as rows.
- Arithmetic aligns strictly by default; `add`, `sub`, `mul` and `div` take `how="inner"` or `how="outer"` with a `fill_value` (zero by default) for counters gem5 left out.
- `scale(factor, unit)` changes units (ticks to cycles with `Run.ticks_per_cycle()`).
- A sum warns only for a known ratio or rate; gem5's `Unspecified` counters add up silently.

### Loading speed

A 20 to 30 MB dump parses in about 0.3 s and builds into stats in about 0.15 s.
`ungem5.stats.cache` keeps each dump's parsed lines (zlib-compressed, about 1.3 MB) under `~/.cache/ungem5/stats`, keyed by the file's path, size and modification time.
A rerun reads them in about 0.1 s, and a changed file (a running simulation) is parsed again.
Specs and owner schemes apply after the cache, so changing them never needs a re-parse.
`cache=False` or `UNGEM5_CACHE=off` bypasses the cache, `ungem5.stats.cache.clear()` empties it, and `workers=N` parses the dumps it misses in N processes.

### Extending the stats

- **A stat with dims in its name**: write a `StatSpec`, for example `StatSpec("usefulBytes.{accessor}", owner_class="gem5::ruby::CacheMemory", kind="histogram")`; `RunSet.suggest_specs()` lists candidates.
- **Methods for one stat**: subclass a stat class and pass the subclass as the spec's `kind`.
- **A dim that needs the run's config**: give the spec a converter with two parameters, `(value, run)`.
- **Different SimObject names for the same hardware**: an `OwnerScheme`.

### gem5 quirks the stats core handles

- `config.json` zero-pads the names of SimObject vectors with ten or more elements (`system_links01`), but stats do not (`system_links1`).
- A one-line value wider than 12 characters is glued to the bar (`|712623289462`).
- Unnamed stats groups merge into their parent, so one controller can print the same name several times; the core numbers them with `occurrence`.
- `nozero` stats vanish when zero; the core fills them back in as zero for every owner of the same class, but never fills a histogram into a run that printed none of it.
- One name can mean different stats on different objects (`lookups` on a branch predictor, its BTB, and its indirect predictor); the core raises `AmbiguousStatError`, and `get(name, owner_class=...)` picks one.

## Three layers

| Layer | Knows | Lives |
|---|---|---|
| Core | gem5's text format, `config.json`, stock gem5 stats and their specs, the stat classes, owner schemes as a mechanism | `ungem5/stats/` |
| Run source | how `helper` jobs lay out their directories: status from `.job/exit.json` and `simulation_finished`, labels from `params.json` | `ungem5/results/` |
| End user | a project's own stats, stat classes, owner names, labels and completion rules | the project (SIFT: `projects/sift/analysis/sift_stats/` of the PhD repo) |

Nothing in `ungem5/` names a project, so a new project stat changes only the project.
`examples/sift/tutorial_specs.py` and `examples/sift/useful_bytes.py` are the tutorial's frozen worked examples, not SIFT's live specs.

## Layout

| Package | Holds |
|---|---|
| `ungem5/cli/` | the `helper` subcommands |
| `ungem5/jobs/` | jobs, experiments, gem5 jobs and builds, load scripts, bundles, resources, project and service settings, the marker names, and the files `helper initialize` copies |
| `ungem5/schedule/` | the scheduler, its daemon, console and dashboard, hosts, workers, the runner, per-user file access, and memory history and model |
| `ungem5/run/` | the build and run backends shared by `helper build`, `helper run` and jobs |
| `ungem5/stats/` | the statistics core |
| `ungem5/results/` | which job directories finished and what labels they carry |
| `ungem5/common/` | logging, commands, and the certificate authority |

## Files and names on disk

| What | Where |
|---|---|
| Service settings (root's) | `/etc/ungem5/config.toml`, or `$UNGEM5_CONFIG` |
| A user's `[lab]` settings | `~/.config/ungem5/config.toml` |
| Certificates | `~/.config/ungem5/pki/`, or `$UNGEM5_PKI_DIR` |
| Scheduler state and logs | `~/.local/state/ungem5/<host>/<name>/` |
| Scheduler lock and socket | `/tmp/ungem5-<uid>/`, or `$UNGEM5_RUNTIME_DIR` |
| A worker's bundle store | `~/.cache/ungem5/store/<host>/<user>/` |
| The parsed-dump cache | `~/.cache/ungem5/stats/`, or `$UNGEM5_CACHE` |
| A root worker's jobs | the systemd slice `ungem5-<owner>.slice` |
| The lab deployment | `/opt/ungem5/venv`, `/etc/ungem5/pki`, `/var/lib/ungem5`, `/run/ungem5`, `deploy/ungem5-{scheduler,worker}.service` |

A job's command finds the worker's Python in `$UNGEM5_PYTHON`.

## Tests

```bash
pytest
```

The stats tests use small handmade fixtures in `tests/fixtures/` and a temporary cache (`tests/gxtest.py`), never `~/.cache/ungem5/stats`.
When `example_data/` exists, they also parse every example run and execute every tutorial notebook.
After editing a notebook source in `examples/sift/src/`, rebuild the notebooks with `python make_notebooks.py` from `examples/sift/`.
