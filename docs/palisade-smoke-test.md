# Smoke test of the ungem5 scheduler on palisade

A single-user run of the new scheduler and one worker on palisade, before `ungem5` becomes the mainline.
Dry-run on cascade on 2026-11-04 from the `ungem5` branch (`476ea39`), isolated from the live `experiment` daemons: everything below passed there.
palisade: x86_64, 8 cores, 31 GB, Python 3.12.3, home on NFS from glacier like cascade, no `/scr/msamani`, nothing of ours running.

## What is shared and what is not

The home directory is shared with cascade, so:
- `~/.config/ungem5/pki` is shared: issue certificates once, from any host.
- `~/.local/state/ungem5/<host>/<name>` is keyed by host: palisade's state never touches cascade's.
- `/tmp/ungem5-<uid>` (lock and control socket) is local to each host.
- `.venv/x86_64` is the venv of the live cascade daemons (`experiment` and `graphix`): do not install into it. Use a separate venv for this test.

## 1. A venv for the test (on palisade)

```bash
cd ~/darchr/phd
python3 -m venv ~/darchr/experiment-test/venv-ungem5
source ~/darchr/experiment-test/venv-ungem5/bin/activate
pip install -e "tools/experiment[dev,stats,model]"
cd tools/experiment && pytest -q      # 287 passed, 6 skipped on cascade
```

## 2. Certificates

Reuse the lab CA so the same certificates serve the old and new daemons during the switchover:

```bash
mkdir -p ~/.config/ungem5
cp -a ~/.config/experiment/pki ~/.config/ungem5/pki
helper certs issue worker palisade --san palisade.idav.ucdavis.edu
helper certs list
```

`palisade.crt` is then valid for `palisade` and the FQDN; the smoke script reaches the worker at `palisade` (`platform.node()`).
If `getent hosts palisade` fails on palisade itself, add `--san localhost` and run the console with `UNGEM5_SMOKE_DOMAIN=localhost`.

## 3. The worker (tmux window 1)

```bash
helper work --name palisade
```

Expected: `Worker listening on 0.0.0.0:9100 (mutual TLS)`.
Without `/etc/ungem5/config.toml` the defaults apply (port 9100, bind `0.0.0.0`); the worker is not root, so it keeps its store in `~/.cache/ungem5/store`.

## 4. The scheduler and the smoke jobs (tmux window 2)

```bash
helper schedule --name palisade
```

The dashboard port comes from `--dashboard-port` (default 9200), not from a config file: the `[scheduler]` section of `config.toml` is the lab scheduler's.
In the console:

```
palisade> load tools/experiment/examples/smoke/jobs.py
palisade> hosts
palisade> jobs --full
```

Expected: `added host palisade`, `added experiment smoke`, `smoke: 5 new job(s)`; the host goes from `down` to `up` within a few seconds and the jobs run two at a time (`cores=2`).
After about ten seconds:

| job | status | rc | note |
|---|---|---|---|
| `ok` | exited | 0 | `result.txt` holds `hello` |
| `fails` | failed | 3 | |
| `mem` | exited | 0 | reserved 1.2G from the 1G guess; peak about 211M |
| `after-ok` | exited | 0 | ran after `ok`; copies its result |
| `long` | running | | `sleep 600`, to kill |

Then exercise the rest:

```
palisade> files ok                    # .job/{launch,process,exit,files}.json, stdout, stderr
palisade> memory mem                  # guess, estimate and the past run
palisade> retry mem --memory 2G       # queued again with at least 2.0G
palisade> signal kill long -y         # long: sent kill; status killed
palisade> reload tools/experiment/examples/smoke/jobs.py   # 0 new, 5 updated; finished runs are adopted, not rerun
palisade> reset --experiment smoke -y # requeues the finished ones; refuses a running job
palisade> info                        # dashboard URL with token, state file, logs
palisade> stop                        # jobs keep running
```

```bash
helper schedule --name palisade       # resumes from the state file: same hosts and jobs
```

Dashboard: `ssh -L 9200:localhost:9200 palisade.idav.ucdavis.edu`, then open the URL from `info` (a 302 to set the token cookie is normal).

## 5. Clean up

```
palisade> stop --kill-jobs
```

Ctrl-C the worker in window 1.
`helper schedule --name palisade --fresh` archives the state when you want to start over.

## Observations from the cascade dry run

- A job runs in its `cwd`, not in its outdir: name output files by absolute path, as the gem5 jobs do. The smoke script sets `OUTDIR` for that.
- The memory reservation learns at once: the second plain job was reserved 243M "(group)" after the first finished with a peak of 18M.
- `helper schedule` ignores `[scheduler] dashboard_port` in a config file; pass the flag when 9200 is taken.
- The CA is still named `experiment CA` in `ungem5/common/pki.py`; cosmetic, since the lab CA already exists.
- The control socket needs a short path: `/tmp/ungem5-<uid>` is fine; `UNGEM5_RUNTIME_DIR` under a deep directory fails with `AF_UNIX path too long`.
