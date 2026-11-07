"""Domain records for the ocean container routing example.

Days are integers counted from the first day of the planning window (day 0 is
the Monday the window opens). Space is counted in FEU: a 40' container is 1 FEU,
a 20' container is 0.5 FEU.

A carrier grants the customer `allocation_feu` on each sailing and sells up to
`spot_feu` more at spot rates. A plan books each container onto a sailing; FEU
loaded beyond the allocation on that sailing is spot space.

Space beyond the allocation is not the customer's until the carrier
acknowledges it. Each sailing tracks:

    confirmed_feu  space the carrier has acknowledged: the allocation (the
                   contract acknowledges it) plus extra the carrier accepted.
                   Freed space stays confirmed until the customer releases it.
    pending_feu    space requested (over-allocation or a spot booking on the
                   carrier's website) and not yet answered.

A request may be rejected. `Scenario.simulation` holds how much extra each
carrier will really accept; only the carrier simulator reads it, never the
planner or the solver request.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

FEU_BY_SIZE = {"20GP": 0.5, "40GP": 1.0, "40HC": 1.0}


@dataclass
class Sailing:
    id: str
    carrier: str
    service: str
    vessel: str
    voyage: str
    lane: str
    week: int
    cutoff_day: int
    etd_day: int
    eta_day: int
    allocation_feu: float
    spot_feu: float
    status: str = "scheduled"   # scheduled | cancelled
    confirmed_feu: Optional[float] = None    # None: the allocation alone
    pending_feu: float = 0.0

    def __post_init__(self) -> None:
        if self.confirmed_feu is None:
            self.confirmed_feu = self.allocation_feu

    @property
    def ceiling_feu(self) -> float:
        """The most this customer can hold on the sailing: allocation + space on sale."""
        return self.allocation_feu + self.spot_feu

    @property
    def committed_feu(self) -> float:
        """Confirmed plus requested space."""
        return float(self.confirmed_feu) + self.pending_feu

    @property
    def held_extra_feu(self) -> float:
        """Confirmed space beyond the allocation."""
        return max(0.0, float(self.confirmed_feu) - self.allocation_feu)


@dataclass
class Container:
    id: str
    po: str
    vendor: str
    lane: str
    size: str
    ready_day: int
    promise_day: int

    @property
    def feu(self) -> float:
        try:
            return FEU_BY_SIZE[self.size]
        except KeyError:
            raise ValueError(f"container {self.id}: unknown size {self.size!r} (known: {sorted(FEU_BY_SIZE)})")


Plan = Dict[str, str]   # container id -> sailing id


@dataclass
class Scenario:
    name: str
    window_start: str
    weeks: int
    carriers: Dict[str, str]
    sailings: List[Sailing]
    containers: List[Container]
    today: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Carrier behaviour for the replay only: {"carrier_accepts_extra_feu": {sailing: feu}}.
    simulation: Dict[str, Any] = field(default_factory=dict)

    # -- lookups -----------------------------------------------------------

    def sailing(self, sailing_id: str) -> Sailing:
        for s in self.sailings:
            if s.id == sailing_id:
                return s
        raise KeyError(f"unknown sailing {sailing_id!r}")

    def container(self, container_id: str) -> Container:
        for c in self.containers:
            if c.id == container_id:
                return c
        raise KeyError(f"unknown container {container_id!r}")

    def active_sailings(self) -> List[Sailing]:
        return [s for s in self.sailings if s.status != "cancelled"]

    def copy(self) -> "Scenario":
        return copy.deepcopy(self)

    # -- io ----------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "window_start": self.window_start,
            "weeks": self.weeks,
            "today": self.today,
            "carriers": self.carriers,
            "sailings": [asdict(s) for s in self.sailings],
            "containers": [asdict(c) for c in self.containers],
            "metadata": self.metadata,
            "simulation": self.simulation,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Scenario":
        return cls(
            name=raw["name"],
            window_start=raw["window_start"],
            weeks=int(raw["weeks"]),
            today=int(raw.get("today", 0)),
            carriers=dict(raw["carriers"]),
            sailings=[Sailing(**s) for s in raw["sailings"]],
            containers=[Container(**c) for c in raw["containers"]],
            metadata=dict(raw.get("metadata") or {}),
            simulation=dict(raw.get("simulation") or {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1) + "\n")

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        return cls.from_dict(json.loads(Path(path).read_text()))


# -- plans ------------------------------------------------------------------

def plan_to_rows(plan: Plan) -> List[Dict[str, str]]:
    return [{"container": cid, "sailing": sid} for cid, sid in sorted(plan.items())]


def plan_from_rows(rows: List[Dict[str, Any]]) -> Plan:
    plan: Plan = {}
    for row in rows:
        cid = str(row["container"])
        if cid in plan:
            raise ValueError(f"container {cid} is booked twice in the plan")
        plan[cid] = str(row["sailing"])
    return plan


def save_plan(plan: Plan, path: Path, *, source: str, extra: Optional[Dict[str, Any]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body: Dict[str, Any] = {"source": source}
    if extra:
        body.update(extra)
    body["bookings"] = plan_to_rows(plan)
    path.write_text(json.dumps(body, indent=1) + "\n")


def load_plan(path: Path) -> Plan:
    raw = json.loads(Path(path).read_text())
    return plan_from_rows(raw["bookings"] if isinstance(raw, dict) else raw)
