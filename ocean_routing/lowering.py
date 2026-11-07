"""Lower a scenario into the solver's entity/group problem, and lift answers back.

Containers are entities and sailings are groups. Each sailing carries:

    capacity_feu    allocation + spot space on sale (hard cap)
    allocation_feu  the customer's allocation; FEU beyond it is spot space
    confirmed_feu   space the carrier has acknowledged
    committed_feu   acknowledged + requested (awaiting the carrier's answer)

The carrier simulator's acceptance limits (`Scenario.simulation`) are never
lowered: the solver plans on what the carrier has said, not on what it will say.

Whether a sailing gets a container to its destination by the promise date is a
property of the (container, sailing) pair. It is lowered onto the container as
`on_time_sailings`: the ids of same-lane sailings arriving by its promise day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import Plan, Scenario

PIN_MODES = ("frozen", "forbid", "prefer")


@dataclass
class Pin:
    container: str
    mode: str                       # frozen | forbid | prefer
    sailing: Optional[str] = None

    def __post_init__(self) -> None:
        if self.mode not in PIN_MODES:
            raise ValueError(f"pin mode must be one of {PIN_MODES}, got {self.mode!r}")

    def to_request(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"entity": self.container, "mode": self.mode}
        if self.sailing is not None:
            out["group"] = self.sailing
        return out


@dataclass
class Lowered:
    problem: Dict[str, Any]
    sailing_ids: List[str]
    container_ids: List[str]
    notes: List[str] = field(default_factory=list)


def lower(
    scenario: Scenario,
    *,
    prior_plan: Optional[Plan] = None,
    pins: Optional[List[Pin]] = None,
    pin_default: Optional[str] = None,
    change_cost: Optional[float] = None,
) -> Lowered:
    sailings = [
        {
            "id": s.id,
            "carrier": s.carrier,
            "service": s.service,
            "lane": s.lane,
            "week": s.week,
            "cutoff_day": s.cutoff_day,
            "etd_day": s.etd_day,
            "eta_day": s.eta_day,
            "allocation_feu": s.allocation_feu,
            "capacity_feu": s.ceiling_feu,
            "confirmed_feu": s.confirmed_feu,
            "committed_feu": s.committed_feu,
        }
        for s in scenario.active_sailings()
    ]
    containers = [
        {
            "id": c.id,
            "lane": c.lane,
            "size": c.size,
            "feu": c.feu,
            "ready_day": c.ready_day,
            "promise_day": c.promise_day,
            "on_time_sailings": [
                s["id"] for s in sailings
                if s["lane"] == c.lane and s["eta_day"] <= c.promise_day
            ],
        }
        for c in scenario.containers
    ]
    problem: Dict[str, Any] = {"containers": containers, "sailings": sailings}
    known_sailings = {s["id"] for s in sailings}
    known_containers = {c["id"] for c in containers}
    notes: List[str] = []
    if prior_plan:
        prior: Dict[str, str] = {}
        for cid, sid in sorted(prior_plan.items()):
            if cid not in known_containers:
                notes.append(f"prior plan: {cid} is no longer in the window; dropped")
            elif sid not in known_sailings:
                notes.append(f"prior plan: {cid} was on {sid}, which is no longer available; replanned")
            else:
                prior[cid] = sid
        problem["prior_plan"] = prior
    if pins:
        problem["pins"] = [p.to_request() for p in pins]
    if pin_default:
        problem["pin_default"] = pin_default
    if change_cost is not None:
        problem["change_cost"] = change_cost
    return Lowered(
        problem=problem,
        sailing_ids=[s["id"] for s in sailings],
        container_ids=[c["id"] for c in containers],
        notes=notes,
    )


def placements_to_plan(placements: List[Dict[str, str]]) -> Plan:
    """The solver's [{entity, group}] placements as container id -> sailing id."""
    plan: Plan = {}
    for p in placements:
        if p["entity"] in plan:
            raise ValueError(f"{p['entity']} placed twice")
        plan[p["entity"]] = p["group"]
    return plan
