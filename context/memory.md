# ungem5 memory

Last updated: 2026-11-04 (Claude Code, after the merge landed on `main` and the submodule became `tools/ungem5`).

## What this is
`ungem5` is the package that merged `experiment` (the `helper` CLI, gem5 builds and runs, and the scheduler that drives lab hosts over mutual-TLS rpyc) with `graphix` (typed gem5 statistics that select, reduce and export to pandas): everything architecture research needs that is not the simulator, by analogy with the uncore.
Layout option A, decided 2026-11-04: one distribution with the subpackages `cli`, `jobs`, `schedule`, `run`, `stats` (graphix's core unchanged), `results` (the job source and `fetch`) and `common`; pandas is the `stats` extra.
The merge is complete: the tool repo's `main` holds the package, the PhD repo checks it out as `tools/ungem5`, and this file (founded as the union of the two tools' memories) is its one memory.

## Current state
- [repo] `main` is the merged tip (0.26.11.1; `ungem5` and `multiuser` are merged into it and can be deleted); it is 45 commits ahead of `origin/main`, which still is `experiment` 0.26.10.2, so push `tools/ungem5` before the PhD repo and rename the GitHub repo `experiment` to `ungem5` (then fix the pip URL in `deploy/README.md` and the `pyproject` homepage).
- [repo] One pytest suite: 297 passed, 6 skipped without `example_data/`; `test_imports.py` checks every module and refuses the old names.
- [callers] Everything imports `ungem5`: SIFT's analysis (`sift_sets.py`, `sift_stats/`) and drivers (`job_defs.py` on `Resources`, `SingleSlotBuild` gone, every load script compiles with `compile_script`), the sandbox `run_many.py`, the thesis `sift_topdown.py`, and the living rules; the SIFT side (15 files) is uncommitted and awaits review; the ten v5 CSVs reproduced byte for byte.
- [installed] The live venvs are behind: `.venv/x86_64` runs the scheduler and the cascade worker from a pinned `experiment` 0.26.10.2 (GitHub 37210e4) plus an editable `graphix` from `tools/graphix`; `.venv/aarch64` has `experiment` only; so the live scheduler cannot reload any SIFT script until the reinstall, and the analyses run from a scratch venv meanwhile.
- [schedule] Bundles work end to end: two sandbox gem5 jobs ran from the store on cascade after the project directory was moved away; `.job/index.json`, `read_file` on any indexed path and the console command `files <job> [--match GLOB]` are in.
- [schedule] `STATE_VERSION` is 2 and workers, scheduler and consoles must run the same version; a finished run already in an outdir is adopted, not rerun; the root-only paths (`systemd-run`, cgroup freeze, slice caps) ran only against a fake `systemd-run`.
- [schedule] The 2026-11-04 dry run on cascade (isolated ports, runtime dir and PKI) passed every path: mutual TLS, launch, exit codes, dependencies, a memory guess refined by the group estimate, `retry --memory`, `signal kill`, `reload` adopting finished runs, `reset`, `stop` and resume from the state file, the dashboard; the runbook is `docs/palisade-smoke-test.md` with `examples/smoke/jobs.py`.
- [results] `helper fetch <experiment|job>... --into DIR [--match GLOB] [--lab]` (`results/fetch.py`, `cli/fetch.py`) pulls through the scheduler's control socket (new `resolve` command, `job_index` and `read_file` on the control service, owner-or-admin checked), only files whose sha256 changed, atomically, with the index's mtime, into `DIR/<outdir parent>/<outdir name>/`; the defaults are what `results` and `stats` read plus `window.json`; tested on a real worker and control socket, not yet on a lab host.
- [stats] Works: the last dump of `stats.txt` or the largest `stats_dump_<N>.txt`, specs with dims, owner schemes, `Scalar`, `Vector`, `Histogram` and `ControllerCount` with arithmetic and exports, the parsed-dump cache under `~/.cache/ungem5/stats` (`UNGEM5_CACHE`), `workers=N`, `Run.dumps()` and `Run.resets()`, `RunSet.from_samples`, `RunSet.from_experiment`, and `ungem5.stats.gem5.derived`.
- [stats] The three-layer rule holds (`grep -rniE "sift|hov" ungem5/stats` is empty); SIFT's specs, owners and labels live in `projects/sift/analysis/sift_stats/`.
- [stats] Speed on 125 v5 runs (cascade): 93 s cold and 31.5 s warm on one process, 44 s cold on 8.
- [stats] Of the 2026-10-05 graphix audit, the redesign fixed everything but plotting (parked in the PhD repo under `under_the_rug/graphix/`) and the absence of a CLI; `stats.json` was dropped by decision.

## Decisions
- 2026-11-04 [merge]: ungem5 is the mainline: `main` fast-forwarded to the `ungem5` branch, the PhD submodule renamed `tools/ungem5`, `tools/graphix` kept only until its unpushed `redesign` is pushed; the daemons' venvs get a pinned install (a half-edited tree must never reach the next job), so a stats change means a reinstall; the parked plotting code lives in `under_the_rug/graphix/`.
- 2026-11-04 [results]: `fetch` goes through the scheduler (the lab's permission model: workers trust only the scheduler's certificate, and only the scheduler knows a job's owner); `window.json` is a default glob because SIFT's `done` reads it; `--match` adds to the defaults.
- 2026-11-04 [schedule]: the `[scheduler]` section of `config.toml` is the lab scheduler's; a user's own scheduler takes its dashboard port from `--dashboard-port` only.
- 2026-11-04 [both]: `experiment` and `graphix` merge into one package named `ungem5` (free on PyPI; `dynorig`, `outboard` and `archrig` were the alternatives), layout option A, pandas as the `stats` extra; the memory is founded as the union of both tools' memories, and nothing pending is dropped without a dated decision.
- 2026-11-04 [jobs]: the bundle store is per host and user, `<store>/<hostname>/<user>/`, so workers sharing a mount never overwrite each other; a user's worker uses `~/.cache/ungem5/store`, a root worker `[worker] store`.
- 2026-11-04 [jobs]: a bundle's files are found from the top-level run script by following imports as Python resolves them (only directories with `__init__.py` count); string literals naming project paths and relative job arguments become data linked into the working directory; dynamic imports warn and go in `extra_files`.
- 2026-11-04 [jobs]: gem5 experiments bundle by default (`bundle=False` opts out); `GitResource` is dropped from the first cut, so gem5's source and binary stay `PathResource`s.
- 2026-11-04 [schedule]: when a job ends the runner writes `.job/index.json` (path, size, mtime, sha256) and `read_file` serves any indexed path; the push from a host without NFS is settled only when such a host exists.
- 2026-11-03 [jobs]: jobs without a shared filesystem: resources are frozen at `load`, bundles tarred by the console and content-addressed, big inputs stay on NFS, and job ids stay parameter-only with the resource hash as a revision.
- 2026-11-03 [schedule]: service settings live in `/etc/ungem5/config.toml` (then `/etc/experiment`); flags override, and a per-user file covers `[lab]` only.
- 2026-11-03 [schedule] (Claude, open to change): a reload gives non-running jobs their new definition and keeps their status; a restart restores compiled jobs without running scripts; hosts added later reach a script's jobs only on its reload.
- 2026-11-03 [schedule] (same): lab names are `<user>/<name>` and scripts `<user>:<path>`; only admins load scripts that add hosts; only owners and admins read job files; a host with partitions refuses users without a share unless `*` has one.
- 2026-11-03 [schedule] (same): grace is 25% over current use; a host under 3% free memory pauses its newest job and resumes at 6%; the model fits at 50 complete runs, refits every 20, with quantile 0.9 and 95% conformal coverage.
- 2026-11-03 [stats]: three layers: the core knows stock gem5 only, `results/` knows the tool's run layout, and the project owns its specs, stat classes, owners and labels.
- 2026-11-03 [stats]: the API breaks freely when every caller changes in the same commit; the bracket-versus-dot dim convention stays (specs handle both); plotting stays out.
- 2026-11-03 [stats]: a histogram's `mean()` is exact (gem5's sum is kept; `stdev`, `total` and quantiles derive from the buckets); quantiles and CDFs count the tails (`inf` in a tail); a sum warns only for known ratios and rates; a histogram is never filled into a run that printed none of it; empty results keep their columns.
- 2026-10-28 [schedule]: memory is measured with psutil and cgroups, never gem5's `hostMemory`, at a configurable sampling interval (2 minutes suggested); the lab scheduler host is liberty.
- 2026-10-27 [all]: no backward compatibility while in beta: break APIs freely, no keyword-only arguments, shims, or tolerance for old workers or state.
- 2026-10-27 [schedule]: the multi-user plan: resources and memory recording; `load` compiles the script in the console so the scheduler never runs user code; one lab scheduler and a root worker per machine launching via `systemd-run` in per-user slices with uid auth over `SO_PEERCRED`; memory enforcement with grace and a learned model.
- 2026-10-27 [schedule]: gem5 jobs take 1 core; memory is learned per simulation and the user's guess is corroborated, never trusted; strict per-user partitions with a 10% borrowing margin and no preemption; msamani is admin and everyone sees all jobs read-only; only GID 1024023017 (`coe-us-cs-darchr`, by number) may launch; a job past its reservation grows if there is room, else it is frozen, with `on_memory_exceed` (`pause`, `kill`, `retry`) overriding.
- 2026-10-12 [jobs]: builds are jobs (`gem5Build`, pinned per host because `/scr` is local) and simulations depend on them.
- 2026-10-09 [stats]: plotting does not come back for now (stats manipulation and DataFrame export only); `stats.json` and the JSON interface are dropped; only the largest dump is read; Formula stats and gem5's summary lines are not read; stat classes follow gem5's stat kinds with operations suited to each.
- 2026-10-03 [jobs]: job IDs depend on parameters only, never on code.
- 2026-10-01 [repo]: the version is `0.YY.MM.N`, lives only in `pyproject.toml`, and is bumped only on request as its own commit.
- 2026-09-30 [schedule]: mutual TLS over rpyc with a self-managed CA (`helper certs`) and the role in the certificate OU; one scheduler per user per machine, locked in `/tmp/ungem5-<uid>`, with state under `~/.local/state/ungem5/<host>/<name>`; the console attaches to the running daemon; Python 3.12 or newer, `cryptography`, `rpyc>=6`, `pytest` as `dev`.

## Constraints and gotchas
- [schedule] gem5's `hostMemory` is VmSize, not resident memory; lab machines run cgroup v2 and systemd 255, so cgroup freeze works; home is NFS from glacier with `sec=sys`, shared by every lab host, so `~/.config/ungem5/pki` is shared while `~/.local/state/ungem5/<host>/` and `/tmp/ungem5-<uid>` are per host.
- [cli] `helper` finds `project_config.json` by walking up and resolves every path; the build name comes first (`helper build main ...`, `helper run main <script> -- <args>`); `helper run` returns 0 when gem5 panics, so check the log.
- [schedule] Certificates live in `~/.config/ungem5/pki/` (the live daemons still read `~/.config/experiment/pki/`; copy it); `socket.getfqdn()` returns the short name on lab machines, so worker certificates need `--san <fqdn>` or TLS fails as a logged timeout; port 9100 on perle is taken.
- [schedule] `/scr` is local, so the gem5 binary must exist on every worker; workers are stateless, so deleting a running job's outdir marks it failed; a job runs in its `cwd`, not its outdir, so commands name output files by absolute path.
- [schedule] Logs are in `~/.local/state/ungem5/<host>/<name>/`; `UNGEM5_RUNTIME_DIR` overrides the runtime directory for tests but must stay short (a Unix socket path is at most 108 bytes); the dashboard on localhost:9200 is reached by SSH tunnel, and `info` prints its URL and token.
- [repo] A non-editable install does not pick up `git pull`; the PhD venv has no pytest, so tests run from a scratch venv with `pip install -e ".[dev,stats,model]"`; `setsid nohup` breaks `$!`, so find test workers by port and kill by PID, never `pkill -f`.
- [schedule] Python 3.12 `argparse` raises `SystemExit` even with `exit_on_error=False`; the sandbox `run_example.py` needs a power-of-two generator count, so `run_many.py` fails for 3, 5, 6 and 7.
- [schedule] Job success is the exit code unless a watcher says otherwise, so a hung gem5 without a watcher holds its slot.
- [stats] The cache is keyed by path, size, mtime and `PARSER_VERSION`, so a copy that does not preserve mtime re-parses once (`fetch` preserves it); `cache=False` or `UNGEM5_CACHE=off` bypasses it.
- [stats] `config.json` zero-pads SimObject vectors of ten or more (`system_links01`) but stats do not; a value wider than 12 characters glues to the bar; unnamed groups print one name several times (`occurrence`); `nozero` stats vanish and are filled as zero; one name on several classes raises `AmbiguousStatError`.
- [results] `from_experiment` labels a run `experiment = job_dir.parent.name`, equal to the experiment name by convention only; `find_jobs` counts any directory with `.job/` or `params.json`, so `helper run --outdir X` runs appear only if the script wrote `params.json`; `run_se.py`'s experiment names (`se-v5-<arm>`) differ from the v5 outdirs (`se-v5-<arm>-v5`), so a load of today's script would not adopt those runs.
- [results] `params.json` and `simulation_finished` are written by the project's copy of `scripts/util/decorators.py`, not by the runner, and `clear_outdir` deletes them before a rerun, so a job rerun in place is invisible to the stats side until it finishes; the stats side's `DONE` needs `simulation_finished` while the scheduler counts exit code 0 as finished, so the two can disagree about one job.
- [hosts] palisade (x86_64, 8 cores, 31 GB, no `/scr/msamani`, nothing of ours running) is the smoke-test host; from cascade it is reachable only with the `experiment_key` identity.

## Open questions
- [schedule] Which of timeout, stall detection, or the `simulation_finished` marker decides success for jobs without a watcher; the natural fix records the marker in `exit.json` and gives both sides one status enum.
- [schedule] Lab messages cut job ids to 8 or 12 characters, mostly the `<user>/` prefix: shorten after the prefix (a 38-line fix in `scheduler.py`)?
- [schedule] Where pushed outputs land and who writes them, once a host without NFS exists (the scheduler runs as a service user, not the owner).
- [jobs] Should the project's copied `decorators.py` import the marker names from `ungem5`, which needs the venv active in the job?
- [results] Should `results/jobs.py` read the experiment name from `constructor.py` or `.job/run.json`, or should `gem5Experiment` write an `experiment.json` marker?
- [results] Is `fetch` through the scheduler (every byte crosses worker to scheduler, then scheduler to console; about 3.4 GB for 125 runs of 27 MB) enough, with `rsync` printed as the alternative, or should the worker serve files directly; and the local copy is hashed in full on every fetch, where size plus mtime would do.
- [results] Does `fetch` need a flag to pull only the `--match` globs, without the defaults?
- [stats] Is the stats side ever meant for people outside the lab; if yes, a separate `stats` distribution is the right call.
- [jobs] SIFT's `hosts.py` still carries the old slot counts as `cores` (cascade, nugget and perle 1), so a build and a simulation never run side by side there; pass the real counts or `None` once memory is the limit?

## Not done
Each item carries its side and the date it was last mentioned.
- jobs:
  - The first SIFT load from the store must be watched, since discovery listed all of `workloads` as data [2026-11-04].
  - `GitResource` with the unpushed-commit check, and `UrlResource` [2026-11-04].
  - `gem5Build` stores `_threads` and never reads it; the load scripts use the private `_get_project_config`, which wants a public name [2026-11-04].
- schedule:
  - Per-job timeouts and batched model predictions [2026-11-03].
  - The root-only paths (`systemd-run`, cgroup freeze, slice caps) never ran as root; the first root test on one host covers launch, pause, resume, `retry --memory` and a partition's slice cap [2026-11-04].
  - The lab job-id truncation fix [2026-11-04].
  - Pushing outputs from a host without NFS, and per-host outdir bases on local disk [2026-11-04].
- stats:
  - The tutorial should say that runs before 2026-10-09 label every access `other` [2026-10-09].
  - Drop or keep the parked plotting code in `under_the_rug/graphix/` [2026-11-04].
  - SIFT's `sift_sets.latency_quantile` could switch to the tail-aware quantiles (NaN against `inf` in the overflow) [2026-11-04].
- results:
  - The plan's phase-5 check on a real sandbox experiment: fetch it from a lab host, compare a scalar with the NFS outdir's, and time it [2026-11-04].
  - One status vocabulary for `DONE`, `UNFINISHED`, `FAILED`, `RUNNING`, `MISSING` and the scheduler's `JobStatus` and `resolve_status`; a schema for `exit.json` and `launch.json` [2026-11-04].
- deployment (Mahyar's):
  - Push `tools/ungem5` before the PhD repo; rename the GitHub repo to `ungem5` and fix the pip URL in `deploy/README.md` and the `pyproject` homepage [2026-11-04].
  - Push graphix's `redesign` (five unpushed commits), then `git rm tools/graphix`, archive the GitHub repo, close PRs #1 to #3 and delete `agent/wp2` [2026-11-04].
  - Review and commit the SIFT diff (15 files) and the PhD-repo rule edits (`CLAUDE.md`, `context/overview.md`, the `visualization-rules` skill) [2026-11-04].
  - In a quiet window: `pip uninstall experiment graphix` and a pinned `pip install tools/ungem5[stats]` in `.venv/x86_64` and `.venv/aarch64`, copy the PKI to `~/.config/ungem5/pki`, restart the scheduler and the cascade worker (state starts fresh under `~/.local/state/ungem5`; reloading the SIFT scripts adopts finished runs), and confirm `sift_sets.load` finds the same 125 v5 runs [2026-11-04].
  - The palisade smoke test per `docs/palisade-smoke-test.md` [2026-11-04].
  - The lab deployment per `deploy/README.md`: root venv in `/opt/ungem5`, `/etc/ungem5/pki`, worker units, the scheduler unit on liberty, `hosts.py` as admin, then the SIFT scripts [2026-11-03].
- docs:
  - The thesis `TODO(cite)` for helper and graphix (`notes/review/M1-mote.md`) [2026-11-04].
  - Copy `example_data/` and `examples/sift/data/` by hand from `tools/graphix` before it is removed [2026-11-04].

## Next steps
1. Mahyar: push `tools/ungem5`, then the PhD repo; rename the GitHub repo and fix the two URLs; push graphix's `redesign`, remove `tools/graphix`, archive graphix and close its PRs.
2. Mahyar: review and commit the SIFT diff and the PhD-repo rule edits.
3. Quiet window: reinstall both venvs (pinned), copy the PKI, restart the scheduler and the cascade worker, reload the SIFT scripts and confirm the 125 v5 runs are adopted.
4. The palisade smoke test from the runbook, then the phase-5 check of `fetch` on a real experiment.
5. The lab deployment per `deploy/README.md` and the first root test on one host.
6. Independent: per-job timeouts, the lab job-id fix, the status vocabulary and the `exit.json` schema, `GitResource`.

## Pointers
- Plans: `docs/plans/2026-11-04-ungem5-merge.md` (the merge), `docs/plans/2026-11-03-graphix-improvements.md` (graphix's); the runbook `docs/palisade-smoke-test.md`; the audit: `briefing-room/20261005-181852-graphix-codebase-audit/summary.md`.
- CLI: `ungem5/cli/{helper,initialize,build,run,work,schedule,certs,fetch}.py`.
- Jobs: `ungem5/jobs/{work,gem5_work,script,bundle,resources,config_util,settings,markers}.py` and `assets/` (the files `helper initialize` copies).
- Scheduling: `ungem5/schedule/{scheduler,daemon,console,dashboard,filters,state}.py` with `templates/` and `static/`, and `{host,worker,runner,userio,memory,model}.py`; TLS `ungem5/common/pki.py`; `deploy/`.
- Backends: `ungem5/run/{build,simulate}.py`.
- Stats: the core `ungem5/stats/*.py`, `ungem5/stats/stats/`, `ungem5/stats/gem5/`; the job source `ungem5/results/jobs.py`; fetching in `ungem5/results/fetch.py` and `ungem5/cli/fetch.py`.
- Tests: `tests/` (pytest; `gxtest.py` gives the stats tests a temporary cache; `test_imports.py` checks every module and the old names); `examples/sift/` and the gitignored `example_data/`; `examples/smoke/jobs.py`.
- Pre-merge trees: `tools/graphix` (until its push) and the branches `multiuser` and `ungem5` here; old-to-new paths are in the commit messages between 599f6a1 and 476ea39; the parked plotting code is in the PhD repo under `under_the_rug/graphix/`.
- Callers: SIFT's `projects/sift/{job_defs,hosts,run_se,restore_*,take_*}.py`, `analysis/sift_sets.py` and `analysis/sift_stats/`; the sandbox `experiment-test/{run_many,run_example}.py` outside the repo; the thesis `workbench/plots/sift_topdown.py`.

## Provenance
The 2026-11-04 union memory, the merge plan, `context/decisions-log.md`, and the 2026-11-04 session that finished phases 3 to 6: the two agent reports (the SIFT switchover and `helper fetch`), the cascade dry run, `git log` on `main`, and `pytest`.
Superseded state (branch and version bookkeeping of the two tools, the open questions the merge answered, the old Next steps) is in `memory-archive.md` under 2026-11-04.
