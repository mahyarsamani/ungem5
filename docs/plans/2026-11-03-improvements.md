# graphix improvement plan

Date: 2026-11-03.
Status: phases 1 to 4 implemented on `redesign` the same day (see "Outcome" at the end); the project side waits for the author's review.

## Why

graphix is now what every SIFT analysis reads stats with, and the notes across the PhD tree show where it falls short.
Three problems recur.

- **SIFT lives inside graphix.**
  Every new SIFT stat needs a commit to the graphix repo: all four wave-1 agent branches (`agent/wp0`, `wp2`, `wp3`, `wp4`) change only `examples/sift/sift_specs.py`, and every analysis script imports that file from graphix's examples folder as if it were production code.
- **Every analysis script rebuilds the same scaffolding.**
  `motivation_v3.py`, `perf_v3.py`, `reduce_v3.py`, and WP4's `eval_common.py` each find `helper` runs, decide which finished, label them from `params.json`, pick the last dump, and cache per-run pickles in a process pool, because loading is slow: about 1.4 s per v3 run (0.5 to 1.0 s parsing a 22 to 33 MB dump, 0.4 to 0.5 s building blocks), so 3 minutes for the 130-run campaign and 10 minutes for `runtime.py`'s multi-dump reads.
- **A few semantics force workarounds.**
  Quantiles and CDFs leave out a Distribution's overflow (`motivation_v3.py` reimplements both), histograms cannot change units (scripts divide `lo` and `hi` by the clock), arithmetic cannot outer-join (`se_v2_baseline.py` merges hits and misses by hand), unitless gem5 counters trigger the ratio warning, a histogram one variant never prints comes back all zero with NaN CDFs, empty results lose their columns, and nothing tells you whether the last dump follows a stats reset.

## Principle: three layers

| Layer | What it knows | Where it lives |
|---|---|---|
| Core | gem5: the text format, `config.json`, stock gem5 stats and their specs, the stat classes, owner schemes as a mechanism | `tools/graphix/graphix/` |
| Run sources | how a tool lays out run directories: plain directories (today), and `helper`'s layout (`params.json`, `window.json`, `.job/exit.json`) | `tools/graphix/graphix/sources/`, reading files only, importing nothing from `tools/experiment` |
| End user | a project's own stats, stat classes, owner names, and labels: the SIFT specs, `UsefulBytes`, `owners.json`, the label functions | the project, `projects/sift/analysis/sift_stats/` |

Rules that follow:
- Nothing in `graphix/` names SIFT, `hov`, or a SIFT stat; the built-in specs stay stock gem5 (`graphix/gem5/specs.py`).
- A new SIFT stat changes `projects/sift` only; graphix changes only when gem5 itself or the stat model changes.
- The tutorial keeps a small, frozen example (a handful of SIFT specs and `UsefulBytes`), marked as an example, so graphix never imports a project.

## Phase 1: move SIFT out of graphix

1. Create `projects/sift/analysis/sift_stats/` (a package): `specs.py` (today's `sift_specs.py`, with `SIFT_SPECS`), `useful_bytes.py`, `owners.json`, and `labels.py` (labels from `params.json`, replacing `sift_labels.py`'s `constructor.py` parsing).
2. Add the missing owner mapping `board.processor.main{n}.core` to `core{n}` (open in `documents/original/thesis/notes/analysis/sift-topdown.md` and `notes/review/B2-sift.md`).
3. Move the four agent branches' spec commits into the project (their content, not their graphix commits), and close graphix PRs #1 to #3 and the local `agent/wp2` branch as superseded.
4. Shrink `examples/sift/` to the tutorial's frozen copy (`tutorial_specs.py`, `useful_bytes.py`, `owners.json`), with a header saying the real specs live in the project.
5. Point every importer at the new package: `analysis/{motivation_v3,sharing_v3,runtime/runtime,wp0_target_flow/compare}.py`, `analysis/perf_v3/perf_v3.py`, `analysis/speedup_v3/{reduce_v3,speedup_v3}.py`, `analysis/se_v1_v2/*.py`, `analysis/se_v2_baseline/se_v2_baseline.py`, and the thesis's `workbench/plots/sift_topdown.py` (the author's file: a proposed diff only).
6. Install graphix in the PhD venv (`pip install -e tools/graphix`, both architectures) so scripts stop inserting two `sys.path` entries; the project package goes on the path from `projects/sift/analysis`, where the scripts already live.
7. Update the living rules: `.claude/skills/visualization-rules/references/gem5-viz.md` (where specs go, imports), and the agent brief template's "every new stat gets a spec in `tools/graphix/examples/sift`" line (`projects/sift/docs/agents/briefs/wave1-common.md` and later briefs).

Check: every script's outputs are identical before and after (section "Verification").

## Phase 2: loading speed and run sources

1. **Measure first**: profile `read_dump` and `build_blocks` on three v3 runs, and fix the hot paths (the per-line regex and string splitting) before adding a cache.
2. **Parsed-dump cache** in the core: the `StatLine`s of a dump, pickled under `~/.cache/graphix/`, keyed by the file's path, size, modification time, and graphix's parser version; spec-independent, so a changed spec never needs a re-parse.
   Opt out with `cache=False`; `graphix.cache.clear()` empties it.
3. **Parallel loading**: `RunSet.from_dirs(..., workers=N)` parses and builds runs in a process pool (stat building stays in the parent; it takes milliseconds).
4. **Dumps as a public API**: `Run.dumps()` lists a run's dumps (today's internal `dump_locations`, used by `runtime.py`), `from_dirs(..., dump=-1 | k | "all")` picks them, and graphix flags a stats reset between consecutive dumps (`simTicks` falling while `finalTick` rises), so "the last dump is the whole ROI" becomes a checked fact (the open `TODO(verify)` of the IMP spec, Q12).
5. **The `helper` source** (`graphix.sources.helper`): `RunSet.from_experiment(out_dir, experiment, done=True, labels=...)` finds the job directories, keeps finished ones (`window.json` stage `done`, `.job/exit.json` return code 0 with no signal, `simulation_finished`), and labels each with the experiment name and its `params.json` (workload, workload parameters, mods), on top of any user label function.
   The rules come from `motivation_v3.discover`, `eval_common.py`, and the thesis's completion checks, which then shrink to one call.

## Phase 3: semantics

1. **Tails in quantiles and CDFs**: `Buckets.quantile(q)` and the bucket export count underflow and overflow as samples; a quantile that falls in a tail returns `-inf` or `inf` and says so.
   The bucket export gains optional tail rows (`to_frame(tails=True)`).
2. **Units for histograms and scalars**: `Histogram.scale(factor, unit)` and `Scalar.scale(...)`, plus `Run.ticks_per_cycle(domain="board.clk_domain")`, so a tick histogram becomes cycles in one call.
3. **Outer alignment**: arithmetic gains `how="outer", fill_value=0`, for counters gem5 leaves out when zero (hits and misses per accessor).
4. **Unknown units are not ratios**: gem5's `Unspecified` and missing units stop triggering the sum warning, which then fires only for known ratios and rates.
5. **Empty histograms**: fill zeros only into runs that printed the family at least once (a ref run never gets an empty `hovLdindE2ELatency`), and add `Stat.drop_empty()`.
6. **Empty results keep their shape**: `catalog()`, `to_frame()`, and `table()` return empty frames with their usual columns.
7. **Derived gem5 quantities** (lower priority): `graphix.gem5.derived` with the common Formula replacements as functions over a `RunSet` (`demand_accesses`, hit ratio, `row_hit_rate`), since graphix skips Formulas by design.

## Phase 4: documentation and tests

- Tests for every change, on the existing fixtures plus a two-dump fixture with a reset.
- README: the three layers, the cache, `workers`, `from_experiment`, and where project specs belong.
- `gem5-viz.md`: drop the gotchas that phase 3 removes; describe the project package.
- The tutorial notebooks rebuilt against the frozen example, and run by the test suite as today.

## Not in this plan

- Plotting stays out of graphix.
- Folding graphix into `tools/experiment` waits; the `helper` source is written so it can move there unchanged.
- The bracket-versus-dot convention for dimension names (`briefing-room/20261005-181852-graphix-codebase-audit/proposal.md`, question 3) stays as it is: specs handle both.

## Verification

- `python -m unittest discover -s tools/graphix/tests` passes, including the tutorials.
- **Identity**: before phase 1, run `motivation_v3.py`, `perf_v3.py`, `speedup_v3/reduce_v3.py`, and `se_v2_baseline.py` with their caches cleared and save their CSVs; after each phase, rerun them and compare every CSV (equal, or different only where a phase 3 change is meant to differ, each such column listed).
- **Speed**: time a cold and a warm load of the 130 v3 runs before and after phase 2 (today about 3 minutes cold).
- **Isolation**: `grep -rniE "sift|hov" tools/graphix/graphix` returns nothing.

## Order and size

| Phase | Depends on | Size |
|---|---|---|
| 1 | answers to questions 1 to 4 | 1 day, mostly import updates and the identity check |
| 2 | phase 1 | 2 days |
| 3 | phase 1 | 1.5 days |
| 4 | phases 2 and 3 | half a day |

Phases 2 and 3 are independent and can be reviewed as separate diffs.

## Questions for the author

1. Is `projects/sift/analysis/sift_stats/` the right home for the SIFT package, or should it sit beside other project code (for example `projects/sift/stats/`)?
2. Should the `helper` source live in graphix (`graphix.sources.helper`, reading files only), or in `tools/experiment` from the start?
3. May I `pip install -e tools/graphix` into both venvs (`.venv/x86_64`, `.venv/aarch64`)?
4. Do the agent branches' graphix PRs (#1 to #3) and `agent/wp2`'s local commit close as superseded once their specs move to the project?
5. graphix's `redesign` branch is still unpushed; should the work land on `redesign`, or on `main` after you push `redesign` to it?
6. Like `tools/experiment`, may graphix break its API freely during this work (no shims for old imports), given that every caller is updated in the same change?

Answers (author, 2026-11-03): 1, `projects/sift/analysis/sift_stats/`; 2, decide so that the later merge into `tools/experiment` is easiest (done: the source lives in graphix and imports experiment's own file names from `experiment.api.runner`); 3, yes, both venvs; 4, yes, once their specs move; 5, on `redesign`; 6, yes.

## Outcome (2026-11-03)

- Between this plan and its implementation the SIFT project replaced its v1 to v3 analyses with `analysis/sift_sets.py` (one loader) and `motivation.py`, `results.py`, `roi.py`; the identity check used those on the v5 set (E41) and the v5-against-v4 comparison.
- Identity: all nine v5 CSVs and `roi_v5_vs_v4.csv` are byte-identical to the committed ones after every phase.
- Speed, 125 v5 runs on cascade: about 104 s before; 93 s cold and 31.5 s warm on one process after phase 2, 44 s cold on 8 processes; a median run (27 MB) loads in 0.50 s, 0.25 s from the cache.
- The scripts still compute their own tail-aware quantiles (`sift_sets.latency_quantile`, NaN in the overflow); graphix's now count the tails too (inf in the overflow), so they can switch when convenient.
- Not done: closing graphix PRs #1 to #3 and the local `agent/wp2` branch (outward-facing; waiting for the author), the aarch64 venv install, and the thesis script's switch to `sift_stats` (the author's file; a proposed diff only).
