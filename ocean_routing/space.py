"""Space beyond the allocation: what to ask the carrier for, and how it answers.

A plan may book containers beyond a sailing's allocation. That space is the
customer's only once the carrier acknowledges it, so after every plan the
planner works out what to say to each carrier:

    request   the plan needs more than is confirmed + pending  -> ask for the gap
    hold      the plan needs less than is confirmed            -> keep it; a later
              change (a surge, a roll, a blank sailing) may need it
    release   held extra space on a sailing whose cutoff is within
              `release_days`, or a pending request it no longer needs   -> give it back

Requests and releases are events (`space_requested`, `space_withdrawn`,
`space_released`), applied through the same event path as everything else.

`CarrierSimulator` stands in for the carriers in the replay: it answers each
pending request from the acceptance limits in `Scenario.simulation`, which the
planner never reads. What a carrier will not take is rejected, and that
sailing is then capped at what the carrier has accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .model import Plan, Scenario
from .scorer import sailing_loads


@dataclass
class SpaceActions:
    events: List[Dict[str, Any]] = field(default_factory=list)
    held: Dict[str, float] = field(default_factory=dict)      # sailing -> idle confirmed extra FEU kept

    def total(self, kind: str) -> float:
        return sum(e["feu"] for e in self.events if e["type"] == kind)

    def summary(self) -> Dict[str, float]:
        return {
            "requested_feu": self.total("space_requested"),
            "withdrawn_feu": self.total("space_withdrawn"),
            "released_feu": self.total("space_released"),
            "held_idle_feu": sum(self.held.values()),
        }


def carrier_actions(scenario: Scenario, plan: Plan, *, release_days: int = 2) -> SpaceActions:
    """What to ask each carrier for so the plan is backed by space, and what
    to hold or give back."""
    out = SpaceActions()
    load = sailing_loads(scenario, plan)
    for s in scenario.active_sailings():
        need = load.get(s.id, 0.0)
        if need > s.committed_feu + 1e-9:
            out.events.append({"type": "space_requested", "sailing": s.id, "feu": need - s.committed_feu})
            continue
        spare = s.committed_feu - max(need, s.allocation_feu)
        if spare <= 1e-9:
            continue
        if s.cutoff_day - scenario.today <= release_days:
            withdraw = min(s.pending_feu, spare)
            if withdraw > 1e-9:
                out.events.append({"type": "space_withdrawn", "sailing": s.id, "feu": withdraw})
            release = min(s.held_extra_feu, spare - withdraw)
            if release > 1e-9:
                out.events.append({"type": "space_released", "sailing": s.id, "feu": release})
        else:
            idle = min(s.held_extra_feu, spare)
            if idle > 1e-9:
                out.held[s.id] = idle
    return out


class CarrierSimulator:
    """Answers pending requests the way the scenario says each carrier would."""

    def __init__(self, scenario: Scenario):
        self.accepts: Dict[str, float] = dict((scenario.simulation or {}).get("carrier_accepts_extra_feu") or {})

    def respond(self, scenario: Scenario) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for s in scenario.active_sailings():
            if s.pending_feu <= 1e-9:
                continue
            room = max(0.0, self.accepts.get(s.id, 0.0) - s.held_extra_feu)
            accept = min(s.pending_feu, room)
            if accept > 1e-9:
                events.append({"type": "space_confirmed", "sailing": s.id, "feu": accept})
            if s.pending_feu - accept > 1e-9:
                events.append({"type": "space_rejected", "sailing": s.id, "feu": s.pending_feu - accept})
        return events
