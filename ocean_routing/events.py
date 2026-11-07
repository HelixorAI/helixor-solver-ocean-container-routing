"""Carrier and container events, and what each one disturbs in the current plan.

Events change the world (the scenario). What the planner does about them is a
separate decision: `apply_events` reports which containers an event touched
(planned freely in the repair), which sailings a container may no longer use
(forbid pins) and which bookings a planner locked (frozen pins). Every event
names real records; an event about an unknown sailing or container is refused,
never skipped.

Event shapes (JSON):

  carrier side
    {"type": "sailing_cancelled", "sailing": id}                  blank sailing
    {"type": "sailing_delayed",   "sailing": id, "days": n}       ETD/ETA slip (cutoff too)
    {"type": "allocation_changed","sailing": id, "allocation_feu": x}
    {"type": "spot_changed",      "sailing": id, "spot_feu": x}
    {"type": "container_rolled",  "container": id}                carrier rolls it off its sailing
  container / vendor side
    {"type": "containers_added",  "containers": [Container...]}
    {"type": "container_cancelled","container": id}
    {"type": "container_expedited","container": id, "promise_day": d}
    {"type": "cargo_ready_changed","container": id, "ready_day": d}
    {"type": "container_changed", "container": id, "size": "20GP"|"40GP"|"40HC"}
  planner
    {"type": "container_locked",  "container": id, "sailing": id}
  space beyond the allocation (see ocean_routing/space.py)
    {"type": "space_requested",   "sailing": id, "feu": x}        we ask: over-allocation or a spot booking
    {"type": "space_confirmed",   "sailing": id, "feu": x}        carrier acknowledges part of what is pending
    {"type": "space_rejected",    "sailing": id, "feu": x}        carrier refuses; it will take no more on that sailing
    {"type": "space_withdrawn",   "sailing": id, "feu": x}        we cancel a pending request
    {"type": "space_released",    "sailing": id, "feu": x}        we hand held extra space back
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from .model import Container, Plan, Scenario


class InvalidEvent(ValueError):
    pass


@dataclass
class Disruption:
    disturbed: Set[str] = field(default_factory=set)
    forbidden: Dict[str, Set[str]] = field(default_factory=dict)
    locked: Dict[str, str] = field(default_factory=dict)
    log: List[str] = field(default_factory=list)


@dataclass
class Cycle:
    today: int
    label: str
    events: List[Dict[str, Any]]


def load_cycles(path: Path) -> List[Cycle]:
    raw = json.loads(Path(path).read_text())
    return [Cycle(today=int(c["today"]), label=str(c.get("label", "")), events=list(c["events"])) for c in raw["cycles"]]


def depart(scenario: Scenario, plan: Plan, today: int) -> Tuple[Scenario, Plan, List[str]]:
    """Advance the clock. Sailings that left before `today` leave the problem
    with the containers booked on them; their utilisation is final."""
    if today < scenario.today:
        raise InvalidEvent(f"time runs forward: today {today} < {scenario.today}")
    world = scenario.copy()
    world.today = today
    departed = {s.id for s in world.sailings if s.etd_day < today}
    sailed = sorted(cid for cid, sid in plan.items() if sid in departed)
    world.sailings = [s for s in world.sailings if s.id not in departed]
    world.containers = [c for c in world.containers if c.id not in set(sailed)]
    remaining = {cid: sid for cid, sid in plan.items() if cid not in set(sailed)}
    log = [f"departed: {len(departed)} sailings, {len(sailed)} containers sailed"] if departed else []
    return world, remaining, log


def apply_events(scenario: Scenario, plan: Plan, events: List[Dict[str, Any]]) -> Tuple[Scenario, Disruption]:
    world = scenario.copy()
    d = Disruption()
    sailings = {s.id: s for s in world.sailings}
    containers = {c.id: c for c in world.containers}

    def sailing(event: Dict[str, Any]):
        sid = event.get("sailing")
        if sid not in sailings:
            raise InvalidEvent(f"{event['type']}: unknown or departed sailing {sid!r}")
        return sailings[sid]

    def container(event: Dict[str, Any]) -> Container:
        cid = event.get("container")
        if cid not in containers:
            raise InvalidEvent(f"{event['type']}: unknown or sailed container {cid!r}")
        return containers[cid]

    def on_board(sid: str) -> List[str]:
        return sorted(cid for cid, booked in plan.items() if booked == sid and cid in containers)

    for event in events:
        kind = event.get("type")
        if kind == "sailing_cancelled":
            s = sailing(event)
            s.status = "cancelled"
            moved = on_board(s.id)
            d.disturbed.update(moved)
            d.log.append(f"{s.id} blank sailing: {len(moved)} containers need a new sailing")
        elif kind == "sailing_delayed":
            s = sailing(event)
            days = int(event["days"])
            s.etd_day += days
            s.eta_day += days
            s.cutoff_day += days
            moved = on_board(s.id)
            d.disturbed.update(moved)
            d.log.append(f"{s.id} delayed {days} d (ETA day {s.eta_day}): {len(moved)} containers re-planned")
        elif kind == "allocation_changed":
            s = sailing(event)
            held = s.held_extra_feu
            before, s.allocation_feu = s.allocation_feu, float(event["allocation_feu"])
            s.confirmed_feu = s.allocation_feu + held       # the contract acknowledges the new allocation
            if s.allocation_feu < before:
                # Containers stay where they are (spot space may absorb them);
                # the repair decides whether any should move.
                d.disturbed.update(on_board(s.id))
            d.log.append(f"{s.id} allocation {before:g} -> {s.allocation_feu:g} FEU")
        elif kind == "spot_changed":
            s = sailing(event)
            # Space the carrier already acknowledged stays ours; a request it
            # can no longer fill is rejected with the sale.
            before, s.spot_feu = s.spot_feu, max(float(event["spot_feu"]), s.held_extra_feu)
            excess = max(0.0, s.committed_feu - s.ceiling_feu)
            s.pending_feu -= excess
            if s.spot_feu < before:
                d.disturbed.update(on_board(s.id))
            d.log.append(f"{s.id} spot space {before:g} -> {s.spot_feu:g} FEU"
                         + (f"; {excess:g} FEU pending request rejected" if excess else ""))
        elif kind == "container_rolled":
            c = container(event)
            sid = plan.get(c.id)
            if sid is None:
                raise InvalidEvent(f"container_rolled: {c.id} has no booking to roll")
            d.disturbed.add(c.id)
            d.forbidden.setdefault(c.id, set()).add(sid)
            d.log.append(f"{c.id} rolled by carrier off {sid}")
        elif kind == "containers_added":
            added = [Container(**row) for row in event["containers"]]
            for c in added:
                if c.id in containers:
                    raise InvalidEvent(f"containers_added: {c.id} already exists")
                if c.ready_day < world.today:
                    raise InvalidEvent(f"containers_added: {c.id} ready day {c.ready_day} is in the past")
                containers[c.id] = c
                world.containers.append(c)
                d.disturbed.add(c.id)
            d.log.append(f"vendor added {len(added)} containers")
        elif kind == "container_cancelled":
            c = container(event)
            world.containers = [x for x in world.containers if x.id != c.id]
            del containers[c.id]
            d.disturbed.discard(c.id)
            d.log.append(f"{c.id} cancelled (PO cancelled)")
        elif kind == "container_expedited":
            c = container(event)
            before, c.promise_day = c.promise_day, int(event["promise_day"])
            d.disturbed.add(c.id)
            d.log.append(f"{c.id} expedited: promise day {before} -> {c.promise_day}")
        elif kind == "cargo_ready_changed":
            c = container(event)
            before, c.ready_day = c.ready_day, int(event["ready_day"])
            d.disturbed.add(c.id)
            d.log.append(f"{c.id} cargo ready day {before} -> {c.ready_day}")
        elif kind == "container_changed":
            c = container(event)
            before, c.size = c.size, str(event["size"])
            c.feu  # validates the size
            d.disturbed.add(c.id)
            d.log.append(f"{c.id} equipment {before} -> {c.size}")
        elif kind == "container_locked":
            c = container(event)
            s = sailing(event)
            d.locked[c.id] = s.id
            d.disturbed.discard(c.id)
            d.log.append(f"{c.id} locked to {s.id} by planner")
        elif kind in SPACE_EVENTS:
            s = sailing(event)
            d.log.append(_apply_space(s, kind, _feu(event)))
        else:
            raise InvalidEvent(f"unknown event type {kind!r}")
    # A lock wins over anything else that touched the container.
    d.disturbed -= set(d.locked)
    d.disturbed &= set(containers)
    return world, d


SPACE_EVENTS = ("space_requested", "space_confirmed", "space_rejected", "space_withdrawn", "space_released")


def _feu(event: Dict[str, Any]) -> float:
    feu = float(event.get("feu", 0))
    if feu <= 0 or abs(feu * 2 - round(feu * 2)) > 1e-9:
        raise InvalidEvent(f"{event['type']}: feu must be a positive multiple of 0.5, got {event.get('feu')!r}")
    return feu


def _apply_space(s, kind: str, feu: float) -> str:
    """One step of the space lifecycle on one sailing. Each step is checked
    against the sailing's state: a carrier cannot confirm more than was asked,
    and we cannot release space we never held."""
    if kind == "space_requested":
        if s.committed_feu + feu > s.ceiling_feu + 1e-9:
            raise InvalidEvent(f"space_requested: {s.id} has {s.ceiling_feu - s.committed_feu:g} FEU left to ask for, not {feu:g}")
        s.pending_feu += feu
        return f"{s.id}: requested {feu:g} FEU beyond the allocation (pending {s.pending_feu:g})"
    if kind in ("space_confirmed", "space_rejected", "space_withdrawn") and feu > s.pending_feu + 1e-9:
        raise InvalidEvent(f"{kind}: {s.id} has {s.pending_feu:g} FEU pending, not {feu:g}")
    if kind == "space_confirmed":
        s.pending_feu -= feu
        s.confirmed_feu += feu
        return f"{s.id}: carrier confirmed {feu:g} FEU (confirmed {s.confirmed_feu:g})"
    if kind == "space_rejected":
        s.pending_feu -= feu
        s.spot_feu = s.committed_feu - s.allocation_feu      # it will take no more on this sailing
        return f"{s.id}: carrier REJECTED {feu:g} FEU; space now capped at {s.ceiling_feu:g} FEU"
    if kind == "space_withdrawn":
        s.pending_feu -= feu
        return f"{s.id}: withdrew {feu:g} FEU pending request"
    if feu > s.held_extra_feu + 1e-9:
        raise InvalidEvent(f"space_released: {s.id} holds {s.held_extra_feu:g} FEU beyond the allocation, not {feu:g}")
    s.confirmed_feu -= feu
    return f"{s.id}: released {feu:g} FEU of held space back to the carrier"
