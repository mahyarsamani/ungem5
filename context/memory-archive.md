# experiment memory archive

Older or superseded material, kept for the record.
Same sections as `memory.md`, no line limit, newest first within a section.

## What this is
- Before 2026-09-30 the package was `setup.py`-based, with a Flask file server per worker (port 9101), unauthenticated rpyc on all interfaces, polling without persistence, and `gem5FSSimulation` as the job class.

## Current state (historical snapshots)
- 2026-10-05: version 0.26.09.1 at 45ae855; local `main` still tracked `origin/master` after the default branch was renamed to `main` (since repointed; `master` is gone from GitHub).
- 2026-10-05: the scheduler on cascade was stopped with nothing queued; the old `env/aarch64` venv had 45ae855 and the old `env/x86_64` had 620b7db, so hosts could disagree.
- 2026-10-02: verified end to end with a worker on perle (port 9360) and the scheduler on cascade; all `experiment-test/run_many.py` jobs launched, and the 4 of 8 that failed used a non-power-of-two core count.
- 2026-09-30: the rewrite replaced the scheduler, worker, host, console, and daemon code: durable state, reattach after a restart, mutual TLS, a Unix-socket control channel, a token-protected dashboard, and 35 tests.
- Commit 3a0ff10 has neither `setup.py` nor `pyproject.toml`, so that single commit does not install.
- 2026-10-03: a plan for build-before-simulate dependencies and content-addressed code snapshots (`experiment/api/snapshot.py`, build provenance written by `helper build`, `reload` moving queued jobs to a new snapshot) was drafted and then cancelled as too complicated.
- 2026-10-01: first deployment to perle failed twice, on the certificate name and on port 9100; fixed by `--san` support and a busy-port message.
- 2026-09-30: versions went 0.27.0 (reverted) to 0.26.09.30 to 0.26.09.1 as the scheme settled.
- 2026-01-05 to 2026-01-07: `cascade.scheduler.log` in the repo records SIFT checkpoint-restore jobs launched and killed on cascade with the old scheduler.
- 2026-01-29: Gemini Antigravity's code tracker shows edits to `build.py` and `run.py`, the only trace of the tool in the Gemini history.

## Decisions
- 2026-10-03 (superseded 2026-10-12 for builds): dependency graphs and code snapshotting were postponed and gem5 built by hand; job IDs depend on parameters only, never on code (still true).
- 2026-09-30: script compatibility was allowed to break: no file-server port in `Host`, `domain` must match a certificate SAN, `gem5FSSimulation` became `gem5Job`, `aux_file_io` became `aux_files`, `optional_dump` became `dumps`, job IDs changed.
- 2026-09-30: the Flask file server and its second port were removed; `requests` was dropped.
- 2026-09-30: warnings print without the source line; gem5-side decorators format their own warnings.
- 2026-09-30: tests are the lowest priority, but they exist and pass.
- 2026-09-24: full code review found the kill-experiment bug, host-dropping on any failure, hash collisions, unauthenticated rpyc on all interfaces, and no persistence; all addressed by the rewrite.
- 2026-10-03 (cancelled with the plan): build per host and pin simulations to it, because `/scr` is per machine; dependents of a failed job wait as "blocked"; freeze gem5 by content (commit, diff, untracked files) at load.
- 2026-09-30: the version scheme was first `0.YY.MM.DD`, changed the next day to `0.YY.MM.N`.
- 2026-09-30: `stop --kill-jobs` keeps experiments so they can be reset; no status toolbar in the console.
- 2026-09-30: on restart, check pid status so running jobs are not relaunched; job truth lives on disk in each outdir.

## Constraints and gotchas
- `psutil.Process(-1)` raises `ValueError`, not a `psutil.Error`.
- If a scheduler tick exceeded the poll interval, console commands were never processed (fixed in 0f17708).
- Old workers' jobs are invisible to the new scheduler after the rewrite.
- SSH from cascade to perle stops at host-key verification the first time.
- The sandbox `experiment-test` uses build `main` with `gem5.fast` from gem5 `stable`, outputs under the gem5 output directory `exp-test/linear-sweep/<job id>/`.

## Open questions (historical)
- Should `master` be deleted on GitHub? (Answered: it is gone.)
- Snapshot design, if revisited: freeze gem5 by content at load, declared globs for project code; the cancelled plan is `let-s-try-to-improve-piped-stream.md` in the Claude plans directory.
- Keep a `setup.py` shim for old installers? (Offered, never requested.)

## Next steps (historical)
- From the 2026-10-02 discussion: per-job wall-clock timeout, stall detection, and a `simulation_finished` marker were proposed; none chosen.

## Pointers
- The 2026-09-24 code review and the 2026-09-30 plan live only in the Claude transcript for the old `experiment` project folder.

## Provenance
Same sources as `memory.md`.

## 2026-11-04: experiment's `memory.md` before the merge into ungem5
On 2026-11-04 the union memory (`context/ungem5-memory.md`) replaced experiment's `context/memory.md`.
These are the parts of the replaced file, as last edited on 2026-11-03, that the union does not carry; file paths are the pre-merge ones (`experiment/api/`, `experiment/common/`).

### Current state (2026-11-03)
- Local `main` had one unpushed commit, 93f5c81, and the PhD repo already pointed at it, so `tools/experiment` had to be pushed before the PhD repo.
- Since 2026-10-05 `main` had gained host ISAs, job watchers (`fail`/`retry`), delete/undelete, filters, a grouped dashboard, job dependencies, `PerHostJob`, `gem5Build`, and the build/run backends behind `helper build`/`helper run`.
- `multiuser`, phase by phase:
  - Phase 1 (9c6931e): `Resources(cores, memory)` replaced `demand`; nice and process-tree RSS sampling in the runner; a SQLite memory history next to the state; placement by cores, host memory limit (default 90% of RAM) and free memory.
  - Phase 2 (9a9b717): the console compiles load scripts in a fresh `python -B` subprocess and sends data; the scheduler holds `LoadedJob`s, saves the compiled data in its state and never runs user code.
  - Phase 3 (ff87749): job owners, root workers (`--allow-gid`) launching via `systemd-run` with per-user `userio` helpers, the lab scheduler (`--lab GID --admin U --socket P`), per-user namespaces and dashboard tokens, partitions, adoption of runs that already ended, and `deploy/`.
  - Phase 4 (fb9cd28, dcf0e61): grace, PAUSED, `on_memory_exceed`, `resume`, `retry --memory`, low-host-memory pausing, per-user slice `MemoryMax`, and the learned model (the `model` extra).
- Tested then: unit tests, plus end-to-end runs on cascade of a per-user and a lab-mode daemon with a user-mode worker.
- Not done then: per-job timeouts, code snapshots, batched model predictions.

### Decisions (detail the union condenses)
- 2026-11-03: the store path was first written `<base>/<hostname>/<user>/store/<sha256>`; git commits were to be verified to exist on the remote at `load` (Claude's recommendation).
- 2026-11-03: co-occupancy and host are recorded but are not model features.
- 2026-10-27: memory is learned per simulation from the gem5 source identity, build options, script, inputs, host, and co-occupancy.
- 2026-10-27: the multi-user plan's phase 3 named one lab scheduler on liberty and Unix-uid auth over `SO_PEERCRED`.
- 2026-10-12: builds as jobs revived part of the cancelled 2026-10-03 plan, without code snapshots.
- 2026-10-01: the version is beta, with a monthly counter from 1, bumped as its own "Updating version number." commit.
- 2026-09-30: certificates come from `helper certs init` and `helper certs issue scheduler|worker <name> [--san]`.
- 2026-09-30: `helper schedule` starts or attaches, `helper console` reattaches, Ctrl-D detaches; commands take names directly.

### Constraints and gotchas (detail the union condenses)
- `helper work --name <n>` selects `<n>.crt/.key` and defaults to the hostname.
- On perle, use another port than 9100 and the same `port=` in `Host(...)`.
- Logs were `~/.local/state/experiment/<host>/<name>/{scheduler,dashboard,control}.log` and `daemon.out`.
- Reach the dashboard with `ssh -L 9200:localhost:9200`.
- The ARM hosts need an aarch64 venv.

### Next steps (2026-11-03)
1. The config file, then the resources design (bundles, `GitResource`, `PathResource`, the per-host store, outdir indexes and `fetch`), both on `multiuser`.
2. Review the `multiuser` commits; merge into `main` and push when satisfied.
3. Deploy per `deploy/README.md`: root venv in `/opt/experiment` on every host, certificates in `/etc/experiment/pki`, worker units, then the scheduler unit on liberty; load `hosts.py` as admin, then the SIFT scripts (runs that already ended are adopted, not rerun).
4. First root test on one host: launch, pause and resume, `retry --memory`, and a partition's slice cap.
5. Port SIFT's `take_checkpoints.py` and `restore_checkpoints*.py` to `gem5Job` and the `load` script form.

### Pointers (2026-11-03)
- CLI `experiment/cli/{helper,initialize,build,run,work,schedule,certs}.py`; scheduler `experiment/api/scheduler/{scheduler,daemon,console,dashboard,state}.py`; worker `experiment/api/{worker,runner,host,work}.py`; TLS `experiment/common/pki.py`.
- Resources, memory history and model `experiment/api/{resources,memory,model}.py`; script compilation `experiment/api/script.py`; file work as the owner `experiment/api/userio.py`; backends `experiment/api/{build,simulate}.py`; job classes `experiment/common/gem5_work.py`; project config `experiment/common/config_util.py`.

## 2026-11-04: superseded by the merge landing on `main`
Moved from `memory.md` when `tools/experiment` became `tools/ungem5`; the facts below were true during the merge and are kept for the record.

### Current state (superseded)
- [merge] Branch `ungem5` (local, from `multiuser` at 599f6a1) held the work: graphix subtree-imported from `redesign` (848d959) and moved into `ungem5/stats/` and `ungem5/results/`, experiment moved into `ungem5/{cli,jobs,schedule,run,common}/`, the on-disk names renamed (`/etc/ungem5`, `~/.local/state/ungem5`, `/tmp/ungem5-<uid>`, `~/.cache/ungem5/{store,stats}`, `ungem5-<owner>.slice`, `UNGEM5_*`), one pytest suite (287 passed, 6 skipped without `example_data/`), version 0.26.11.1; `main` was fast-forwarded to it on 2026-11-04.
- [merge] Done on `ungem5`: `results/jobs.py` imports the runner's names directly (no `_runner()`, no `HAVE_EXPERIMENT`); the marker names live in `ungem5/jobs/markers.py`, spelled out in the copied `decorators.py` and checked by a test; `ungem5.results` and `import ungem5.stats` need neither pandas nor the scheduler; graphix's README is folded into `README.md`; `examples/sift/` moved.
- [experiment] Version 0.26.10.2; `main` (93f5c81, `gem5Build` demand separate from build threads) was one commit ahead of `origin/main`.
- [experiment] Branch `multiuser` had eight commits over `main`: resources and memory placement, scripts compiled in the console, the lab scheduler and root workers, memory enforcement, the learned model, `/etc/experiment/config.toml`, bundles with the per-host store and the outdir index, and a bundle fix; 141 tests passed.
- [experiment] experiment's own `context/memory.md` (as last edited 2026-11-03) was replaced by the union memory; its unique parts are above under 2026-11-04.
- [graphix] Version 0.2.0, Python 3.10 or newer, pandas the only dependency, no CLI; 3780 lines in `graphix/{parse,config,names,specs,records,run,cache,frame,owners}.py`, `stats/`, `gem5/` and `sources/helper.py`; 87 unittest tests.
- [graphix] Branch `redesign` (checked out in `tools/graphix`, no upstream) holds the whole redesign and the five commits of 2026-11-03/04 (66f2fcf to 848d959); `main` (8644275, "Snapshot before redesign") predates it.
- [graphix] Installed editable in `.venv/x86_64` only; `.venv/aarch64` had neither graphix nor pandas.
- [graphix] The `helper` source imported experiment's file names from `experiment.api.runner` so the merge would be easiest (decision 2026-11-03).

### Open questions answered by the merge
- The on-disk identifiers followed the rename (`/etc/ungem5`, `~/.local/state/ungem5`, `/tmp/ungem5-<uid>`, `ungem5-<owner>.slice`, `~/.cache/ungem5`, `UNGEM5_*`), done in phase 2.
- Repository strategy: the `experiment` repo was kept and graphix subtree-imported (so `git blame` survives under a prefix); the GitHub rename to `ungem5` is Mahyar's.
- Editable or pinned per venv: pinned for the venvs that run daemons; editable only in scratch venvs.
- "The helper source" of the chats is `results/jobs.py`.
- Phase 4's quiet window: the git side was done without one (the live daemons run a pinned install, untouched by the rename); the venv reinstall and restart still need one.

### Not done items closed on 2026-11-04
- SIFT's switchover in `job_defs.py` (`demand` to `Resources`, `SingleSlotBuild` deleted since `gem5Build` already counts resources, not threads); the checkpoint and restore scripts were already load scripts and compile.
- `helper fetch` on the index (phase 5).
- Review and merge `multiuser` (folded into the fast-forward); refresh experiment's memory for the config-file and bundle commits (this file).
- The living rules that name the packages: `CLAUDE.md`, `gem5-viz.md`, the `visualization-rules` skill, `wave1-common.md`, `context/overview.md`; the `gem5-build`, `gem5-run` and `memory-update` skills never named them.
- The thesis script `sift_topdown.py` imports the installed package (phase 3).
- The parent symlink `../experiment` turned out not to exist, so there was nothing to repoint.

### Next steps (historical, the merge plan's phases)
1. Phase 0: land what is in flight (merged into the fast-forward instead; the pushes remain).
2. Phase 1: the name `ungem5`, layout option A, the union memory (2026-11-04).
3. Phase 2: the package on branch `ungem5` (2026-11-04).
4. Phase 3: move the callers and the living rules; the CSVs stayed identical (2026-11-04).
5. Phase 4: `.gitmodules` and the submodule rename done; the venv reinstall and daemon restart remain.
6. Phase 5: `helper fetch` (2026-11-04); the check on a real experiment remains.
7. Phase 6: the decision is logged and the memories refreshed; archiving graphix and the pushes remain.
