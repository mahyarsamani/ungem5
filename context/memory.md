# ungem5 memory

Last updated: 2026-11-04 (Claude Code, drafted as the union of the `experiment` and `graphix` memories, then updated when phase 2 built ungem5 on branch `ungem5`).

## What this is
`ungem5` is the package that merges `experiment` (the `helper` CLI, gem5 builds and runs, and the scheduler that drives lab hosts over mutual-TLS rpyc) with `graphix` (typed gem5 statistics that select, reduce and export to pandas): everything architecture research needs that is not the simulator, by analogy with the uncore.
Layout option A, decided 2026-11-04: one distribution with the subpackages `cli`, `jobs`, `schedule`, `run`, `stats` (graphix's core unchanged), `results` (the job source and `fetch`) and `common`; pandas is the `stats` extra.
Phase 2 of the merge plan is done on branch `ungem5` of the `tools/experiment` repo; the installed packages, `tools/graphix` and every caller stay as they were until phases 3 and 4.
This file is the union of the two tools' memories, so that nothing pending on either side is lost.

## Current state
- [merge] Branch `ungem5` (local, from `multiuser` at 599f6a1) holds this work: graphix subtree-imported from `redesign` (848d959) and moved into `ungem5/stats/` and `ungem5/results/`, experiment moved into `ungem5/{cli,jobs,schedule,run,common}/`, the on-disk names renamed (`/etc/ungem5`, `~/.local/state/ungem5`, `/tmp/ungem5-<uid>`, `~/.cache/ungem5/{store,stats}`, `ungem5-<owner>.slice`, `UNGEM5_*`), one pytest suite (287 passed, 6 skipped without `example_data/`), version 0.26.11.1.
- [merge] Done on `ungem5`: `results/jobs.py` imports the runner's names directly (no `_runner()`, no `HAVE_EXPERIMENT`); the marker names live in `ungem5/jobs/markers.py`, spelled out in the copied `decorators.py` and checked by a test; `ungem5.results` and `import ungem5.stats` need neither pandas nor the scheduler; graphix's README is folded into `README.md`; `examples/sift/` moved; the old-API notebook and `under_the_rug/` are parked in `docs/parked/graphix-plotting/`.
- [merge] Not moved yet: SIFT, the sandbox, the thesis script, the PhD repo's rules and submodules, and both venvs still use `experiment` and `graphix`.
- [experiment] Version 0.26.10.2; `main` (93f5c81, `gem5Build` demand separate from build threads) is one commit ahead of `origin/main`, and the PhD repo records a different commit than the checkout.
- [experiment] Branch `multiuser` (checked out, local only) has eight commits over `main`: resources and memory placement, scripts compiled in the console, the lab scheduler and root workers, memory enforcement, the learned model, `/etc/experiment/config.toml`, bundles with the per-host store and the outdir index, and a bundle fix; 141 tests pass.
- [experiment] Bundles work end to end: two sandbox gem5 jobs ran from the store on cascade after the project directory was moved away; `.job/index.json`, `read_file` on any indexed path and the console command `files <job> [--match GLOB]` are in.
- [experiment] Installed: a non-editable copy of GitHub 37210e4 in `.venv/x86_64` runs my scheduler and the cascade worker (since 2026-10-13); `.venv/aarch64` has the same version; nothing newer is installed anywhere.
- [experiment] `STATE_VERSION` is 2 and workers, scheduler and consoles must run the same version; a finished run already in an outdir is adopted, not rerun; the root-only paths (`systemd-run`, cgroup freeze, slice caps) ran only against a fake `systemd-run`.
- [experiment] experiment's own `context/memory.md` (as last edited 2026-11-03) was replaced by this file; its unique parts are in `memory-archive.md` under 2026-11-04.
- [graphix] Version 0.2.0, Python 3.10 or newer, pandas the only dependency, no CLI; 3780 lines in `graphix/{parse,config,names,specs,records,run,cache,frame,owners}.py`, `stats/`, `gem5/` and `sources/helper.py`; 87 unittest tests.
- [graphix] Branch `redesign` (checked out, no upstream) holds the whole redesign and the five commits of 2026-11-03/04 (66f2fcf to 848d959); `main` (8644275, "Snapshot before redesign") predates it; the PhD repo records yet another commit.
- [graphix] Works: the last dump of `stats.txt` or the largest `stats_dump_<N>.txt`, specs with dims, owner schemes, `Scalar`, `Vector`, `Histogram` and `ControllerCount` with arithmetic and exports, the parsed-dump cache under `~/.cache/graphix` (`GRAPHIX_CACHE`), `workers=N`, `Run.dumps()` and `Run.resets()`, `RunSet.from_samples`, `RunSet.from_experiment`, and `graphix.gem5.derived`.
- [graphix] Installed editable in `.venv/x86_64` only; `.venv/aarch64` has neither graphix nor pandas.
- [graphix] The three-layer rule holds (`grep -rniE "sift|hov" graphix/` is empty); SIFT's specs, owners and labels live in `projects/sift/analysis/sift_stats/`, uncommitted and awaiting my review together with `sift_sets.py`, `wave1-common.md` and `gem5-viz.md`.
- [graphix] Speed on 125 v5 runs (cascade): 93 s cold and 31.5 s warm on one process, 44 s cold on 8; all nine v5 CSVs and `roi_v5_vs_v4.csv` stayed byte-identical through every phase.
- [graphix] Of the 2026-10-05 audit, the redesign fixed the operator bugs, the parent bookkeeping, the `grep` shell-out, the asserts, the fake-`Node` aggregators, the `List[List[Scalar]]` shape, the empty `__init__`, the missing Vector class, the number regexes, and the missing README, tests and packaging; the parked plotting code in `under_the_rug/` (not importable) and the absence of a CLI remain, and `stats.json` was dropped by decision.

## Decisions
- 2026-11-04 [both]: `experiment` and `graphix` merge into one package named `ungem5` (free on PyPI; `dynorig`, `outboard` and `archrig` were the alternatives), layout option A, pandas as the `stats` extra; the memory is founded as the union of both tools' memories, and nothing pending is dropped without a dated decision.
- 2026-11-04 [experiment]: the bundle store is per host and user, `<store>/<hostname>/<user>/`, so workers sharing a mount never overwrite each other; a user's worker uses `~/.cache/experiment/store`, a root worker `[worker] store`.
- 2026-11-04 [experiment]: a bundle's files are found from the top-level run script by following imports as Python resolves them (only directories with `__init__.py` count); string literals naming project paths and relative job arguments become data linked into the working directory; dynamic imports warn and go in `extra_files`.
- 2026-11-04 [experiment]: gem5 experiments bundle by default (`bundle=False` opts out); `GitResource` is dropped from the first cut, so gem5's source and binary stay `PathResource`s.
- 2026-11-04 [experiment]: when a job ends the runner writes `.job/index.json` (path, size, mtime, sha256) and `read_file` serves any indexed path; the push from a host without NFS is settled only when such a host exists; `helper fetch` on the index is the hand-off to the stats side.
- 2026-11-03 [experiment]: jobs without a shared filesystem: resources are frozen at `load`, bundles tarred by the console and content-addressed, big inputs stay on NFS, and job ids stay parameter-only with the resource hash as a revision.
- 2026-11-03 [experiment]: service settings move to `/etc/experiment/config.toml`; flags override, and a per-user file covers `[lab]` only.
- 2026-11-03 [experiment] (Claude, open to change): a reload gives non-running jobs their new definition and keeps their status; a restart restores compiled jobs without running scripts; hosts added later reach a script's jobs only on its reload.
- 2026-11-03 [experiment] (same): lab names are `<user>/<name>` and scripts `<user>:<path>`; only admins load scripts that add hosts; only owners and admins read job files; a host with partitions refuses users without a share unless `*` has one.
- 2026-11-03 [experiment] (same): grace is 25% over current use; a host under 3% free memory pauses its newest job and resumes at 6%; the model fits at 50 complete runs, refits every 20, with quantile 0.9 and 95% conformal coverage.
- 2026-11-03 [graphix]: three layers: the core knows stock gem5 only, `sources/` knows a tool's run layout, and the project owns its specs, stat classes, owners and labels; the `helper` source imports experiment's file names from `experiment.api.runner` so the merge is easiest.
- 2026-11-03 [graphix]: graphix breaks its API freely when every caller changes in the same commit; work lands on `redesign`; the bracket-versus-dot dim convention stays (specs handle both); plotting stays out.
- 2026-11-03 [graphix]: a histogram's `mean()` is exact (gem5's sum is kept; `stdev`, `total` and quantiles derive from the buckets); quantiles and CDFs count the tails (`inf` in a tail); a sum warns only for known ratios and rates; a histogram is never filled into a run that printed none of it; empty results keep their columns.
- 2026-10-28 [experiment]: memory is measured with psutil and cgroups, never gem5's `hostMemory`, at a configurable sampling interval (2 minutes suggested) (inferred from the chat; the memory file records only the gotcha); the lab scheduler host is liberty.
- 2026-10-27 [experiment]: no backward compatibility while in beta: break APIs freely, no keyword-only arguments, shims, or tolerance for old workers or state.
- 2026-10-27 [experiment]: the multi-user plan: resources and memory recording; `load` compiles the script in the console so the scheduler never runs user code; one lab scheduler and a root worker per machine launching via `systemd-run` in per-user slices with uid auth over `SO_PEERCRED`; memory enforcement with grace and a learned model.
- 2026-10-27 [experiment]: gem5 jobs take 1 core; memory is learned per simulation and the user's guess is corroborated, never trusted; strict per-user partitions with a 10% borrowing margin and no preemption; msamani is admin and everyone sees all jobs read-only; only GID 1024023017 (`coe-us-cs-darchr`, by number) may launch; a job past its reservation grows if there is room, else it is frozen, with `on_memory_exceed` (`pause`, `kill`, `retry`) overriding.
- 2026-10-12 [experiment]: builds are jobs (`gem5Build`, pinned per host because `/scr` is local) and simulations depend on them.
- 2026-10-09 [graphix]: plotting does not come back for now (stats manipulation and DataFrame export only); `stats.json` and the JSON interface are dropped; only the largest dump is read; Formula stats and gem5's summary lines are not read; stat classes follow gem5's stat kinds with operations suited to each; graphix will be integrated into `tools/experiment` (now the ungem5 decision).
- 2026-10-03 [experiment]: job IDs depend on parameters only, never on code.
- 2026-10-01 [experiment]: the version is `0.YY.MM.N`, lives only in `pyproject.toml`, and is bumped only on request as its own commit; the merge proposes `0.26.11.1`.
- 2026-09-30 [experiment]: mutual TLS over rpyc with a self-managed CA (`helper certs`) and the role in the certificate OU; one scheduler per user per machine, locked in `/tmp/experiment-<uid>`, with state under `~/.local/state/experiment/<host>/<name>`; the console attaches to the running daemon; Python 3.12 or newer, `cryptography`, `rpyc>=6`, `pytest` as `dev`.

## Constraints and gotchas
- [experiment] gem5's `hostMemory` is VmSize, not resident memory; lab machines run cgroup v2 and systemd 255, so cgroup freeze works; home is NFS from glacier with `sec=sys`.
- [experiment] `helper` finds `project_config.json` by walking up and resolves every path; the build name comes first (`helper build main ...`, `helper run main <script> -- <args>`); `helper run` returns 0 when gem5 panics, so check the log.
- [experiment] Certificates live in `~/.config/experiment/pki/`; `socket.getfqdn()` returns the short name on lab machines, so worker certificates need `--san <fqdn>` or TLS fails as a logged timeout; port 9100 on perle is taken.
- [experiment] `/scr` is local, so the gem5 binary must exist on every worker; workers are stateless, so deleting a running job's outdir marks it failed.
- [experiment] Logs are in `~/.local/state/experiment/<host>/<name>/`; `EXPERIMENT_RUNTIME_DIR` overrides the runtime directory for tests; the dashboard on localhost:9200 is reached by SSH tunnel, and `info` prints its URL and token.
- [experiment] A non-editable install does not pick up `git pull`; the PhD venv has no pytest, so tests run from a scratch venv with `pip install -e ".[dev]"`; `setsid nohup` breaks `$!`, so find test workers by port and kill by PID, never `pkill -f`.
- [experiment] Python 3.12 `argparse` raises `SystemExit` even with `exit_on_error=False`; the sandbox `run_example.py` needs a power-of-two generator count, so `run_many.py` fails for 3, 5, 6 and 7.
- [experiment] Job success is the exit code unless a watcher says otherwise, so a hung gem5 without a watcher holds its slot.
- [graphix] The cache is keyed by path, size, mtime and `PARSER_VERSION`, so a copy that does not preserve mtime re-parses once; `cache=False` or `GRAPHIX_CACHE=off` bypasses it.
- [graphix] `config.json` zero-pads SimObject vectors of ten or more (`system_links01`) but stats do not; a value wider than 12 characters glues to the bar; unnamed groups print one name several times (`occurrence`); `nozero` stats vanish and are filled as zero; one name on several classes raises `AmbiguousStatError`.
- [graphix] `from_experiment` labels a run `experiment = job_dir.parent.name`, equal to the experiment name by convention only; `find_jobs` counts any directory with `.job/` or `params.json`, so `helper run --outdir X` runs appear only if the script wrote `params.json`.
- [both] `params.json` and `simulation_finished` are written by the project's copy of `scripts/util/decorators.py`, not by experiment, and `clear_outdir` deletes them before a rerun, so a job rerun in place is invisible to graphix until it finishes.
- [both] graphix's `DONE` needs `simulation_finished` while experiment counts exit code 0 as finished, so the two can disagree about one job; `job_status` reads `exit.json` keys that no schema class types.
- [both] Both assume every host sees the outdir over NFS; the 2026-11-03 design breaks that on the output side, where graphix reads.

## Open questions
- [experiment] Which of timeout, stall detection, or the `simulation_finished` marker decides success for jobs without a watcher; the merge is the natural point to record the marker in `exit.json` and give both sides one status enum.
- [experiment] Lab messages cut job ids to 8 or 12 characters, mostly the `<user>/` prefix: shorten after the prefix (a 38-line fix in `scheduler.py`)?
- [experiment] Where pushed outputs land and who writes them, once a host without NFS exists (the scheduler runs as a service user, not the owner).
- [merge] Do the on-disk identifiers (`/etc/experiment`, `~/.local/state/experiment`, `/tmp/experiment-<uid>`, `experiment-<owner>.slice`, `~/.cache/graphix`, `EXPERIMENT_*`, `GRAPHIX_CACHE`) follow the rename, cheap now and costly after the lab deployment?
- [merge] Repository strategy: rename `mahyarsamani/experiment` and subtree-import graphix (keeps `git blame` under a prefix), or a new repo and a plain copy (loses blame for 3780 lines)?
- [merge] Should the project's copied `decorators.py` import the marker names from `ungem5`, which needs the venv active in the job?
- [merge] Editable or pinned install per venv once one package serves daemons and analysis; today graphix is editable and experiment pinned.
- [merge] Should `results/jobs.py` read the experiment name from `constructor.py` or `.job/run.json`, or should `gem5Experiment` write an `experiment.json` marker?
- [merge] Is `fetch` through one rpyc socket (about 3.4 GB for 125 runs of 27 MB) enough, with `rsync` printed as the alternative, or should the worker serve files directly?
- [merge] Is graphix ever meant for people outside the lab; if yes, a separate `stats` distribution is the right call.
- [merge] The word: "the helper source" in the chats against `results/jobs.py` in the plan.
- [merge] Phase 4 needs a window with nothing running and a plan for jobs mid-flight; closing PRs #1 to #3 and renaming or archiving the GitHub repos is outward-facing and mine.

## Not done
Each item carries its side and the date it was last mentioned.
- cli: `helper fetch <experiment|job>... --into DIR [--match GLOB]` on the outdir index [experiment, 2026-11-04].
- jobs:
  - SIFT's switchover in `job_defs.py` (`demand` to `Resources(memory=...)`, `SingleSlotBuild` to `gem5Build`), untouched because agents work in SIFT [experiment, 2026-11-04].
  - The first SIFT load from the store must be watched, since discovery listed all of `workloads` as data [experiment, 2026-11-04].
  - `GitResource` with the unpushed-commit check, and `UrlResource` [experiment, 2026-11-04].
  - Porting SIFT's `take_checkpoints.py` and `restore_checkpoints*.py` to `gem5Job` and the `load` form [experiment, 2026-11-03].
  - Code snapshots, which bundles now largely cover (inferred) [experiment, 2026-11-03].
- schedule:
  - Per-job timeouts and batched model predictions [experiment, 2026-11-03].
  - The root-only paths (`systemd-run`, cgroup freeze, slice caps) never ran as root [experiment, 2026-11-04]; the first root test on one host covers launch, pause, resume, `retry --memory` and a partition's slice cap [experiment, 2026-11-03].
  - The lab job-id truncation fix [experiment, 2026-11-04].
  - Pushing outputs from a host without NFS, and per-host outdir bases on local disk [experiment, 2026-11-04].
  - The worker daemon flags (`--status/--stop/--restart`) proposed on 2026-10-27, which the systemd units replace (inferred) [experiment, 2026-10-27].
- run: nothing recorded on either side.
- stats:
  - The tutorial should say that runs before 2026-10-09 label every access `other` [graphix, 2026-10-09].
  - Drop or keep the parked `under_the_rug/{barplot,markers,util}.py` at the move (inferred) [graphix, 2026-11-03].
  - SIFT's `sift_sets.latency_quantile` could switch to graphix's tail-aware quantiles (NaN against `inf` in the overflow) [graphix, 2026-11-04].
- results:
  - `results/fetch.py` [merge, 2026-11-04].
  - One status vocabulary for graphix's `DONE`, `UNFINISHED`, `FAILED`, `RUNNING`, `MISSING` and experiment's `JobStatus` and `resolve_status` [merge, 2026-11-04]; a schema for `exit.json` and `launch.json` [merge, 2026-11-04].
- common: nothing open; the identifier rename, the `_runner()` removal and pandas-free `results/` are done on `ungem5` [merge, 2026-11-04].
- deployment:
  - Review `multiuser`, merge into `main`, push, then update the PhD pointer [experiment, 2026-11-04].
  - Deploy per `deploy/README.md`: root venv in `/opt/experiment`, `/etc/experiment/pki`, worker units, the scheduler unit on liberty, `hosts.py` as admin, then the SIFT scripts [experiment, 2026-11-03]; set up liberty [experiment, 2026-10-28].
  - Review and commit `sift_stats/`, `sift_sets.py`, `wave1-common.md`, `gem5-viz.md` and the `tools/graphix` pointer [graphix, 2026-11-04].
  - Push `redesign` and merge it into `main` [graphix, 2026-11-04]; close graphix PRs #1 to #3 and delete `agent/wp2` [graphix, 2026-11-04].
  - Install graphix (or ungem5) in `.venv/aarch64` [graphix, 2026-11-04].
  - Reinstall both venvs and restart the scheduler and cascade worker on the new version [merge, 2026-11-04]; repoint the parent symlink `../experiment` and `.gitmodules` [merge, 2026-11-04]; archive the GitHub `graphix` repo [merge, 2026-11-04].
- docs:
  - Refresh experiment's memory for the config-file and bundle commits [experiment, 2026-11-04].
  - The thesis script `sift_topdown.py` should use the installed package and `sift_stats.OWNERS` (my file, diff only) [graphix, 2026-11-04]; the thesis `TODO(cite)` for helper and graphix (`notes/review/M1-mote.md`) [graphix, 2026-11-04].
  - The living rules that name the packages: `CLAUDE.md`, `gem5-viz.md`, the `gem5-build`, `gem5-run` and `memory-update` skills, `wave1-common.md`, `context/overview.md` [merge, 2026-11-04].
  - Copy `example_data/` and `examples/sift/data/` by hand from `tools/graphix` (the README fold and the `examples/sift/` move are done) [merge, 2026-11-04].

## Next steps
1. Phase 0, land what is in flight: review and merge `multiuser`, push experiment and then the PhD pointer; review and commit the SIFT side, merge `redesign` into graphix `main` and push; close PRs #1 to #3 and `agent/wp2`; `pytest` (141) and `unittest` (87) pass, and the v5 CSVs reproduce byte for byte with a fresh `GRAPHIX_CACHE`.
2. Phase 1: decide the identifiers and the repository strategy, log them, and promote this file to ungem5's `context/memory.md`.
3. Phase 2 is done on branch `ungem5` (not `merge-graphix`): review its commits (`git log --oneline multiuser..ungem5`).
4. Phase 3: move the callers (SIFT's `sift_sets.py`, `sift_stats/`, `job_defs.py`, `hosts.py`, `port_proxy_watch.py`, the sandbox scripts) and the living rules; the CSVs stay identical, one sandbox job exits 0, and the tutorials run.
5. Phase 4: switch `.gitmodules` and the parent symlink, reinstall both venvs in a quiet window, restart the scheduler and cascade worker, and confirm `sift_sets.load` finds the same 125 v5 runs.
6. Phase 5: `results/fetch.py` and `helper fetch`, preserving mtime; a second fetch transfers zero bytes.
7. Phase 6: archive the graphix repo, log the merge, and refresh memory and overview.
8. Independent of the merge: deploy per `deploy/README.md` and the first root test; the SIFT `job_defs.py` switchover and the checkpoint-script port; per-job timeouts; the lab job-id fix; the thesis script diff.

## Pointers
- Plans: `docs/plans/2026-11-04-ungem5-merge.md` (the merge), `docs/plans/2026-11-03-graphix-improvements.md` (graphix's); the audit: `briefing-room/20261005-181852-graphix-codebase-audit/summary.md`.
- CLI: `ungem5/cli/{helper,initialize,build,run,work,schedule,certs}.py`.
- Jobs: `ungem5/jobs/{work,gem5_work,script,bundle,resources,config_util,settings,markers}.py` and `assets/` (the files `helper initialize` copies).
- Scheduling: `ungem5/schedule/{scheduler,daemon,console,dashboard,filters,state}.py` with `templates/` and `static/`, and `{host,worker,runner,userio,memory,model}.py`; TLS `ungem5/common/pki.py`; `deploy/`.
- Backends: `ungem5/run/{build,simulate}.py`.
- Stats: the core `ungem5/stats/*.py`, `ungem5/stats/stats/`, `ungem5/stats/gem5/`; the job source `ungem5/results/jobs.py`; `fetch.py` is still to come in `results/` and `cli/`.
- Tests: `tests/` (pytest; `gxtest.py` gives the stats tests a temporary cache; `test_imports.py` checks every module and the old names); `examples/sift/` and the gitignored `example_data/`; parked plotting in `docs/parked/graphix-plotting/`.
- Pre-merge trees: `tools/graphix` (untouched) and the `multiuser` branch here; old-to-new paths are in the commit messages on `ungem5`.
- Callers: SIFT's `projects/sift/{job_defs,hosts}.py`, `analysis/sift_sets.py` and `analysis/sift_stats/`; the sandbox `experiment-test/{run_many,run_example}.py` outside the repo; the thesis `workbench/plots/sift_topdown.py`.

## Provenance
The merge plan; experiment's `context/memory.md`, `memory-archive.md` and `CLAUDE.md`; graphix's `README.md`, `docs/plans/2026-11-03-improvements.md`, `pyproject.toml` and source; the 2026-10-05 audit checked against today's code; the extracted experiment and graphix chats (2026-09-24 to 2026-11-04); `context/decisions-log.md`; `git status`, `git branch -vv` and `git log` in both tools.
Shortened for the line limit: experiment's gotchas and its 2026-10-27 decisions are merged into a few lines each, and the merge's open questions are condensed; every Not-done item is kept.
Nothing here is decided beyond what the decisions log records, and "(inferred)" marks what no source states.
