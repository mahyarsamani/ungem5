"""Specs for a few SIFT stats: the tutorial's frozen example.

This is an example for the tutorial (notebooks 05 and 06), frozen to the
stats the January 2026 runs in `example_data/` print.
SIFT's real specs live in the SIFT project, `projects/sift/analysis/sift_stats/`
of the PhD repo; graphix never imports a project, and a project's specs never
live in graphix.

| gem5 source | printed name | spec result |
|---|---|---|
| `CacheMemory.cc` | `usefulBytes.<accessor>` (also `read`, `write`, `resident`) | `UsefulBytes` histograms named `usefulBytes` (...), dim `accessor` |
| `CacheMemory.cc` | `bytesTransferred.<type>.<accessor>.<dst>` | Scalar `bytesTransferred`, dims `type`, `accessor`, `dst` |
| `CacheMemory.cc` | `bytesTransferred[<label>][<dst>]` (the example runs' naming) | Scalar `bytesTransferred`, dims `label`, `dst` |
| `Sequencer.cc` | `indRel{,Prod,Cons}AccLat.<consumer>[<producer>]<instance>` | Histogram `indRelAccLat`, dims `role`, `consumer`, `producer`, `instance` |

`dst` is gem5's MachineID string (`Cache_12`): the destination controller's
machine type and version.
The `dst` converter turns it into that controller's owner name, so transfers
can be grouped by where they went.
"""

import re

import graphix as gx
from useful_bytes import UsefulBytes

CACHE = "gem5::ruby::CacheMemory"
SEQUENCER = "gem5::ruby::Sequencer"


def controller_owner(dst: str, run: gx.Run) -> str:
    """`Cache_12` -> the canonical owner name of controller version 12."""
    return run.canonical(run.controller(dst))[0]


USEFUL_BYTES = gx.StatSpec(
    re.compile(
        r"(?P<stat>usefulBytes|readUsefulBytes|writeUsefulBytes|residentUsefulBytes)"
        r"\.(?P<accessor>.+)"
    ),
    name="{stat}",
    owner_class=CACHE,
    kind=UsefulBytes,
    unit="Byte",
    reduce="merge",
    doc="Used bytes per evicted block, per accessor label",
)

BYTES_TRANSFERRED = gx.StatSpec(
    "bytesTransferred.{type}.{accessor}.{dst}",
    owner_class=CACHE,
    kind="scalar",
    dims={"dst": controller_owner},
    unit="Byte",
)

BYTES_TRANSFERRED_OLD = gx.StatSpec(
    "bytesTransferred[{label}][{dst}]",
    name="bytesTransferred",
    owner_class=CACHE,
    kind="scalar",
    dims={"label": lambda label: label or "demand", "dst": controller_owner},
    unit="Byte",
    doc="The example runs' naming: the label is an accessor or 'prefetch'",
)

IND_REL_LATENCY = gx.StatSpec(
    re.compile(
        r"indRel(?P<role>Prod|Cons|)AccLat\."
        r"(?P<consumer>[^.\[]+)\[(?P<producer>[^\]]*)\](?P<instance>\d*)"
    ),
    name="indRelAccLat",
    owner_class=SEQUENCER,
    kind="histogram",
    dims={
        "role": lambda role: {"": "overall", "Prod": "producer", "Cons": "consumer"}[role],
        "instance": lambda text: int(text) if text else 0,
    },
    plots=("cdf", "box"),
)

TUTORIAL_SPECS = [
    USEFUL_BYTES,
    BYTES_TRANSFERRED,
    BYTES_TRANSFERRED_OLD,
    IND_REL_LATENCY,
]
