# Merging `tools/experiment` and `tools/graphix` into `ungem5`: planning brief

Date: 2026-11-04.
Sources: the two packages as checked out in `/home/msamani/darchr/phd/tools/`, their memory and plan files, the `experiment-development` transcript (`b4bb809e-c16e-4951-a56f-bf4bc2f5ce04.jsonl`, 2026-09-24 to 2026-11-04), and the graphix transcript (`9269752e-5dd5-4e34-8971-13aa3fdaabd9.jsonl`, 2026-10-06 to 2026-11-04).
Nothing was modified; nothing below is decided until Mahyar says so.

## 1. State of each package today

### tools/experiment (`experiment`, the `helper` CLI and scheduler)

- Version `0.26.10.2` in `pyproject.toml`; Python 3.12 or newer; dependencies `cryptography`, `flask`, `gitpython`, `prompt_toolkit`, `psutil`, `rpyc>=6`, `werkzeug`, optional extras `dev` (`pytest`) and `model` (`scikit-learn`); entry point `helper = experiment.cli.helper:main_function`.
- Layout: `experiment/cli/{helper,initialize,build,run,work,schedule,certs}.py`; `experiment/api/{build,bundle,host,memory,model,resources,runner,script,simulate,userio,worker,work}.py`; `experiment/api/scheduler/{scheduler,daemon,console,dashboard,filters,state}.py` with `templates/` and `static/`; `experiment/common/{cmd_util,config_util,gem5_work,log,pki,settings}.py` and `common/assets/{decorators,run_example}.py`; `tests/` with 128 pytest tests; `deploy/` with `config.toml`, two systemd units and a README.
- What works and is deployed: the installed copy in `.venv/x86_64` is a non-editable install of GitHub commit `37210e4` (older than `main`), and it runs Mahyar's scheduler and the cascade worker; `.venv/aarch64` has the same version.
- `main` is at `0.26.10.2` with one unpushed commit `93f5c81` (`gem5Build` demand separate from build threads); the local refs still list `origin/master` and `origin/HEAD -> origin/master`, although memory says the remote has only `main`.
- Branch `multiuser` (checked out, local only) has eight commits on top of `main`: resources and memory placement (`9c6931e`), scripts compiled in the console (`9a9b717`), the lab scheduler and root workers (`ff87749`), memory enforcement (`fb9cd28`), the learned model (`dcf0e61`), `/etc/experiment/config.toml` (`9ca9175`), bundles with the per-host store and the outdir index (`cab6800`), and a bundle fix (`599f6a1`); 141 tests pass there.
- In flight on `multiuser`: `.job/index.json` written by the runner at exit (path, size, mtime, sha256 per file), `Worker.exposed_job_index`, `Host.job_index`, `userio.job_index`, `read_file` on any indexed path, and the console command `files <job> [--match GLOB]`.
- Not done: `helper fetch` (pulling an outdir through the index), pushing outputs from a host without NFS, per-job timeouts, the `GitResource` kind, and deployment per `deploy/README.md`.
- Job success is still the exit code; `simulation_finished` as a success rule is an open question in `context/memory.md`.
- `context/memory.md` and `memory-archive.md` are modified and uncommitted in the submodule, and the PhD repo records a different `tools/experiment` commit than the one checked out (`+` in `git submodule status`).
- The decision of 2026-10-27 holds: no backward compatibility while in beta, so the merge may rename and move freely with no shims.

### tools/graphix (`graphix`, typed gem5 statistics)

- Version `0.2.0` in `pyproject.toml`; Python 3.10 or newer; the only dependency is `pandas>=2` (the venv has pandas 3.0.6); no entry point and no CLI.
- Layout (3780 lines): `graphix/{__init__,cache,config,frame,names,owners,parse,records,run,specs}.py`, `graphix/stats/{align,base,histogram,ruby,scalar,vector}.py`, `graphix/gem5/{specs,derived}.py`, `graphix/sources/{__init__,helper}.py`; `tests/` with 87 unittest tests (`gxtest.py` gives them a temporary cache); `examples/sift/` with six tutorial notebooks built from `src/*.py` by `make_notebooks.py`, `tutorial_specs.py`, `useful_bytes.py`, `owners.json`, `sift_labels.py`; `example_data/` (23 MB, gitignored); `docs/plans/2026-11-03-improvements.md`; `under_the_rug/{barplot,markers,util}.py`, the parked plotting code.
- Public API (`graphix/__init__.py`): `RunSet`, `Run`, `Scalar`, `Vector`, `Histogram`, `ControllerCount`, `StatSpec`, `Registry`, `OwnerScheme`, `ConfigTree`, `read_dump`, `dump_locations`, `concat`, `table`, `suggest_specs`, and the `AmbiguousStatError`.
- What works: parsing the last dump of `stats.txt` or the largest `stats_dump_<N>.txt`, specs with dims, owner schemes, the four stat classes with their arithmetic and exports, the parsed-dump cache under `~/.cache/graphix` (`GRAPHIX_CACHE`), `workers=N` parallel parsing, `Run.dumps()` and `Run.resets()`, `RunSet.from_samples`, `RunSet.from_experiment`, and `graphix.gem5.derived`.
- Branch `redesign` (checked out, local, unpushed) holds the whole redesign and the five commits of 2026-11-03/04 (`66f2fcf` to `848d959`: SIFT out of the package, cache and helper source, semantics, docs); `main` tracks `origin/main` and predates the redesign.
- The PhD repo again records a different `tools/graphix` commit than the one checked out; the SIFT side of the 2026-11-03 plan (`projects/sift/analysis/sift_stats/`, the edited `sift_sets.py`) waits for Mahyar's review and commit.
- Installed editable in `.venv/x86_64` only; `.venv/aarch64` has neither graphix nor pandas.
- The three-layer rule is in force: nothing in `graphix/` names a project (`grep -rniE "sift|hov" graphix/` is empty); project knowledge lives in `projects/sift/analysis/sift_stats/`.
- Open outward-facing items: graphix PRs #1 to #3 and the local `agent/wp2` branch are superseded but not closed; the thesis script `documents/original/thesis/workbench/plots/sift_topdown.py` still inserts `tools/graphix` on `sys.path` and selects cores by raw path.
- Plotting stays out by decision (2026-10-09), and the plan file says "Folding graphix into `tools/experiment` waits; the `helper` source is written so it can move there unchanged."

## 2. Where the two already touch

- `graphix/sources/helper.py` imports `experiment.api.runner` lazily (`_runner()`), taking `JOB_DIR`, `EXIT_FILE`, `LAUNCH_FILE` and `read_json` from it; graphix's core never imports experiment, and `tests/test_sources.py` skips the source tests when `experiment.api.runner` is missing (`HAVE_EXPERIMENT`).
- `graphix/run.py::RunSet.from_experiment(experiment_dirs, statuses=(DONE,), done=None, labels=..., ...)` calls `helper.select_jobs` and `helper.job_labels`, then `from_dirs`; it is the only place the core touches the source.
- `helper.job_status` reads `.job/exit.json` for `returncode`, `signals_received` and `error`, and the existence of `.job/launch.json`; these are dict literals in `runner.py` (`main`, around the `write_json_atomic` of the exit record) with no schema class, so graphix depends on key names that nothing types.
- The statuses graphix defines (`DONE`, `UNFINISHED`, `FAILED`, `RUNNING`, `MISSING`) overlap but do not equal experiment's `JobStatus` in `api/work.py` or `userio.resolve_status` (`missing`, `running`, `exited`, `failed`, `killed`, `retry`); two vocabularies for one `exit.json`.
- graphix hardcodes `PARAMS_FILE = "params.json"` and `FINISHED_MARKER = "simulation_finished"`; experiment writes neither: the project's run script does, through `record_args` in `experiment/common/assets/decorators.py`, which `helper initialize` copies to `<project>/scripts/util/decorators.py` (`config_util.initialize_directories`), so every project has a copy that can drift from the asset.
- `clear_outdir` in the same decorator file deletes `params.json`, `run_function_called`, `simulation_finished`, `stats_dump_<N>.txt` and `readfile_*` before a rerun, so a job that is rerun in place loses the files graphix reads until it finishes again.
- graphix's `DONE` requires `simulation_finished`; experiment's scheduler never reads that marker and counts a job finished on exit code 0 (or its watcher), so the two tools can disagree about the same job (graphix `UNFINISHED`, experiment `FINISHED`).
- Outdir layout: experiment writes each job to `<experiment outdir>/<job id>` where the load script chooses the experiment outdir (`gem5Experiment(..., outdir=...)`; SIFT passes `gem5_out_base_dir / <name>` from `project_config.json` via `_get_project_config` in `job_defs.py`), and `graphix` takes those directories as paths and labels each run `experiment = job_dir.parent.name`, which equals the experiment name by convention only.
- `helper.find_jobs` treats any subdirectory holding `.job/` or `params.json` as a job; `helper run --outdir X` directories have no `.job/` and are found only when the run script wrote `params.json`.
- Inside a job directory experiment writes `constructor.py`, `stdout`, `stderr`, `.job/{run.json,build.json,process.json,files.json,launch.json,exit.json,memory.json,memory.jsonl,runner.log,watcher,watcher.json,watcher.log,resources.json,index.json}`; gem5 writes `stats.txt` or `stats_dump_<N>.txt`, `config.json` and `board.terminal`; the project writes `params.json`, the two markers and `window.json` (SIFT only).
- graphix reads only `stats*.txt`, `config.json`, `.job/exit.json`, `.job/launch.json`, `params.json` and `simulation_finished`; SIFT's `sift_done` adds `window.json`.
- experiment registers `stats.txt` and `board.terminal` as labelled dashboard files (`gem5_work.py`, the `files` list in `gem5Job`), and since `cab6800` the runner's `.job/index.json` lists every file with its sha256, `read_file` serves any indexed path, and `_cmd_files` in `scheduler.py` lists them; this is the designed hand-off point for the stats side, and `helper fetch` on top of it is not written.
- graphix's cache (`graphix/cache.py`) is keyed by path, size and mtime, so any fetch that copies files must preserve mtime or every fetched run re-parses once.
- Both assume today that every host sees the outdir over NFS (`/home/msamani/phd-outdir/sift` for SIFT); the 2026-11-03 design is the first that breaks this assumption, and it breaks it on the output side exactly where graphix reads.
- SIFT is the live bridge: `projects/sift/analysis/sift_sets.py` imports `graphix`, `graphix.sources.helper` and `sift_stats`, and calls `helper.select_jobs(out / experiment, done=sift_done)` and `gx.RunSet.from_dirs([run_dir], specs=SIFT_SPECS)`; `projects/sift/job_defs.py` imports `experiment.api.runner.JOB_DIR`, `experiment.api.work.Job`, `experiment.common.config_util._get_project_config` and `experiment.common.gem5_work`.
- Packaging: Python floors differ (3.12 against 3.10), dependency sets are disjoint (rpyc stack against pandas), versions follow different schemes (`0.YY.MM.N` against `0.2.0`), tests use different runners (pytest against unittest), and both are PhD submodules whose checked-out branch is not the one the PhD repo records.

## 3. What the chats decided or implied about merging

- 2026-10-09 (graphix chat, Mahyar, answering "Should graphix become a submodule like `tools/experiment`?"): "Eventually graphix will be integrated into `tools/experiment`."
- 2026-10-09 (same message): plotting is not coming back for now ("For now we should allow manipulation of stats and then exporting to DataFrame"), only the largest `stats_dump` is read, Formula stats and gem5's summaries are not read; these set what the stats side of a merged package does and does not do.
- 2026-11-03 (graphix plan, question 2 and the author's answer): the `helper` source should "decide so that the later merge into `tools/experiment` is easiest", done by having the source live in graphix and import "experiment's own file names from `experiment.api.runner`"; the plan's "Not in this plan" says "Folding graphix into `tools/experiment` waits; the `helper` source is written so it can move there unchanged."
- 2026-11-03 (same plan, answers 3 and 6): graphix is installed editable in both venvs, and graphix "may break its API freely during this work (no shims for old imports), given that every caller is updated in the same change".
- 2026-11-04 (graphix chat, closing summary): "`RunSet.from_experiment` ... uses experiment's own file-name constants, so it can move into `tools/experiment` unchanged later."
- 2026-11-04 03:31 (experiment chat, Mahyar): "the bundle directory should be per worker to prevent workers overriding each other in case they share a remote-fs", which became `<store>/<hostname>/<user>/`.
- 2026-11-04 03:34 (experiment chat, Mahyar, on outputs without a shared filesystem): "every time a job finishes, the worker sends an index of the jobs outdir. Then graphix and experiment can work together to pull the actual data that is needed instead of causing actual traffic on network."
- 2026-11-04 03:35 (Claude's reading of it, accepted): "When a job ends, the worker builds an index of the outdir (relative path, size, sha256 per file) and the scheduler keeps it with the job. graphix then asks for the files it needs through the existing chunked `read_file` call, which I'll extend to any path in the index."
- 2026-11-04 03:42 (experiment chat, the resources plan, part B): "`helper fetch <experiment|job>... --into DIR [--match GLOB ...]` materializes run directories locally through the scheduler, skipping files whose sha256 it already has. The default globs are what graphix reads, so `gx.RunSet.from_experiment([DIR/...])` then works unchanged."
- 2026-11-04 03:42 (same): "graphix reads run directories (`stats*.txt`, `config.json`, `.job/exit.json`, `params.json`, `simulation_finished`) and caches by path, size and mtime. It never needs the scheduler; it needs those files on a local path."
- 2026-11-04 03:48 (Mahyar): "as soon as the job is finished send all the files over (if they're not already on a shared device obviously)", to which Claude answered that a push needs a destination and a writer (the lab scheduler runs as a service user, not the owner), so "I'd build the index now and settle the push when a host without NFS exists", and Mahyar said "Start please".
- 2026-11-04 22:20 (experiment chat, closing summary): the index is built and served, and "`helper fetch` for graphix, built on the index" is listed under "Not done yet".
- The experiment memory records the design as a decision dated 2026-11-03: "when a job ends the worker sends an index of its outdir and graphix pulls only the files it needs; big inputs stay `PathResource` on NFS for now; job ids stay parameter-only with the resource hash as a revision."
- 2026-10-27 (experiment memory): "no backward compatibility while in beta: break APIs freely, no keyword-only arguments, shims, or tolerance for old workers or state."
- Nothing in either chat names the merged package, its layout, or whether the two git repositories merge; the only API shape implied is `RunSet.from_experiment` over directories that `fetch` fills.

## 4. Proposed layouts for `ungem5`

### Option A: one package with subpackages (recommended)

```
ungem5/
  cli/            helper.py, initialize.py, build.py, run.py, work.py, schedule.py, certs.py, fetch.py (new)
  jobs/           work.py, gem5_work.py, script.py, bundle.py, resources.py, config_util.py, settings.py, assets/
  schedule/       scheduler.py, daemon.py, console.py, dashboard.py, filters.py, state.py, host.py, worker.py, runner.py, userio.py, memory.py, model.py, templates/, static/
  run/            build.py, simulate.py            (the backends shared by `helper build`/`helper run` and jobs)
  stats/          graphix's core as is: parse.py, config.py, names.py, specs.py, records.py, run.py, cache.py, frame.py, owners.py, stats/, gem5/
  results/        jobs.py (today's graphix/sources/helper.py), fetch.py (new: pull indexed files through the scheduler)
  common/         cmd_util.py, log.py, pki.py
```

- One distribution, one version (`0.YY.MM.N`), one `pyproject.toml`, one test directory run by pytest (unittest classes run under pytest unchanged), one install on every host.
- `results/jobs.py` imports `..schedule.runner` directly and stops being a lazy, optional import; the two status vocabularies can become one.
- The stats core moves with its relative imports intact (every module in `graphix/` uses relative imports), so the move is a directory rename plus the `__init__.py` exports; analysis code writes `import ungem5.stats as gx` and the `gx.` idiom survives.
- pandas becomes either a hard dependency of the workers or an extra (`ungem5[stats]`); an extra keeps root workers light and avoids a pandas wheel on aarch64 until needed, at the cost of a lazy import in `results/jobs.py`.
- Cost: every absolute `experiment.` import (17 files across `experiment/` and `tests/`), the `python -m experiment.api.{runner,simulate,build,script,userio}` command strings in `runner.py`, `worker.py`, `userio.py`, `script.py`, `build.py`, `simulate.py`, `scheduler/daemon.py` and `deploy/experiment-scheduler.service`, and the identifiers on disk (`/etc/experiment/config.toml`, `~/.config/experiment/pki`, `~/.local/state/experiment`, `/tmp/experiment-<uid>`, `~/.cache/experiment/store`, `experiment-<owner>.slice`, `EXPERIMENT_RUNTIME_DIR`, `EXPERIMENT_CONFIG`, `~/.cache/graphix`, `GRAPHIX_CACHE`).
- Cost: the two git histories must be joined (`git subtree add` or `git filter-repo` of graphix into the experiment repo before the rename), or one history is abandoned.

### Option B: a namespace package `ungem5.experiment` and `ungem5.graphix`

- Each package keeps its modules and history, moved one directory down; imports change from `experiment.api.x` to `ungem5.experiment.api.x` and from `graphix` to `ungem5.graphix`.
- Least code motion and the smallest diff, and the two can stay separate distributions sharing a namespace.
- It merges nothing real: two versions or one, two test runners, `graphix.sources.helper` still imports across the boundary, and the names `experiment` and `graphix` outlive the package they belonged to.
- Namespace packages (no `__init__.py` at the top) interact badly with editable installs and `setuptools.packages.find`, and the PhD venv uses an editable install for graphix today.
- Verdict: a rename that postpones the merge; only worth it if Mahyar wants the two histories and release cadences kept apart for a while longer.

### Option C: graphix stays a separate distribution; the merged package is experiment plus the results side

- Move `graphix/sources/helper.py` into experiment as `experiment/results/jobs.py` (the plan's "moves there unchanged"), add `helper fetch`, and have graphix depend on nothing from experiment: `RunSet.from_dirs` takes the directories that `experiment.results` selects.
- Keeps graphix a clean, project-free, lab-free library that anyone with a gem5 `stats.txt` can install, with pandas as its only dependency and Python 3.10 as its floor.
- Keeps two repos, two versions, two installs on every host and two memory files; `sift_sets.py` imports both; the ARM venv still needs both installs; and Mahyar's 2026-10-09 statement that graphix "will be integrated into `tools/experiment`" is not met.
- Verdict: the right choice only if graphix is meant for an audience outside the lab (a release or a paper artifact); nothing in the chats says that.

### Recommendation

- Option A, with pandas as the `stats` extra so workers and the lab's root venv stay as light as today, and with `results/` as the subpackage that knows both the job layout and the stats reader.
- Keep the graphix core's internal structure and relative imports exactly as they are under `ungem5/stats/`, so the diff is a move and the 87 tests follow with only their import lines changed.
- Join the histories with `git subtree add --prefix=graphix <graphix repo> redesign` (or `filter-repo` to the `stats/` path) before moving files, so `git log --follow` works on the stats modules.
- Keep `helper` as the CLI name (every skill, memory file, script and host alias uses it) and let the package name be invisible to the shell.

## 5. What the merge changes for users

- Imports in analysis code: `import graphix as gx` becomes `import ungem5.stats as gx`; `from graphix.sources import helper` becomes `from ungem5.results import jobs` (or the chosen name); `gx.RunSet.from_experiment` keeps its signature.
- Imports in job and host definitions: `experiment.api.host.Host`, `experiment.api.runner.JOB_DIR`, `experiment.api.work.Job`, `experiment.common.config_util._get_project_config`, `experiment.common.gem5_work.*` and `experiment.api.scheduler.console.ControlClient` move to their new subpackages (`ungem5.schedule.host`, `ungem5.schedule.runner`, `ungem5.jobs.work`, `ungem5.jobs.config_util`, `ungem5.jobs.gem5_work`, `ungem5.schedule.console`).
- The `helper` CLI keeps its name and subcommands (`initialize`, `build`, `run`, `work`, `schedule`, `console`, `certs`) and gains `fetch`; its entry point line in `pyproject.toml` changes to `ungem5.cli.helper:main_function`.
- `project_config.json` keeps its schema (`project_name`, `path_config`, `build_config`) because nothing in it names the package; `helper initialize` still writes it; the only question is whether the copied `scripts/util/decorators.py` should import its marker names from `ungem5` instead of holding its own copies.
- SIFT files that import graphix or experiment (from `grep -rl` in `projects/sift/analysis` plus the project root): `analysis/sift_sets.py` (graphix, `graphix.sources.helper`, `sift_stats`), `analysis/motivation.py` and `analysis/results.py` (through `sift_sets`), `analysis/sift_stats/__init__.py`, `analysis/sift_stats/specs.py`, `analysis/sift_stats/useful_bytes.py` (graphix), and outside `analysis/`: `job_defs.py`, `hosts.py`, `scripts/util/port_proxy_watch.py` (experiment); `analysis/roi.py` and `analysis/ume_pipe3d/` import neither.
- The thesis script `documents/original/thesis/workbench/plots/sift_topdown.py` inserts `tools/graphix` on `sys.path` and must switch to the installed package (Mahyar's file, proposed diff only).
- Notebooks under `tools/graphix/examples/sift/` (`01` to `06`, `plot_sgo_original.ipynb`) and their sources in `src/*.py`, `tutorial_specs.py`, `useful_bytes.py` all `import graphix as gx`; they move to `ungem5/examples/` (or `docs/examples/`), get the new import, and are rebuilt by `make_notebooks.py`; `example_data/` (23 MB, gitignored) and `examples/sift/data/` must be copied by hand because git does not track them.
- Living rules that name the packages: `CLAUDE.md` (Layout and Skills), `.claude/skills/visualization-rules/references/gem5-viz.md`, `.claude/skills/gem5-build/` and `gem5-run/`, `.claude/skills/memory-update/`, `projects/sift/docs/agents/briefs/wave1-common.md`, `tools/experiment/CLAUDE.md`, `context/overview.md`, and the memory files of both tools.
- The PhD repo: `.gitmodules` drops `tools/experiment` and `tools/graphix` and adds `tools/ungem5`; the parent-directory symlink `../experiment` (kept so old scripts and other hosts work) must point at `tools/ungem5`, or those hosts break.
- Venv installs: `.venv/x86_64` has experiment `0.26.10.2` non-editable (GitHub `37210e4`) and graphix `0.2.0` editable; `.venv/aarch64` has experiment only; both get `pip uninstall experiment graphix` and one `pip install tools/ungem5[stats]`, and the running scheduler and workers must be restarted on the new version (`STATE_VERSION` governs whether saved state survives).
- The lab deployment (`deploy/README.md`): the root venv in `/opt/experiment`, `/etc/experiment/pki`, `/etc/experiment/config.toml` and the unit files are not deployed yet, so renaming them now costs nothing; renaming them later costs a migration.

## 6. Phased plan

No backward-compatibility requirement applies (decision 2026-10-27), so no shims, aliases or `graphix` compatibility packages are planned; every caller changes in the same commit as the thing it calls.

### Phase 0: land what is in flight, so the merge starts from pushed `main` branches

- Mahyar reviews the `multiuser` branch, merges it into experiment `main`, and pushes; then the PhD repo's `tools/experiment` pointer is updated (the submodule must be pushed before the PhD repo, as memory says).
- Mahyar reviews and commits the SIFT side of the graphix plan (`projects/sift/analysis/sift_stats/`, `sift_sets.py`, `wave1-common.md`, `gem5-viz.md`), merges graphix `redesign` into `main`, pushes, and updates the `tools/graphix` pointer.
- Close graphix PRs #1 to #3 and delete `agent/wp2` (superseded), so no open branch targets a repo about to be archived.
- Install graphix into `.venv/aarch64` only if an ARM analysis is needed before the merge; otherwise skip and let phase 4 do one install.
- Test: `pytest` in `tools/experiment` (141) and `python -m unittest discover -s tools/graphix/tests` (87) pass; `motivation.py`, `results.py`, `roi.py` reproduce the committed v5 CSVs byte for byte with a fresh `GRAPHIX_CACHE`.

### Phase 1: decide the name, the layout and the identifiers

- Mahyar picks `ungem5` and the subpackage names (the table in section 4 is a proposal), decides pandas as a hard dependency or the `stats` extra, and decides whether the on-disk identifiers (`/etc/experiment`, `~/.local/state/experiment`, `/tmp/experiment-<uid>`, `experiment-<owner>.slice`, `~/.cache/graphix`, the `EXPERIMENT_*` and `GRAPHIX_CACHE` variables) follow the rename.
- Decide the repository strategy: rename the GitHub repo `mahyarsamani/experiment` to `ungem5` (GitHub redirects the old URL) and subtree-import graphix, or start a new repo and archive both.
- Write the decisions into `tools/experiment/context/memory.md` (Decisions) and `context/decisions-log.md` with the `memory-update` skill.
- Write ungem5's `context/memory.md` as the union of the two tools' memories (decided 2026-11-04): experiment's `context/memory.md` and `memory-archive.md`, and graphix's state from its README, `docs/plans/2026-11-03-improvements.md`, the audit of 2026-10-05 and the development chat; its "Not done" and "Next steps" sections are the union of both tools' open work, each item marked with the side it came from, and nothing is dropped without a dated decision.
- Test: none to run; the deliverables are a dated decision entry, the final layout table, and the merged memory file, which replaces experiment's `context/memory.md` when the repo becomes ungem5 in phase 2.

### Phase 2: build `ungem5` in the experiment repo on a branch, leaving both installed packages untouched

- On a branch `merge-graphix` of the experiment repo: `git subtree add --prefix=graphix-import <graphix repo> main` to carry graphix's history, then `git mv` the modules into the layout: `experiment/` to `ungem5/`, `api/scheduler/*` and `api/{host,worker,runner,userio,memory,model}.py` to `ungem5/schedule/`, `api/{work,script,bundle,resources}.py` and `common/{gem5_work,config_util,settings}.py` and `common/assets/` to `ungem5/jobs/`, `api/{build,simulate}.py` to `ungem5/run/`, `graphix-import/graphix/` to `ungem5/stats/` minus `sources/`, and `graphix-import/graphix/sources/helper.py` to `ungem5/results/jobs.py`.
- Rewrite every import with the Edit tool (no scripted mass edits, per CLAUDE.md), including the `python -m experiment.api.*` strings in `runner.py`, `worker.py`, `userio.py`, `script.py`, `build.py`, `simulate.py`, `scheduler/daemon.py`, the docstrings that cite them, `deploy/experiment-scheduler.service`, `deploy/experiment-worker.service`, `deploy/config.toml` and `deploy/README.md`.
- In `results/jobs.py` import `JOB_DIR`, `EXIT_FILE`, `LAUNCH_FILE`, `read_json` directly from `..schedule.runner`, move `PARAMS_FILE` and `FINISHED_MARKER` next to `record_args` in `jobs/assets/decorators.py` (or a tiny `jobs/markers.py` the asset imports), and drop the `_runner()` indirection and the `HAVE_EXPERIMENT` guard in the tests.
- Merge `pyproject.toml`: name `ungem5`, version `0.26.11.1`, `requires-python >= 3.12`, experiment's dependencies, extras `dev`, `model`, `stats = ["pandas>=2"]`, entry point `helper = ungem5.cli.helper:main_function`, package data for `jobs/assets/*.py`, `schedule/templates/*.html`, `schedule/static/*.css`.
- Move `tests/` of both into one `tests/` run by pytest; keep `gxtest.py`'s temporary cache; add a test that imports every module of `ungem5` to catch a missed rename.
- Move `examples/sift/` and `make_notebooks.py` to `examples/`, change `import graphix as gx` to `import ungem5.stats as gx` in `src/*.py`, `tutorial_specs.py`, `useful_bytes.py`, rebuild the notebooks, copy `example_data/` by hand.
- Fold `README.md` (graphix) and `CLAUDE.md`, `context/memory.md`, `context/memory-archive.md` (experiment) into one set: the README gains the "Three layers" and "How it works" sections, the memory gains graphix's state and decisions.
- Test: `pytest` passes (141 + 87) in a scratch venv with `pip install -e ".[dev,stats,model]"`; `grep -rnE "\b(import|from) (experiment|graphix)\b" ungem5 tests examples` returns nothing; `python -m ungem5.schedule.runner --help`, `python -m ungem5.run.simulate --help` and `helper --help` run from the scratch venv.

### Phase 3: move the callers, still against the scratch venv

- SIFT: `analysis/sift_sets.py`, `analysis/sift_stats/{__init__,specs,useful_bytes}.py`, `job_defs.py`, `hosts.py`, `scripts/util/port_proxy_watch.py`, and `scripts/util/decorators.py` if phase 2 moved the marker names; `analysis/motivation.py` and `analysis/results.py` need no change.
- The sandbox `experiment-test/run_many.py` and `run_example.py` (outside the PhD repo) change their imports the same way.
- Rules and docs: `CLAUDE.md` (Layout, Skills, "Dogfood" line), `gem5-viz.md`, the `gem5-build`, `gem5-run` and `memory-update` skills, `wave1-common.md`, `context/overview.md`; propose a diff for `documents/original/thesis/workbench/plots/sift_topdown.py`.
- Test: with the scratch venv first on `PATH`, `motivation.py`, `results.py`, `roi.py` and `roi.py --against v4` reproduce the committed CSVs byte for byte; a load script in the sandbox compiles (`helper schedule` plus `load run_many.py` against a scratch scheduler on a free port) and one `run_example.py` job exits 0; the six tutorials execute under the test suite.

### Phase 4: switch the PhD repo and the hosts

- In the PhD repo: replace the two submodules in `.gitmodules` with `tools/ungem5` (the renamed experiment repo), remove `tools/experiment` and `tools/graphix`, point the parent-directory symlink `../experiment` at `tools/ungem5`, and keep `../scatter-gather-opt` as is.
- In a quiet window with no running simulations: `pip uninstall experiment graphix` and `pip install tools/ungem5[stats]` in `.venv/x86_64`, the same in `.venv/aarch64` on an ARM host, then restart the scheduler (`helper schedule`) and the cascade worker (`helper work --name cascade --san <fqdn>`) on the new version; saved state is reloaded only if `STATE_VERSION` matches, otherwise finished jobs are adopted from their outdirs (phase 3 of the multi-user plan).
- Decide editable or pinned install per host: the daemons should run a pinned install (as today), analysis may use the same venv, so a change to the stats core means a reinstall and not a `git pull`.
- Test: `helper --help` on every host, the console reattaches and `jobs` shows the adopted jobs, one real sandbox job runs end to end, and `sift_sets.load` finds the same 125 v5 runs as before.

### Phase 5: the merge's payoff, `fetch` on the index

- Add `ungem5/results/fetch.py` and `cli/fetch.py`: `helper fetch <experiment|job>... --into DIR [--match GLOB]` walks the scheduler's jobs, asks each host for `job_index`, pulls files whose sha256 differs from what `DIR` holds through `read_file`, preserves mtime so the stats cache stays valid, and defaults the globs to what `results/jobs.py` and `stats` read (`stats*.txt`, `config.json`, `.job/launch.json`, `.job/exit.json`, `params.json`, `simulation_finished`, `window.json`).
- Give `RunSet.from_experiment` nothing new; it reads `DIR/<experiment>/<job>` as it reads NFS today, which is the shape the 2026-11-04 plan promised.
- Settle the push-versus-pull question only when a host without NFS exists (the chat's conclusion of 2026-11-04 03:49).
- Test: fetch a finished sandbox experiment into a scratch directory, load it with `from_experiment`, and compare `runs["simTicks"]` (or any scalar) with the same stat read from the NFS outdir; a second fetch transfers zero bytes.

### Phase 6: close out

- Archive the GitHub `graphix` repo (read only), add the merge to `context/decisions-log.md`, refresh the memory files and `context/overview.md` with `memory-update`, and remove stale `__pycache__` entries that still name `experiment` modules.
- Test: `git submodule status` in the PhD repo shows one tool submodule with no `+`, and a fresh clone with `--recurse-submodules` installs and passes `pytest`.

## 7. Risks and open questions for Mahyar

- Running daemons: the scheduler and cascade worker run on GitHub `37210e4`, older than `main` and much older than `multiuser`; the first reinstall is already a large jump (`STATE_VERSION` 2), and the merge adds a package rename on top, so phase 4 needs a window with nothing running and a plan for jobs mid-flight.
- Two unpushed branches hold all recent work (`multiuser`, eight commits; `redesign`, the whole redesign), and the PhD repo records older commits for both submodules; losing either working tree loses work, so phase 0 (push) should not wait for the name.
- Identifiers on disk: renaming `/etc/experiment`, `~/.local/state/experiment`, `/tmp/experiment-<uid>`, the `experiment-<owner>.slice` names and `~/.cache/graphix` is cheap now and expensive after the lab deployment; the question is whether these follow the package name or stay as deployment names.
- The `simulation_finished` disagreement: experiment counts exit code 0 as finished, the stats side requires the marker; the merge is the natural point to make the runner record the marker's presence in `exit.json` (it knows the outdir) and to give both sides one status enum, but that changes job semantics and belongs to Mahyar.
- `record_args` lives in every project as a copied `scripts/util/decorators.py`, so the marker names graphix depends on are defined in files experiment does not control; should the copy import them from `ungem5` instead (SIFT's gem5 run adds `ungem5` to the import path only if the venv is active in the job)?
- pandas as a hard dependency puts a pandas wheel in the root workers' venv and in `.venv/aarch64`; the `stats` extra avoids that but means `results/jobs.py` must not import pandas at module level.
- Python 3.12 becomes the floor for the stats code (graphix allowed 3.10); the PhD venvs are 3.12, so only outside users would notice.
- History: a subtree import keeps graphix's commits but puts them under a prefix; a plain copy loses `git blame` for 3780 lines; which matters to you?
- The CLI name `helper` and the module name `sources/helper.py` collide in conversation; the proposal renames the module to `results/jobs.py`, but the chat and plan file consistently say "the helper source", so pick the word.
- Editable against pinned installs in one venv: today graphix is editable and experiment pinned because the daemons must not pick up edits; a single package forces one choice per venv unless the daemons move to their own venv (the lab plan's `/opt/experiment` does that for root workers only).
- The `experiment` label in `job_labels` equals the directory name, not `Experiment.name()`; should `results/jobs.py` read the name from `constructor.py` or `.job/run.json`, or should `gem5Experiment` write an `experiment.json` marker, now that both sides are one package?
- `fetch` through the scheduler is single-threaded over rpyc with chunked `read_file`; a 27 MB dump per run for 125 runs is about 3.4 GB through one socket, so is `rsync` printed as the alternative enough, or should the worker serve files directly?
- Is graphix ever meant for people outside the lab (a release, an artifact for a paper)? If yes, option C or a separate `stats` distribution is the right call and this brief's recommendation flips.
- Who closes graphix PRs #1 to #3 and renames or archives the GitHub repos: these are outward-facing actions the agent rules reserve for you.
