# Deploying the lab scheduler

One scheduler serves the whole lab from liberty, and every machine runs one worker as root.
Members of `coe-us-cs-darchr` (gid 1024023017) load their own scripts, see everyone's jobs, and change only their own.
Admins (`admins` in the config file) also change hosts, limits and partitions.
Each job runs as the user who loaded it, in its own systemd unit inside `experiment-<user>.slice`.

## How the pieces trust each other

- A user's console talks to the scheduler over `/run/experiment/scheduler.sock` on liberty; the kernel tells the scheduler which user is on the other end.
- The console runs the user's load script itself, as that user, and sends the scheduler plain data, so the scheduler never runs anyone's code.
- The scheduler talks to the workers over mutual TLS, as before; a worker accepts only the scheduler's certificate.
- A worker launches a job only for an owner whose uid is at least 1000 and who is in group 1024023017.
- A worker never touches a user's files as root: a helper process, running as the owner, reads and writes them, so permissions and NFS root squash apply as they would to the owner.

## Install (once per machine, as root)

```bash
mkdir -p /etc/experiment
cp deploy/config.toml /etc/experiment/config.toml   # ports, groups, admins
python3 -m venv /opt/experiment/venv
/opt/experiment/venv/bin/pip install "experiment[model] @ git+https://github.com/mahyarsamani/experiment.git@<commit>"
```

The `model` extra (scikit-learn) gives the scheduler a learned memory estimate once 50 runs have finished; the workers don't need it.

Root must never run code from a directory a user can write to, so the venv lives in `/opt`, owned by root.
Install the same commit on every machine and in the venvs users run their consoles from, because the console and the scheduler exchange compiled scripts.

## Certificates

Create them where the CA lives (`helper certs init` once, if there is no CA yet):

```bash
helper certs issue scheduler
helper certs issue worker <host> --san <host's fqdn>   # for every host
```

Then copy them to `/etc/experiment/pki/` on each machine:

| Machine | Files | Owner | Mode |
|---|---|---|---|
| liberty | `ca.crt`, `scheduler.crt`, `scheduler.key` | `experiment` | 0600 for the key |
| every worker | `ca.crt`, `<host>.crt`, `<host>.key` | `root` | 0600 for the key |

## Workers (every machine, as root)

```bash
cp deploy/experiment-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now experiment-worker
```

The worker listens on the `[worker] port` of the config file, 9100 by default.
On perle another process holds 9100: set another port in its config file, and give the same `port=` to that `Host` in `hosts.py`.

## The scheduler (liberty, as root)

```bash
useradd --system --home-dir /var/lib/experiment --shell /usr/sbin/nologin experiment
cp deploy/experiment-scheduler.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now experiment-scheduler
```

The state, the memory history and the logs live in `/var/lib/experiment/experiment/liberty/lab/`.

## First steps (an admin, on liberty)

```bash
helper console --lab
lab> load projects/sift/hosts.py
lab> partition perle <user> --cores 16 --memory 128G
lab> partition perle '*' --cores 4 --memory 32G
```

Only an admin may load a script that adds hosts.
Once a host has partitions, a user can run there only within their own share or the `*` share.
They may go 10% past their share while other users leave theirs idle; nothing is preempted.
After an admin adds a host, users `reload` their scripts so their jobs can run on it.

## Using it (every user)

```bash
ssh -L 9200:localhost:9200 liberty
helper console --lab
lab> info              # the dashboard URL with your own token
lab> load my_jobs.py
```

Names are per user: your jobs are `<you>/<id>`, and a short id without a `/` means one of your own.
The dashboard shows everyone's jobs; it acts as you, and shows job files only to their owner and admins.

## Known limits

- A job runs with systemd's environment for its user (`HOME`, `USER`, `SHELL` and a default `PATH`), not the environment of the shell that loaded it; commands must name their programs by absolute path, as gem5 jobs do.
- Scripts are compiled on liberty, where `/scr` of other hosts is absent; gem5 jobs then learn memory without the build options recorded next to the binary.
