"""The Python a job runs, found from its top-level script and shipped.

`discover` follows a script's imports the way Python would resolve them,
without running anything: it parses the script, maps every `import a.b`
and `from a import b` to a file under the project's search roots (the
script's own directory, then the project root, as gem5 and
`expose_project_dir` set up `sys.path`), and follows those files in turn.
Names that resolve outside the project (the standard library, site
packages, `m5` and `gem5`, which live in the gem5 binary) are left to the
worker. Directories without an `__init__.py` are not packages here, so the
gem5 source tree in a project is never mistaken for code to ship.

What imports can't show is reported, not guessed:
- dynamic imports (`importlib.import_module`, `__import__`) are warnings;
  a job lists what they load in `extra_files`;
- string literals that name an existing path under the root (or next to the
  module that holds them) are data: they stay on the shared filesystem and
  are linked into the job's working directory at the same relative path.

A `Bundle` keeps the files at their paths relative to the root, so code
that finds the root from `__file__` finds the job's working directory. Its
id is the sha256 of every file's path and content; `pack` makes a tar of
it that is the same bytes for the same files.
"""

import ast
import gzip
import hashlib
import io
import tarfile

from dataclasses import dataclass, field
from pathlib import Path

# NOTE: Bundles travel through the scheduler; past this, warn.
WARN_BYTES = 5 << 20
MAX_BYTES = 50 << 20


class BundleError(Exception):
    """A bundle can't be made; the message is for the user."""


@dataclass
class Bundle:
    root: Path
    # NOTE: Paths relative to the root, sorted.
    files: list[str]
    data: list[str]
    warnings: list[str] = field(default_factory=list)
    id: str = ""

    def spec(self) -> dict:
        """What a job's compiled data says about its bundle."""
        return {
            "id": self.id,
            "root": str(self.root),
            "files": self.files,
            "data": self.data,
        }


def _digest(root: Path, files: list[str]) -> str:
    hasher = hashlib.sha256()
    for name in files:
        content = (root / name).read_bytes()
        hasher.update(name.encode() + b"\0")
        hasher.update(hashlib.sha256(content).digest())
    return hasher.hexdigest()


class _Finder:
    """Resolves module names against the search roots, as Python's path
    finder would for regular packages and modules."""

    def __init__(self, roots: list[Path], root: Path) -> None:
        self._roots = roots
        self._root = root

    def _inside(self, path: Path) -> bool:
        return path.resolve().is_relative_to(self._root)

    def module(self, name: str) -> list[Path] | None:
        """The files importing `name` runs (its packages' `__init__.py`
        and the module itself), or None if it isn't under a root."""
        parts = name.split(".")
        for base in self._roots:
            found, directory = [], base
            for i, part in enumerate(parts):
                package = directory / part / "__init__.py"
                module = directory / f"{part}.py"
                if package.is_file():
                    found.append(package)
                    directory = directory / part
                elif module.is_file() and i == len(parts) - 1:
                    found.append(module)
                    break
                else:
                    found = None
                    break
            if found and all(self._inside(path) for path in found):
                return found
        return None


def _imports(tree: ast.AST, path: Path, root: Path) -> tuple[list[str], list[str]]:
    """(module names this file imports, warnings). A `from a import b`
    yields both `a` and `a.b`, since `b` may be a submodule."""
    names, warnings = [], []
    package = ".".join(path.relative_to(root).with_suffix("").parts[:-1])
    if path.name == "__init__.py":
        package = ".".join(path.relative_to(root).parent.parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            if prefix:
                names.append(prefix)
            names += [
                f"{prefix}.{alias.name}" if prefix else alias.name
                for alias in node.names
                if alias.name != "*"
            ]
        elif isinstance(node, ast.Call):
            func = node.func
            called = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else ""
            )
            if called in ("import_module", "__import__"):
                warnings.append(
                    f"{path.relative_to(root)}:{node.lineno} imports "
                    "dynamically; list what it loads in extra_files"
                )
    return names, warnings


def _data(tree: ast.AST, path: Path, root: Path) -> list[str]:
    """String literals naming an existing path under the root, relative to
    the root or to this file's directory, as paths relative to the root."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        if not text or "\n" in text or len(text) > 400:
            continue
        # NOTE: "." and "./" are separators and prefixes far more often
        # than paths; never take them for the directory they'd name.
        if all(part in (".", "..") for part in Path(text).parts) or text.strip(
            "./"
        ) == "":
            continue
        if "/" not in text and "." not in text:
            continue
        for base in (Path(), root, path.parent):
            candidate = Path(text) if Path(text).is_absolute() else base / text
            try:
                if base == Path() and not candidate.is_absolute():
                    continue
                if not candidate.exists():
                    continue
                resolved = candidate.resolve()
            except (OSError, ValueError):
                continue
            if resolved.is_relative_to(root) and resolved != root:
                found.append(str(resolved.relative_to(root)))
                break
    return found


def discover(
    script: Path, root: Path, extra_files: list[Path] = ()
) -> Bundle:
    """The bundle of `script`: it, every project file its imports reach,
    `extra_files`, and the data paths they name."""
    root = Path(root).resolve()
    script = Path(script).resolve()
    if not script.is_relative_to(root):
        raise BundleError(f"{script} is not under the project {root}")
    finder = _Finder([script.parent, root], root)
    files: set[Path] = set()
    data: set[str] = set()
    warnings: list[str] = []
    pending = [script, *(Path(p).resolve() for p in extra_files)]
    while pending:
        path = pending.pop()
        if path in files:
            continue
        if not path.is_file():
            raise BundleError(f"{path} does not exist")
        files.add(path)
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError as e:
            raise BundleError(f"{path}: {e}") from None
        names, found = _imports(tree, path, root)
        warnings += found
        for name in names:
            pending += finder.module(name) or []
        data.update(_data(tree, path, root))
    relative = sorted(str(path.relative_to(root)) for path in files)
    # NOTE: A data path that is a bundled file is code, not data, and one
    # inside another data path comes with it.
    data -= set(relative)
    data = {
        path for path in data
        if not any(Path(path).is_relative_to(other) and path != other
                   for other in data)
    }
    size = sum((root / name).stat().st_size for name in relative)
    if size > MAX_BYTES:
        raise BundleError(
            f"the bundle of {script.name} is {size >> 20} MB, over "
            f"{MAX_BYTES >> 20} MB; is data being imported?"
        )
    if size > WARN_BYTES:
        warnings.append(f"the bundle of {script.name} is {size >> 20} MB")
    bundle = Bundle(root, relative, sorted(data), warnings)
    bundle.id = _digest(root, relative)
    return bundle


def pack(bundle: Bundle) -> bytes:
    """The bundle's files as a gzipped tar, the same bytes for the same
    files: sorted, with no times, owners or other metadata."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in bundle.files:
            content = (bundle.root / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return gzip.compress(raw.getvalue(), mtime=0)


def unpack(data: bytes, bundle_id: str, target: Path) -> list[str]:
    """Extract a packed bundle into `target` and check it is `bundle_id`.
    Returns the files' relative paths."""
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        names = sorted(member.name for member in tar.getmembers())
        tar.extractall(target, filter="data")
    if _digest(target, names) != bundle_id:
        raise BundleError(f"bundle {bundle_id[:12]} doesn't match its id")
    return names
