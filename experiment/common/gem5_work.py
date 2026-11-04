import json
import platform
import shlex
import subprocess
import time

from argparse import Namespace
from dataclasses import asdict, replace
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import List, Optional

from ..api.bundle import Bundle, discover
from ..api.resources import Resources
from ..api.runner import JOB_DIR
from ..api.simulate import RunSpec
from ..api.work import Job, Experiment, PerHostJob, ProjectConfiguration
from .log import warn

SOURCE_IDENTITY_SECS = 60.0
_source_identities: dict[str, tuple[float, dict]] = {}


def source_identity(source_dir: Path) -> dict:
    """The gem5 source a build would compile right now: commit, and a hash
    of uncommitted changes. Cached for SOURCE_IDENTITY_SECS, since a load
    asks once per job."""
    key = str(source_dir)
    cached = _source_identities.get(key)
    if cached is not None and time.time() - cached[0] < SOURCE_IDENTITY_SECS:
        return cached[1]

    def git(*args) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", key, *args],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    identity = {
        "commit": commit.strip() if commit else None,
        "changes": (
            sha256(git("diff", "HEAD").encode()).hexdigest()[:16]
            if status
            else None
        ),
    }
    _source_identities[key] = (time.time(), identity)
    return identity


def _file_digest(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _is_valid(args, attr):
    if hasattr(args, attr):
        return getattr(args, attr) is not None
    else:
        return False


def calculate_hash(items: List) -> str:
    # NOTE: json keeps item boundaries, so ("a", "bc") and ("ab", "c")
    # hash differently.
    str_form = json.dumps(items, default=str)
    return sha256(str_form.encode()).hexdigest()


class ISA(Enum):
    NOTHING = "null"
    ARM = "ARM"
    RISCV = "RISCV"
    X86 = "X86"

    @classmethod
    def from_string(cls, isa: str) -> None:
        uppered = isa.upper()
        if uppered in ["NULL", "NOTHING", ""]:
            return cls.NOTHING
        if uppered == "ARM":
            return cls.ARM
        elif uppered == "RISCV":
            return cls.RISCV
        elif uppered == "X86":
            return cls.X86
        else:
            raise ValueError(f"Unknown ISA: {isa}")

    @classmethod
    def from_comma_separated_string(cls, isas: str) -> List["ISA"]:
        isa_list = isas.split(",")
        isa_list = sorted(isa_list)
        ret = [cls.from_string(isa) for isa in isa_list]
        if cls.NOTHING in ret and len(ret) > 1:
            raise ValueError(
                "Cannot use Nothing ISA with other ISAs. "
                "Please use only Nothing ISA."
            )
        return ret

    @classmethod
    def from_kvm_support(cls, os_reported: str) -> "ISA":
        if os_reported == "aarch64":
            return cls.ARM
        if os_reported == "x86_64":
            return cls.X86
        raise ValueError(f"Unknown OS reported ISA: {os_reported}")

    @classmethod
    def return_all_values(cls) -> List[str]:
        return [
            "nothing",
            "arm",
            "riscv",
            "x86",
        ]

    def __str__(self):
        return self.value

    def __repr__(self):
        return self.__str__()


class Protocol(Enum):
    NOTHING = "Nothing"
    CHI = "CHI"
    MESI2 = "MESI_Two_Level"
    MESI3 = "MESI_Three_Level"
    CXL = "CXL"
    VIPER = "GPU_VIPER"

    @classmethod
    def from_string(cls, protocol: str) -> None:
        uppered = protocol.upper()
        if uppered in ["NULL", "NOTHING", ""]:
            return cls.NOTHING
        if uppered in ["CHI"]:
            return cls.CHI
        elif uppered in ["MESI2", "MESI_TWO_LEVEL"]:
            return cls.MESI2
        elif uppered in ["MESI3", "MESI_THREE_LEVEL"]:
            return cls.MESI3
        elif uppered in ["CXL"]:
            return cls.CXL
        elif uppered in ["VIPER", "GPU_VIPER"]:
            return cls.VIPER
        else:
            raise ValueError(f"Unknown protocol: {protocol}")

    @classmethod
    def from_comma_separated_string(cls, protocols: str) -> List["Protocol"]:
        protocol_list = protocols.split(",")
        protocol_list = sorted(protocol_list)
        ret = [cls.from_string(protocol) for protocol in protocol_list]
        if cls.NOTHING in ret and len(ret) > 1:
            raise ValueError(
                "Cannot use Nothing protocol with other protocols. "
                "Please use only Nothing protocol."
            )
        return ret

    @classmethod
    def return_all_values(cls) -> List[str]:
        return ["nothing", "chi", "mesi2", "mesi3", "cxl", "viper"]

    def __str__(self):
        return self.value

    def __repr__(self):
        return self.__str__()


class BinaryOpt(Enum):
    DEBUG = "debug"
    OPT = "opt"
    FAST = "fast"

    @classmethod
    def from_string(cls, binary_opt: str) -> None:
        uppered = binary_opt.upper()
        if uppered == "DEBUG":
            return cls.DEBUG
        elif uppered == "OPT":
            return cls.OPT
        elif uppered == "FAST":
            return cls.FAST
        else:
            raise ValueError(f"Unknown binary option: {binary_opt}")

    @classmethod
    def return_all_values(cls) -> List[str]:
        return ["debug", "opt", "fast"]

    def __str__(self):
        return self.value

    def __repr__(self):
        return self.__str__()


class OtherValues(Enum):
    Store_Const = "store_const"

    def has_value(self, value):
        if value == OtherValues.Store_Const:
            return False
        return True

    def __str__(self) -> str:
        if self == OtherValues.Store_Const:
            return "OtherValues.Store_Const"


def _literal(value) -> str:
    """Python source that evaluates to `value` (for constructor.py)."""
    if isinstance(value, Path):
        return f"Path({str(value)!r})"
    if isinstance(value, OtherValues):
        return f"OtherValues.{value.name}"
    if isinstance(value, Resources):
        return f"Resources(cores={value.cores}, memory={value.memory!r})"
    return repr(value)


class gem5Job(Job):
    """One gem5 run: `gem5 --outdir=<outdir> <script> <args> --<kwargs>`.

    Positional args are passed as they are; each keyword argument becomes
    `--key-name value` (or just `--key-name` for OtherValues.Store_Const).
    The run is described by a RunSpec saved as <outdir>/.job/run.json;
    `helper run --spec <that file>` reruns it by hand.

    `resources` is a Resources (one core, and your guess of the peak
    memory) or None. The scheduler learns memory per (binary, build, gem5
    source, script content, arguments, input file sizes); see `features`.
    """

    @staticmethod
    def _cli_args(args, kwargs, shorten: bool) -> list[str]:
        def show(value) -> str:
            if shorten and isinstance(value, Path):
                return str(Path(*value.parts[-2:]))
            return str(value)

        words = [show(arg) for arg in args]
        for key, value in kwargs.items():
            flag = f"--{key.replace('_', '-')}"
            if isinstance(value, OtherValues) and not value.has_value(value):
                words.append(flag)
            else:
                words += [flag, show(value)]
        return words

    @staticmethod
    def make_command(
        gem5_path: Path, outdir: Path, run_script_path: Path, *args, **kwargs
    ) -> str:
        words = [
            str(gem5_path),
            f"--outdir={outdir}",
            str(run_script_path),
            *gem5Job._cli_args(args, kwargs, shorten=False),
        ]
        return shlex.join(words)

    @staticmethod
    def make_shorthand_command(
        gem5_path: Path, run_script_path: Path, *args, **kwargs
    ) -> str:
        words = [
            str(Path(*gem5_path.parts[-2:])),
            run_script_path.name,
            *gem5Job._cli_args(args, kwargs, shorten=True),
        ]
        return " ".join(words)

    @staticmethod
    def write_constructor(
        experiment: "gem5Experiment",
        resources: Resources,
        run_script_path: Path,
        *args,
        **kwargs,
    ) -> str:
        """Source for a script that recreates this job."""
        call_args = [
            "experiment",
            _literal(resources),
            _literal(Path(run_script_path)),
            *(_literal(arg) for arg in args),
            *(f"{key}={_literal(value)}" for key, value in kwargs.items()),
        ]
        body = "".join(f"    {arg},\n" for arg in call_args)
        return (
            "from pathlib import Path\n\n"
            "from experiment.api.resources import Resources\n"
            "from experiment.common.gem5_work import (\n"
            "    OtherValues,\n"
            "    gem5Experiment,\n"
            "    gem5Job,\n"
            ")\n\n"
            "experiment = gem5Experiment(\n"
            f"    name={experiment.name()!r},\n"
            f"    cwd={_literal(experiment.cwd())},\n"
            f"    gem5_path={_literal(experiment.gem5_path())},\n"
            f"    outdir={_literal(experiment.outdir())},\n"
            ")\n"
            f"job = gem5Job(\n{body})\n"
            "experiment.register_job(job)\n"
        )

    def __init__(
        self,
        experiment: "gem5Experiment",
        resources: Resources | None,
        run_script_path: Path,
        *args,
        **kwargs,
    ):
        resources = Resources.of(resources)
        run_script_path = Path(run_script_path)
        id = calculate_hash(
            [
                experiment.name(),
                experiment.gem5_path(),
                run_script_path,
                list(args),
                sorted(kwargs.items()),
            ]
        )
        outdir = experiment.outdir() / id
        found = experiment.bundle_for(run_script_path)
        if found is not None:
            # NOTE: Arguments that name paths in the project are data too;
            # the job runs in the bundle's directory, with the script at
            # its path relative to the root.
            root = experiment.cwd()
            extra = set()
            for value in (*args, *kwargs.values()):
                if isinstance(value, (str, Path)) and not Path(value).is_absolute():
                    path = root / value
                    if path.exists() and path.resolve().is_relative_to(root):
                        extra.add(str(path.resolve().relative_to(root)))
            found = replace(found, data=sorted(set(found.data) | extra))
            script = run_script_path
            script = (script if script.is_absolute() else root / script).resolve()
            script, cwd = str(script.relative_to(root)), None
        else:
            script, cwd = str(run_script_path), str(experiment.cwd())
        self._run_spec = RunSpec(
            binary=str(experiment.gem5_path()),
            script=script,
            script_args=gem5Job._cli_args(args, kwargs, shorten=False),
            outdir=str(outdir),
            cwd=cwd,
        )
        spec_path = outdir / JOB_DIR / "run.json"
        super().__init__(
            experiment.name(),
            experiment.cwd(),
            # NOTE: The worker's Python runs the spec and becomes gem5; see
            # experiment.api.simulate.
            '"$EXPERIMENT_PYTHON" -m experiment.api.simulate '
            + shlex.quote(str(spec_path)),
            gem5Job.make_shorthand_command(
                experiment.gem5_path(), run_script_path, *args, **kwargs
            ),
            outdir,
            resources,
            id,
            aux_files=[
                ("stats", outdir / "stats.txt"),
                ("terminal", outdir / "board.terminal"),
            ],
            dumps=[
                (
                    "constructor",
                    gem5Job.write_constructor(
                        experiment, resources, run_script_path, *args, **kwargs
                    ),
                    outdir / "constructor.py",
                ),
                ("run.json", self._run_spec.to_json(), spec_path),
            ],
        )
        self._run_script_path = run_script_path
        self._args = args
        self._kwargs = kwargs
        self._gem5_path = experiment.gem5_path()
        self._build = experiment.build()
        self._bundle = found
        if self._build is not None:
            self.depends_on(self._build)

    def run_spec(self) -> RunSpec:
        return self._run_spec

    def _script_path(self) -> Path:
        path = self._run_script_path
        return path if path.is_absolute() else self.cwd() / path

    def features(self) -> dict:
        """The binary and how it was built (the build's spec and the gem5
        source it compiles, or the build's recorded configuration), the
        script and its content, the arguments, and the size of every
        argument that is an existing file."""
        if self._build is not None:
            spec = self._build.spec()
            build = asdict(spec)
            # NOTE: Threads change how fast it builds, not what it builds.
            build.pop("threads", None)
            build["source"] = source_identity(Path(spec.gem5_source_dir))
        else:
            config = self._gem5_path.parent / "gem5.build" / "compile_config.json"
            try:
                build = json.loads(config.read_text())
            except (OSError, json.JSONDecodeError):
                build = None
        inputs = {}
        for value in (*self._args, *self._kwargs.values()):
            if isinstance(value, (str, Path)) and "/" in str(value):
                path = Path(value)
                path = path if path.is_absolute() else self.cwd() / path
                try:
                    if path.is_file():
                        inputs[str(value)] = path.stat().st_size
                except OSError:
                    pass
        return {
            "binary": str(self._gem5_path),
            "build": build,
            "script": str(self._run_script_path),
            "script_content": _file_digest(self._script_path()),
            # NOTE: All the Python the job runs, not just its script.
            "bundle": self._bundle.id if self._bundle is not None else None,
            "args": [str(arg) for arg in self._args],
            "kwargs": {key: str(value) for key, value in self._kwargs.items()},
            "inputs": inputs,
        }

    def memory_group(self) -> str:
        """Runs of the same script with the same binary path."""
        return f"{self._run_script_path.name} on {self._gem5_path}"

    def display_command(self) -> str:
        return shlex.join(self._run_spec.argv())


# NOTE: Old name, kept so existing experiment scripts keep working.
gem5FSSimulation = gem5Job


class gem5Build(PerHostJob):
    """Build gem5 on every compatible host, into that host's build dir.

    One job per host (they appear as `<id>@<host>`), each running the same
    backend as `helper build` on its worker. Jobs of a gem5Experiment made
    with `build=` this run only on hosts where this build succeeded.
    Override `compatible_with_host` to build on fewer hosts.
    """

    def __init__(
        self,
        config: "gem5ProjectConfiguration",
        name: str,
        resources: Resources | None = None,
        threads: int | None = None,
        experiment: str = "builds",
        **options,
    ):
        """
        :param resources: what each build takes: cores the scheduler counts
            (at most the whole host; None is 1 core, so a build starts beside
            running jobs whatever its `threads`) and a memory guess.
        :param options: isas, protocols, binary_opt, bits_per_set, linker,
            gprof, no_tcmalloc, as for `helper build`; unset ones take the
            project's defaults.
        """
        # NOTE: Imported here: experiment.api.build imports this module.
        from ..api.build import BuildSpec

        self._spec = BuildSpec.from_project(
            config, name, threads=threads, **options
        )
        paths = config.path_config()
        super().__init__(
            experiment,
            f"build-{config.name()}-{name}",
            paths.gem5_out_base_dir() / experiment / name,
        )
        self._cwd = paths.project_dir()
        self._resources = Resources.of(resources)
        self._threads = threads or 1

    def spec(self):
        return self._spec

    def binary(self) -> Path:
        return self._spec.binary()

    def resources(self) -> Resources:
        return self._resources

    def make_job(self, host_name: str, id: str, outdir: Path) -> Job:
        spec_path = outdir / JOB_DIR / "build.json"
        return Job(
            self.experiment(),
            self._cwd,
            '"$EXPERIMENT_PYTHON" -m experiment.api.build '
            + shlex.quote(str(spec_path)),
            f"build {self._spec.name}",
            outdir,
            self._resources,
            id,
            dumps=[("build.json", self._spec.to_json(), spec_path)],
        )


class gem5Experiment(Experiment):
    def __init__(
        self,
        name: str,
        cwd: Path,
        gem5_path: Path | None = None,
        outdir: Path | None = None,
        build: gem5Build | None = None,
        bundle: bool = True,
        extra_files: list[Path] = (),
    ):
        """
        :param build: a gem5Build; every job of this experiment then
            depends on it and runs its binary (`gem5_path` is not needed).
        :param bundle: ship each job's Python (its run script and the
            project files it imports; see experiment.api.bundle) to its
            host, frozen at load, instead of running the files in `cwd`.
            `cwd` is the project root the bundle is relative to.
        :param extra_files: files to bundle that imports don't show, e.g.
            modules loaded with importlib.
        """
        if outdir is None:
            raise ValueError(f"gem5Experiment {name}: outdir is required")
        if gem5_path is None and build is None:
            raise ValueError(f"gem5Experiment {name}: give gem5_path or build")
        super().__init__(name, Path(outdir).resolve())
        self._cwd = Path(cwd).resolve()
        self._build = build
        self._gem5_path = Path(
            gem5_path if gem5_path is not None else build.binary()
        ).resolve()
        self._bundle = bundle
        self._extra_files = [Path(path) for path in extra_files]
        self._bundles: dict[Path, Bundle] = {}

    def bundle_for(self, run_script_path: Path) -> Bundle | None:
        """The bundle of a run script, found once per script."""
        if not self._bundle:
            return None
        script = Path(run_script_path)
        script = (script if script.is_absolute() else self._cwd / script).resolve()
        if script not in self._bundles:
            self._bundles[script] = discover(
                script, self._cwd, self._extra_files
            )
        return self._bundles[script]

    def cwd(self) -> Path:
        return self._cwd

    def gem5_path(self) -> Path:
        return self._gem5_path

    def build(self) -> gem5Build | None:
        return self._build


class gem5BuildConfiguration:
    def __init__(
        self,
        isas: str,
        protocols: str,
        binary_opt: str,
        bits_per_set: int,
    ):
        self.isas = ISA.from_comma_separated_string(isas)
        self.protocols = Protocol.from_comma_separated_string(protocols)
        self.binary_opt = BinaryOpt.from_string(binary_opt)
        self.bits_per_set = bits_per_set

        self._check_validity()

    def _check_validity(self):
        if Protocol.VIPER in self.protocols and ISA.X86 not in self.isas:
            raise ValueError("VIPER protocol only works with X86 ISA.")

    def is_same(self, other_dict):
        this_dict = self.dump_config()
        return (
            this_dict["isas"] == other_dict["isas"]
            and this_dict["protocols"] == other_dict["protocols"]
            and this_dict["bits_per_set"] == other_dict["bits_per_set"]
        )

    def make_setconfig_command(self, build_dir):
        command = f"scons setconfig {build_dir}"
        if ISA.NOTHING not in self.isas:
            command += " BUILD_ISA=y "
            command += " ".join([f"USE_{str(isa)}_ISA=y" for isa in self.isas])

        if Protocol.NOTHING not in self.protocols:
            command += " RUBY=y "
            if len(self.protocols) > 1:
                command += 'USE_MULTIPLE_PROTOCOLS=y PROTOCOL="MULTIPLE" '
            if Protocol.VIPER in self.protocols:
                command += "BUILD_GPU=y VEGA_GPU_ISA=y "
            command += " ".join(
                [
                    f"RUBY_PROTOCOL_{str(protocol)}=y"
                    for protocol in self.protocols
                ]
            )

            command += f" NUMBER_BITS_PER_SET={self.bits_per_set}"

        try:
            kvm_isa = ISA.from_kvm_support(platform.machine())
            if kvm_isa in self.isas:
                command += f" USE_KVM=y KVM_ISA={str(kvm_isa).lower()}"
        except ValueError:
            warn("KVM is not supported on this platform for this compilation.")
        return command

    def to_dict(self) -> dict:
        return {
            "isas": self.isas,
            "protocols": self.protocols,
            "binary_opt": self.binary_opt,
            "bits_per_set": self.bits_per_set,
        }

    def dump_config(self) -> dict:
        return {
            "isas": [str(isa) for isa in self.isas],
            "protocols": [str(protocol) for protocol in self.protocols],
            "bits_per_set": self.bits_per_set,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "gem5BuildConfiguration":
        return cls(
            isas=",".join(data["isas"]),
            protocols=",".join(data["protocols"]),
            binary_opt=data["binary_opt"],
            bits_per_set=data["bits_per_set"],
        )

    @classmethod
    def from_args_and_config(
        cls, args: Namespace, config: "gem5BuildConfiguration"
    ) -> "gem5BuildConfiguration":
        isas = (
            ",".join([str(isa) for isa in config.isas])
            if not _is_valid(args, "isas")
            else args.isas
        )

        protocols = (
            ",".join([str(protocol) for protocol in config.protocols])
            if not _is_valid(args, "protocols")
            else args.protocols
        )

        binary_opt = (
            str(config.binary_opt)
            if not _is_valid(args, "binary_opt")
            else args.binary_opt
        )

        bits_per_set = (
            config.bits_per_set
            if not _is_valid(args, "bits_per_set")
            else args.bits_per_set
        )

        return cls(
            isas=isas,
            protocols=protocols,
            binary_opt=binary_opt,
            bits_per_set=bits_per_set,
        )


class gem5PathConfiguration:
    def __init__(
        self,
        project_dir: str,
        gem5_source_dir: str,
        gem5_binary_base_dir: str,
        gem5_out_base_dir: str,
        gem5_resource_json_path: Optional[str] = None,
    ):
        self._project_dir = Path(project_dir).resolve()
        self._gem5_source_dir = Path(gem5_source_dir).resolve()
        self._gem5_binary_base_dir = Path(gem5_binary_base_dir).resolve()
        self._gem5_out_base_dir = Path(gem5_out_base_dir).resolve()
        if gem5_resource_json_path is not None:
            self._gem5_resource_json_path = Path(
                gem5_resource_json_path
            ).resolve()
        else:
            self._gem5_resource_json_path = None

    def project_dir(self):
        return self._project_dir

    def gem5_source_dir(self):
        return self._gem5_source_dir

    def gem5_resource_json_path(self):
        return self._gem5_resource_json_path

    def gem5_binary_base_dir(self):
        return self._gem5_binary_base_dir

    def gem5_out_base_dir(self):
        return self._gem5_out_base_dir

    def to_dict(self) -> dict:
        return {
            "project_dir": self._project_dir,
            "gem5_source_dir": self._gem5_source_dir,
            "gem5_binary_base_dir": self._gem5_binary_base_dir,
            "gem5_out_base_dir": self._gem5_out_base_dir,
            "gem5_resource_json_path": self._gem5_resource_json_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "gem5PathConfiguration":
        return cls(
            project_dir=data["project_dir"],
            gem5_source_dir=data["gem5_source_dir"],
            gem5_binary_base_dir=data["gem5_binary_base_dir"],
            gem5_out_base_dir=data["gem5_out_base_dir"],
            gem5_resource_json_path=data["gem5_resource_json_path"],
        )


class gem5ProjectConfiguration(ProjectConfiguration):
    def __init__(
        self,
        project_name: str,
        project_dir: str,
        gem5_source_dir: str,
        gem5_binary_base_dir: str,
        gem5_out_base_dir: str,
        gem5_resource_json_path: str,
        default_isas: str,
        default_protocols: str,
        default_binary_opt: str,
        default_bits_per_set: int,
    ):
        super().__init__()
        self._name = project_name
        self._path_config = gem5PathConfiguration(
            project_dir,
            gem5_source_dir,
            gem5_binary_base_dir,
            gem5_out_base_dir,
            gem5_resource_json_path,
        )
        self._build_config = gem5BuildConfiguration(
            default_isas,
            default_protocols,
            default_binary_opt,
            default_bits_per_set,
        )

    def name(self):
        return self._name

    def base_dir(self):
        return self._path_config.project_dir()

    def get_experiment_dir(self, experiment: gem5Experiment) -> Path:
        if not isinstance(experiment, gem5Experiment):
            raise ValueError(
                "experiment must be an instance of gem5Experiment"
            )
        return self._path_config.project_dir() / experiment.name()

    def path_config(self):
        return self._path_config

    def build_config(self):
        return self._build_config

    def to_dict(self) -> dict:
        return {
            "project_name": self.name(),
            "path_config": self._path_config.to_dict(),
            "build_config": self._build_config.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "gem5ProjectConfiguration":
        path_dict = data["path_config"]
        build_dict = data["build_config"]
        return cls(
            project_name=data["project_name"],
            project_dir=path_dict["project_dir"],
            gem5_source_dir=path_dict["gem5_source_dir"],
            gem5_resource_json_path=path_dict["gem5_resource_json_path"],
            gem5_binary_base_dir=path_dict["gem5_binary_base_dir"],
            gem5_out_base_dir=path_dict["gem5_out_base_dir"],
            default_isas=",".join(build_dict["isas"]),
            default_protocols=",".join(build_dict["protocols"]),
            default_binary_opt=build_dict["binary_opt"],
            default_bits_per_set=build_dict["bits_per_set"],
        )
