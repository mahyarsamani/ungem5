"""Canonical owner names for SimObject paths that mean the same hardware.

Runs in one study can name the same component differently:
`...core_clusters0.dcache` in one config and `...core_clusters0.l1dcache`
in another.
An `OwnerScheme` maps every such raw path to one canonical name:

    OwnerScheme({
        "core{c}.l1d": [
            "board.cache_hierarchy.core_tiles{t}.core_clusters{c}.dcache",
            "board.cache_hierarchy.core_tiles{t}.core_clusters{c}.l1dcache",
        ],
    })

- A raw template matches a whole path or a prefix of it, token by token.
  `{x}` captures a run of characters without a dot (inside a token, as in
  `core_tiles{t}`).
  The longest matching prefix wins.
- With `keep_suffix=True`, the rest of the path is kept, so
  `...dcache.cache` becomes `core0.l1d.cache`.
- Captures that the canonical name does not use (`t` above) become dims, so
  no information is lost.
- `unmapped` decides what happens to a path that no template covers:
  `"keep"` it as is, `"warn"` and keep it, or raise an `"error"`.
"""

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union
from warnings import warn

from .specs import compile_template

Pairs = Tuple[Tuple[str, str], ...]


class OwnerScheme:
    def __init__(
        self,
        mapping: Dict[str, Union[str, Sequence[str]]],
        keep_suffix: bool = True,
        unmapped: str = "keep",
    ) -> None:
        if unmapped not in ("keep", "warn", "error"):
            raise ValueError("unmapped must be 'keep', 'warn', or 'error'.")
        self.mapping = {
            canonical: [raw] if isinstance(raw, str) else list(raw)
            for canonical, raw in mapping.items()
        }
        self.keep_suffix = keep_suffix
        self.unmapped = unmapped
        self._rules = []
        for canonical, raws in self.mapping.items():
            canonical_fields = set(re.findall(r"\{(\w+)\}", canonical))
            for raw in raws:
                regex, _ = compile_template(raw)
                prefix = re.compile(regex.pattern + r"(?:\.(?P<_suffix>.+))?")
                self._rules.append((prefix, canonical, canonical_fields))
        self._cache: Dict[str, Tuple[str, Pairs]] = {}
        self._warned = set()

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "OwnerScheme":
        with open(path, "r") as scheme_file:
            data = json.load(scheme_file)
        return cls(
            data["mapping"],
            keep_suffix=data.get("keep_suffix", True),
            unmapped=data.get("unmapped", "keep"),
        )

    def to_json(self, path: Union[str, Path]) -> None:
        with open(path, "w") as scheme_file:
            json.dump(
                {
                    "mapping": self.mapping,
                    "keep_suffix": self.keep_suffix,
                    "unmapped": self.unmapped,
                },
                scheme_file,
                indent=2,
            )
            scheme_file.write("\n")

    def map(self, path: str) -> Tuple[str, Pairs]:
        """Return (canonical owner, extra dims) for a raw SimObject path."""
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        best = None
        for prefix, canonical, fields in self._rules:
            match = prefix.fullmatch(path)
            if match is None:
                continue
            suffix = match.group("_suffix")
            if suffix and not self.keep_suffix:
                continue
            matched_length = len(path) - (len(suffix) + 1 if suffix else 0)
            if best is None or matched_length > best[0]:
                best = (matched_length, match, canonical, fields, suffix)
        if best is None:
            result = (path, ())
            if self.unmapped == "error":
                raise KeyError(f"No owner template covers '{path}'.")
            if self.unmapped == "warn" and path not in self._warned:
                self._warned.add(path)
                warn(f"No owner template covers '{path}'; keeping it as is.")
        else:
            _, match, canonical, fields, suffix = best
            captures = {k: v for k, v in match.groupdict().items() if k != "_suffix"}
            name = canonical.format(**{k: captures[k] for k in fields})
            if suffix:
                name = f"{name}.{suffix}"
            extra = tuple((k, v) for k, v in captures.items() if k not in fields)
            result = (name, extra)
        self._cache[path] = result
        return result

    def check(self, paths: Iterable[str]) -> None:
        """Raise if two different raw paths map to one canonical owner."""
        seen: Dict[Tuple[str, Pairs], str] = {}
        for path in paths:
            key = self.map(path)
            other = seen.setdefault(key, path)
            if other != path:
                raise ValueError(
                    f"'{other}' and '{path}' both map to owner '{key[0]}'"
                    + (f" with dims {dict(key[1])}" if key[1] else "")
                    + "; make the templates tell them apart."
                )

    def __repr__(self) -> str:
        return f"OwnerScheme({len(self.mapping)} canonical owners)"
