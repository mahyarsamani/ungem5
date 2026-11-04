"""Bundles: the Python a job runs, found from its script, frozen at load,
and run on the worker from its store."""

import base64
import json
import os
import time

from pathlib import Path

import pytest

from experiment.api.bundle import BundleError, discover, pack, unpack
from experiment.api.scheduler.scheduler import CommandError, Scheduler, current_user
from experiment.api.scheduler.state import StateStore
from experiment.api.script import compile_objects
from experiment.api.userio import LocalIO, resolve_status, workdir
from experiment.api.worker import Worker
from experiment.common.gem5_work import gem5Experiment, gem5Job

from test_scheduler import FakeRoot, make_host, run

ME = current_user()


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def project(tmp_path):
    """A project like SIFT's: a run script in scripts/ that imports from
    scripts/util and, through the root, from components and workloads."""
    root = tmp_path / "project"
    write(root, {
        "scripts/run.py": (
            "import json, os\n"
            "from util.helpers import helper\n"
            "import components.boards.board\n"
            "from components import modifier\n"
            "import gem5.isas\n"
            "import workloads\n"
            "import importlib\n"
            "importlib.import_module('plugins.' + os.environ['X'])\n"
            "SYSROOT = 'workloads/sysroots'\n"
        ),
        "scripts/util/__init__.py": "",
        "scripts/util/helpers.py": "def helper(): pass\n",
        "scripts/unused.py": "",
        "components/__init__.py": "",
        "components/modifier.py": "",
        "components/boards/__init__.py": "",
        "components/boards/board.py": (
            "from .shared import x\n"
            "TABLE = 'table.json'\n"
        ),
        "components/boards/shared.py": "x = 1\n",
        "components/boards/table.json": "{}\n",
        "workloads/__init__.py": "",
        "workloads/sysroots/lib.so": "binary",
        "plugins/extra.py": "",
        # NOTE: Like the gem5 source tree: no __init__.py, so not a package.
        "gem5/isas.py": "",
    })
    return root


def test_discover_follows_imports_like_python(project):
    found = discover(project / "scripts" / "run.py", project)
    assert found.files == [
        "components/__init__.py",
        "components/boards/__init__.py",
        "components/boards/board.py",
        "components/boards/shared.py",
        "components/modifier.py",
        "scripts/run.py",
        "scripts/util/__init__.py",
        "scripts/util/helpers.py",
        "workloads/__init__.py",
    ]
    # NOTE: Literals that name paths are data: by the root, or next to the
    # module that names them.
    assert found.data == ["components/boards/table.json", "workloads/sysroots"]
    assert any("dynamically" in w for w in found.warnings)
    with_extra = discover(
        project / "scripts" / "run.py", project, [project / "plugins" / "extra.py"]
    )
    assert "plugins/extra.py" in with_extra.files
    with pytest.raises(BundleError, match="not under"):
        discover(project.parent / "elsewhere.py", project)


def test_packing_is_the_same_bytes_and_unpacking_checks_the_id(project, tmp_path):
    found = discover(project / "scripts" / "run.py", project)
    assert pack(found) == pack(found)
    (project / "components" / "modifier.py").write_text("changed = 1\n")
    changed = discover(project / "scripts" / "run.py", project)
    assert changed.id != found.id
    assert unpack(pack(changed), changed.id, tmp_path / "a") == changed.files
    with pytest.raises(BundleError, match="doesn't match"):
        unpack(pack(changed), found.id, tmp_path / "b")


def test_a_working_directory_merges_data_with_bundled_files(project, tmp_path):
    found = discover(project / "scripts" / "run.py", project)
    store = tmp_path / "store"
    io = LocalIO()
    assert not io.call("has_bundle", str(store), found.id)
    io.call("put_bundle", str(store), found.id, pack(found))
    assert io.call("has_bundle", str(store), found.id)
    # NOTE: All of workloads/ is data, but workloads/__init__.py is code.
    work = Path(workdir(str(store), found.id, str(project), ["workloads"]))
    assert (work / "workloads").is_dir() and not (work / "workloads").is_symlink()
    assert (work / "workloads" / "sysroots").is_symlink()
    assert not (work / "workloads" / "__init__.py").is_symlink()
    assert (work / "scripts" / "run.py").read_text().startswith("import json")
    assert not (work / "scripts" / "unused.py").exists()
    # NOTE: Made once.
    assert workdir(str(store), found.id, str(project), ["workloads"]) == str(work)


@pytest.fixture
def runnable(tmp_path):
    """A project whose run script imports a component and reads data, and
    a fake gem5 that runs the script with the system Python."""
    root = tmp_path / "proj"
    write(root, {
        "run.py": (
            "import sys\n"
            "from pathlib import Path\n"
            "from components.value import VALUE\n"
            "out = Path(sys.argv[1])\n"
            "data = Path('inputs/data.txt').read_text().strip()\n"
            "(out / 'result.txt').write_text(f'{VALUE} {data}')\n"
        ),
        "components/__init__.py": "",
        "components/value.py": "VALUE = 'old'\n",
        "inputs/data.txt": "seen\n",
    })
    gem5 = tmp_path / "gem5.fake"
    gem5.write_text(
        "#!/bin/bash\n"
        "for a; do case $a in --outdir=*) out=${a#--outdir=};; "
        '*) args+=("$a");; esac; done\n'
        'exec python3 "${args[0]}" "$out"\n'
    )
    gem5.chmod(0o755)
    return root, gem5


def test_a_worker_runs_the_bundle_it_was_given(runnable, tmp_path, monkeypatch):
    root, gem5 = runnable
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    experiment = gem5Experiment("e", root, gem5, tmp_path / "out")
    job = gem5Job(experiment, None, root / "run.py", "inputs/data.txt")
    spec = job.bundle_spec()
    # NOTE: The argument that names a project path is data too.
    assert spec["data"] == ["inputs/data.txt"]
    assert job.run_spec().script == "run.py" and job.run_spec().cwd is None
    assert job.features()["bundle"] == spec["id"]
    packed = pack(job.bundle())
    # NOTE: Edited after load: the job must run what was loaded.
    (root / "components" / "value.py").write_text("VALUE = 'new'\n")

    worker = Worker(store=tmp_path / "store")
    assert not worker.exposed_has_bundle(ME, spec["id"])
    worker.exposed_put_bundle(ME, spec["id"], packed)
    worker.exposed_launch_job(
        job.id(), ME, str(job.cwd()), job.command(), str(job.outdir()),
        tuple((label, str(path)) for label, path in job.files()),
        tuple((content, str(path)) for _, content, path in job.dumps()),
        (spec["id"], spec["root"], tuple(spec["data"])),
    )
    deadline = time.time() + 15
    while resolve_status(job.outdir())[0] in ("running", "missing"):
        assert time.time() < deadline
        time.sleep(0.05)
    assert resolve_status(job.outdir())[0] == "exited", (
        job.outdir() / "stderr"
    ).read_text()
    assert (job.outdir() / "result.txt").read_text() == "old seen"
    recorded = json.loads((job.outdir() / ".job" / "resources.json").read_text())
    assert recorded["id"] == spec["id"]
    # NOTE: The store is per host, under the user.
    assert (tmp_path / "store" / os.uname().nodename / ME / "blobs").is_dir()

    # NOTE: The outdir's index, and reading any file in it.
    deadline = time.time() + 5
    while json.loads(worker.exposed_job_index(ME, str(job.outdir()))) is None:
        assert time.time() < deadline
        time.sleep(0.05)
    index = json.loads(worker.exposed_job_index(ME, str(job.outdir())))
    assert index["files"]["result.txt"][0] == len("old seen")
    assert worker.exposed_read_file(
        ME, str(job.outdir()), "result.txt", 0, 100
    ) == b"old seen"
    with pytest.raises(PermissionError):
        worker.exposed_read_file(ME, str(job.outdir()), "../proj/run.py", 0, 9)


def test_the_scheduler_ships_each_bundle_once(runnable, tmp_path):
    root, gem5 = runnable
    experiment = gem5Experiment("e", root, gem5, tmp_path / "out")
    for i in range(2):
        experiment.register_job(gem5Job(experiment, None, root / "run.py", i))
    store = StateStore("test")
    scheduler = Scheduler("test", polling_secs=0, store=store)
    fake = FakeRoot()
    host = make_host(fake, capacity=4)
    scheduler._add_host(host)
    assert host.connect()
    compiled = compile_objects("s.py", "", [], [experiment], [host])
    (bundle_id,) = compiled["blobs"]

    forged = dict(compiled, blobs={bundle_id: base64.b64encode(b"x").decode()})
    with pytest.raises(CommandError, match=bundle_id[:12]):
        run(scheduler, "load", compiled=forged)

    run(scheduler, "load", compiled=compiled)
    scheduler._tick()
    assert list(fake.bundles) == [bundle_id]
    assert [b[0] for b in fake.launched_bundles] == [bundle_id, bundle_id]
    for job in scheduler._jobs.values():
        fake.status[str(job.outdir())] = "exited"
    scheduler._tick()
    # NOTE: Kept beside the state, not in it: the state names it only.
    scheduler._persist(force=True)
    assert compiled["blobs"][bundle_id] not in store.path().read_text()
    assert (store.directory() / "blobs" / f"{bundle_id}.tar.gz").is_file()

    # NOTE: After a restart, a host that lost it gets it from the file.
    resumed = Scheduler("test", polling_secs=0, store=store)
    other = FakeRoot()
    host2 = make_host(other, capacity=4)
    resumed._add_host(host2)
    assert host2.connect()
    resumed.resume(store.load())
    run(resumed, "reset", jobs=[j.id() for j in experiment.jobs()])
    resumed._tick()
    assert list(other.bundles) == [bundle_id]
