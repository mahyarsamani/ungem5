"""Built-in specs for stock gem5 stats.

Sources, all under gem5's `src/`:
- Formula stats are skipped, because graphix does not support them; the list
  comes from the `statistics::Formula` declarations of the objects in an Arm
  full-system board with O3 cores, CHI Ruby, and DRAM.
  A leaf name can be a Formula on one object and a Scalar or Vector on
  another (`hits` on a TLB versus on an indirect predictor), so each skip
  is tied to its owner's C++ class.
- Vector2d stats print each row as `<name>_<x>::<y>` (`base/stats/text.cc`,
  `Text::visit(Vector2dInfo)`); the specs below turn `<x>` into a dimension.
- Ruby's collated controller vectors (`<Machine>_Controller.<event>` and
  `<Machine>_Controller.<state>.<event>` under `ruby_system`) are indexed by
  controller version (`mem/slicc/symbols/StateMachine.py`, `collateStats`).
- SLICC transaction histograms and the Ruby profiler's groups put dimension
  values inside the stat name.
- `VectorStandardDeviation` stats (`mem_ctrl.avgPriority_<requestor>`) print
  only summaries, which graphix does not read, so they are skipped.
"""

import re

from ..specs import StatSpec

R = re.compile

# Owner classes, as `config.json` records them in `cxx_class`.
ROOT = "gem5::Root"
CPU = "gem5::*CPU"
BPRED = "gem5::branch_prediction::*"
TLB = "gem5::ArmISA::TLB"
WALKER = "gem5::ArmISA::TableWalker"
PREFETCHER = "gem5::prefetch::*"
MEM_CTRL = "gem5::memory::*MemCtrl"
DRAM = "gem5::memory::DRAMInterface"
XBAR = "gem5::*XBar"
RUBY_SYSTEM = "gem5::ruby::RubySystem"
RUBY_CONTROLLER = "gem5::ruby::*_Controller"
RUBY_CACHE = "gem5::ruby::CacheMemory"
MESSAGE_BUFFER = "gem5::ruby::MessageBuffer"
SWITCH = "gem5::ruby::Switch"
SIMPLE_NETWORK = "gem5::ruby::SimpleNetwork"


def _skip(owner_class: str, regex: str, doc: str) -> StatSpec:
    return StatSpec(R(regex), owner_class=owner_class, skip=True, doc=doc)


FORMULA_SKIPS = [
    _skip(ROOT, r"simSeconds|hostTickRate|hostInstRate|hostOpRate", "Formula"),
    _skip(
        CPU,
        r"cpi|ipc|issueRate|fuBusyRate"
        r"|fetchStats\d+\.(fetchRate|branchRate)"
        r"|executeStats\d+\.(numStoreInsts|instRate)"
        r"|commitStats\d+\.(cpi|ipc|numArchMemRefs)"
        r"|iew\.(branchMispredicts|wbRate|wbFanout)"
        r"|fetch\.idleRate",
        "Formula",
    ),
    _skip(BPRED, r"BTBHitRatio", "Formula"),
    _skip(
        TLB,
        r"readAccesses|writeAccesses|instAccesses|hits|misses|accesses",
        "Formula",
    ),
    _skip(PREFETCHER, r"accuracy|coverage|pfLate", "Formula"),
    _skip(
        MEM_CTRL,
        r"avgRdBWSys|avgWrBWSys|avgGap|requestorReadRate|requestorWriteRate"
        r"|requestorReadAvgLat|requestorWriteAvgLat",
        "Formula",
    ),
    _skip(
        DRAM,
        r"avgQLat|avgBusLat|avgMemAccLat|readRowHitRate|writeRowHitRate"
        r"|avgRdBW|avgWrBW|peakBW|busUtil|busUtilRead|busUtilWrite"
        r"|pageHitRate|bwRead|bwInstRead|bwWrite|bwTotal",
        "Formula",
    ),
    _skip(XBAR, r"\w+\.utilization", "Formula"),
    _skip(MESSAGE_BUFFER, r"m_avg_stall_time|m_occupancy", "Formula"),
    _skip(
        SWITCH,
        r"throttle\d+\.(link_utilization|avg_msg_wait_time|avg_bandwidth"
        r"|avg_useful_bandwidth|msg_bytes\.\w+)"
        r"|percent_links_utilized|msg_count\.\w+|msg_bytes\.\w+",
        "Formula",
    ),
    _skip(SIMPLE_NETWORK, r"msg_count\.\w+|msg_byte\.\w+", "Formula"),
    _skip(RUBY_CACHE, r"m_demand_accesses|m_prefetch_accesses", "Formula"),
    _skip(
        MEM_CTRL,
        r"avgPriority(Distance)?_.+",
        "VectorStandardDeviation: summaries only",
    ),
]

VECTOR2D = [
    StatSpec(
        R(
            r"(?P<stat>lookups|squashes|corrected|earlyResteers|committed"
            r"|mispredicted|targetWrong)_(?P<thread>\d+)"
        ),
        name="{stat}",
        owner_class=BPRED,
        kind="vector",
        dims={"thread": int},
        element_dim="branch_type",
        doc="Vector2d: thread x BranchType",
    ),
    StatSpec(
        R(r"targetProvider_(?P<thread>\d+)"),
        name="targetProvider",
        owner_class=BPRED,
        kind="vector",
        dims={"thread": int},
        element_dim="provider",
        doc="Vector2d: thread x TargetProvider",
    ),
    StatSpec(
        R(r"(?P<stat>commit\.committedInstType|statIssuedInstType)_(?P<thread>\d+)"),
        name="{stat}",
        owner_class=CPU,
        kind="vector",
        dims={"thread": int},
        element_dim="op_class",
        doc="Vector2d: thread x OpClass",
    ),
    StatSpec(
        R(r"requestOrigin_(?P<stage>Requested|Completed)"),
        name="requestOrigin",
        owner_class=WALKER,
        kind="vector",
        element_dim="origin",
        doc="Vector2d: Requested/Completed x Data/Inst",
    ),
    StatSpec(
        R(r"(?P<stat>pktCount|pktSize)_(?P<src>.+)"),
        name="{stat}",
        owner_class=XBAR,
        kind="vector",
        element_dim="dst",
        doc="Vector2d: source port x destination port",
    ),
]

RUBY = [
    StatSpec(
        R(r"(?P<machine>\w+?)_Controller\.(?P<state>[^.]+)\.(?P<event>[^.]+)"),
        name="{machine}_Controller.transitions",
        owner_class=RUBY_SYSTEM,
        kind="ruby_controller",
        doc="Transition counts per controller, collated by version",
    ),
    StatSpec(
        R(r"(?P<machine>\w+?)_Controller\.(?P<event>[^.]+)"),
        name="{machine}_Controller.events",
        owner_class=RUBY_SYSTEM,
        kind="ruby_controller",
        doc="Event counts per controller, collated by version",
    ),
    StatSpec(
        "outTransLatHist.{event}.retries",
        owner_class=RUBY_CONTROLLER,
        kind="scalar",
    ),
    StatSpec(
        "inTransLatHist.{event}.retries",
        owner_class=RUBY_CONTROLLER,
        kind="scalar",
    ),
    StatSpec(
        "inTransLatHist.{event}.{initial}.{final}.total",
        name="inTransLatHist.transitions",
        owner_class=RUBY_CONTROLLER,
        kind="scalar",
        doc="The '.total' belongs to the stat name, not a '::total' field",
    ),
    # gem5 prints no unit, but AbstractController samples ticksToCycles(...).
    StatSpec(
        "outTransLatHist.{event}",
        owner_class=RUBY_CONTROLLER,
        kind="histogram",
        unit="Cycle",
    ),
    StatSpec(
        "inTransLatHist.{event}",
        owner_class=RUBY_CONTROLLER,
        kind="histogram",
        unit="Cycle",
    ),
    StatSpec(
        "RequestType.{request}.{stat}",
        name="RequestType.{stat}",
        owner_class=RUBY_SYSTEM,
        kind="histogram",
    ),
    StatSpec(
        "MachineType.{machine}.miss_latency_hist_seqr.{phase}",
        name="MachineType.miss_latency_phase_hist_seqr",
        owner_class=RUBY_SYSTEM,
        kind="histogram",
    ),
    StatSpec(
        "MachineType.{machine}.incomplete_times_seqr",
        owner_class=RUBY_SYSTEM,
        kind="scalar",
    ),
    StatSpec(
        "MachineType.{machine}.{stat}",
        name="MachineType.{stat}",
        owner_class=RUBY_SYSTEM,
        kind="histogram",
    ),
    StatSpec(
        "RequestTypeMachineType.{request}.{machine}.{stat}",
        name="RequestTypeMachineType.{stat}",
        owner_class=RUBY_SYSTEM,
        kind="histogram",
    ),
    StatSpec(
        R(r"delayVCHist\.vnet_(?P<vnet>\d+)"),
        name="delayVCHist",
        owner_class=RUBY_SYSTEM,
        kind="histogram",
        dims={"vnet": int},
    ),
    StatSpec(
        R(r"throttle(?P<port>\d+)\.msg_count\.(?P<msg_size>[^.]+)"),
        name="throttle.msg_count",
        owner_class=SWITCH,
        kind="vector",
        dims={"port": int},
        element_dim="vnet",
    ),
    StatSpec(
        R(r"throttle(?P<port>\d+)\.(?P<stat>[^.]+)"),
        name="throttle.{stat}",
        owner_class=SWITCH,
        dims={"port": int},
    ),
]

GEM5_SPECS = FORMULA_SKIPS + VECTOR2D + RUBY
