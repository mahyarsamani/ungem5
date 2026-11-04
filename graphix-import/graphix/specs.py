"""Stat specs: what a stat key means, without editing graphix.

A `StatSpec` matches the key of a stat (the part of its name after the owner
SimObject and before `::`) and says:
- the stat's family name, shared by every key the spec matches;
- which parts of the key are dimensions, such as `event` in
  `outTransLatHist.{event}`;
- the stat class (`kind`) to build, or `None` to infer it from the lines;
- defaults the stat classes use: unit, reduction, plot kinds, and the name
  of the dimension that `::<field>` elements become;
- or that matching lines are skipped (`skip=True`), as for gem5 Formulas.

A `Registry` holds specs in order and returns the first match.
User specs come before the built-in gem5 specs, so a user can override any
built-in.
"""

import inspect
import re
import string
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Tuple, Union

_PLACEHOLDER = re.compile(r"\{(\w+)(\.\.\.)?\}")


def compile_template(template: str) -> Tuple[Pattern, str]:
    """Turn a key template into a regex and a default family name.

    `{x}` matches any run of characters without a dot, including none, so it
    can sit inside a token (`indRel{kind}AccLat`).
    `{x...}` matches the rest, dots included.
    The default family name is the template with its placeholders and the
    empty tokens they leave removed: `outTransLatHist.{event}.retries`
    becomes `outTransLatHist.retries`.
    """
    regex = []
    position = 0
    for match in _PLACEHOLDER.finditer(template):
        regex.append(re.escape(template[position : match.start()]))
        body = ".+" if match.group(2) else "[^.]*"
        regex.append(f"(?P<{match.group(1)}>{body})")
        position = match.end()
    regex.append(re.escape(template[position:]))
    stripped = _PLACEHOLDER.sub("", template)
    name = ".".join(token for token in stripped.split(".") if token)
    return re.compile("".join(regex)), name


@dataclass(frozen=True)
class Resolution:
    """The outcome of matching one key: family name, dims, and the spec."""

    name: str
    dims: Tuple[Tuple[str, Any], ...]
    spec: Optional["StatSpec"]

    @property
    def skip(self) -> bool:
        return self.spec is not None and self.spec.skip


class StatSpec:
    """How to read one family of stat keys.

    Args:
        pattern: a key template (`"usefulBytes.{accessor}"`) or a compiled
            regex with named groups, matched against the whole key.
        name: the family name, as a format string over the captures
            (`"{stat}"`).
            Captures used in the name are not dimensions.
            Required for a regex whose captures should not all be dims.
        owner: a glob on the owner's SimObject path.
        owner_class: a glob on the owner's C++ class from `config.json`
            (`"gem5::ruby::*Cache_Controller"`).
        kind: `"scalar"`, `"vector"`, `"histogram"`, `"ruby_controller"`,
            a stat class, or `None` to infer the kind from the lines.
        dims: converters for captured dimensions, such as `{"thread": int}`;
            see `convert_dims`.
        element_dim: the column name that vector elements (`::<field>`)
            take in exports; defaults to `"element"`.
        unit: overrides the unit gem5 printed.
        reduce: the default reduction over owners (`"sum"`, `"mean"`,
            `"merge"`, ...).
        plots: plot kinds this family suits, overriding the class default.
        skip: drop matching lines entirely.
        doc: a note shown by `graphix.describe`.
    """

    def __init__(
        self,
        pattern: Union[str, Pattern],
        *,
        name: Optional[str] = None,
        owner: Optional[str] = None,
        owner_class: Optional[str] = None,
        kind: Union[str, type, None] = None,
        dims: Optional[Dict[str, Callable[[str], Any]]] = None,
        element_dim: Optional[str] = None,
        unit: Optional[str] = None,
        reduce: Optional[str] = None,
        plots: Optional[Tuple[str, ...]] = None,
        skip: bool = False,
        doc: Optional[str] = None,
    ) -> None:
        if isinstance(pattern, str):
            self.regex, default_name = compile_template(pattern)
        else:
            self.regex = pattern
            default_name = None if pattern.groupindex else pattern.pattern
        if name is None and default_name is None and not skip:
            # A regex with groups and no name: every group becomes a dim and
            # the family is named after the regex itself.
            default_name = pattern.pattern
        self.pattern = pattern
        self.name = name if name is not None else default_name
        self.owner = owner
        self.owner_class = owner_class
        self.kind = kind
        self.dims = dict(dims or {})
        self.element_dim = element_dim
        self.unit = unit
        self.reduce = reduce
        self.plots = tuple(plots) if plots is not None else None
        self.skip = skip
        self.doc = doc
        self._name_fields = (
            set(_format_fields(self.name)) if self.name is not None else set()
        )

    def match_key(self, key: str) -> Optional[Dict[str, str]]:
        match = self.regex.fullmatch(key)
        return None if match is None else match.groupdict()

    def match_owner(self, owner: str, owner_class: Optional[str]) -> bool:
        if self.owner is not None and not _glob(owner, self.owner):
            return False
        if self.owner_class is not None:
            if owner_class is None or not _glob(owner_class, self.owner_class):
                return False
        return True

    def resolve(self, key: str, captures: Dict[str, str]) -> Resolution:
        """Name the family and collect the raw (string) dimension values."""
        if self.skip:
            return Resolution(key, (), self)
        values = {k: ("" if v is None else v) for k, v in captures.items()}
        name = self.name.format(**values) if self._name_fields else self.name
        dims = tuple(
            (dim, value)
            for dim, value in values.items()
            if dim not in self._name_fields
        )
        return Resolution(name, dims, self)

    def convert_dims(
        self, dims: Tuple[Tuple[str, Any], ...], run: Any = None
    ) -> Tuple[Tuple[str, Any], ...]:
        """Apply the `dims` converters.

        A converter takes the raw string, or the raw string and the `Run` the
        stat came from (`lambda dst, run: run.controller(dst)`), so it can
        look things up in that run's config.
        """
        if not self.dims:
            return dims
        converted = []
        for dim, value in dims:
            convert = self.dims.get(dim)
            if convert is not None:
                value = (
                    convert(value, run)
                    if _takes_two(convert)
                    else convert(value)
                )
            converted.append((dim, value))
        return tuple(converted)

    def __repr__(self) -> str:
        pattern = (
            self.pattern if isinstance(self.pattern, str) else self.pattern.pattern
        )
        flags = " skip" if self.skip else ""
        return f"StatSpec({pattern!r}{flags})"


class Registry:
    """An ordered list of specs; the first match wins.

    `Registry(my_specs)` puts `my_specs` before the built-in gem5 specs;
    `Registry(my_specs, builtins=False)` uses only `my_specs`.
    """

    def __init__(
        self, specs: Iterable[StatSpec] = (), builtins: bool = True
    ) -> None:
        self._user: List[StatSpec] = list(specs)
        if builtins:
            from .gem5.specs import GEM5_SPECS

            self._builtin: List[StatSpec] = list(GEM5_SPECS)
        else:
            self._builtin = []
        self._key_cache: Dict[str, List[Tuple[StatSpec, Dict[str, str]]]] = {}

    @property
    def specs(self) -> List[StatSpec]:
        return self._user + self._builtin

    def add(self, *specs: StatSpec) -> None:
        """Append user specs; they still come before the built-ins."""
        self._user.extend(specs)
        self._key_cache.clear()

    def resolve(
        self, owner: str, owner_class: Optional[str], key: str
    ) -> Resolution:
        """Return the first matching spec's resolution, or a plain one."""
        candidates = self._key_cache.get(key)
        if candidates is None:
            candidates = []
            for spec in self.specs:
                captures = spec.match_key(key)
                if captures is not None:
                    candidates.append((spec, captures))
            self._key_cache[key] = candidates
        for spec, captures in candidates:
            if spec.match_owner(owner, owner_class):
                return spec.resolve(key, captures)
        return Resolution(key, (), None)


@lru_cache(maxsize=65536)
def _glob(value: str, pattern: str) -> bool:
    return fnmatchcase(value, pattern)


@lru_cache(maxsize=1024)
def _takes_two(function: Callable) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):  # builtins such as int
        return False
    positional = [
        p
        for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def _format_fields(template: str) -> List[str]:
    return [
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    ]
