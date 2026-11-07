"""Initial planning, repair and continuous planning on the hosted solver.

One solver contract covers all three (the solver's pinning-and-repair design): a
request carries the records, optionally the previous plan (`prior_plan`) and
per-container pins.

    frozen  the container stays on this sailing (booked with the carrier)
    forbid  the container must not use this sailing (e.g. the carrier rolled it)
    prefer  keep the prior sailing unless moving pays more than `change_cost`
    (none)  `pin_default` applies; "free" plans it from scratch

The continuous-planning policy below decides those modes from the state of the
world; the solver decides everything else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .client import SolverClient, SolverError
from .lowering import Pin, lower, placements_to_plan
from .model import Plan, Scenario
from .scorer import Score, score, sailing_loads


class PlanningFailed(RuntimeError):
    """The solver returned no admissible plan. Carries the typed reasons."""

    def __init__(self, message: str, details: Any):
        self.details = details
        super().__init__(f"{message}: {details}")


@dataclass
class PlanResult:
    plan: Plan
    score: Score
    response: Dict[str, Any]
    request: Dict[str, Any]
    pins: List[Pin] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def certificate(self) -> Dict[str, Any]:
        return (self.response.get("solution") or {}).get("admission") or {}

    @property
    def solve_ms(self) -> Optional[int]:
        return self.response.get("solve_time_ms")


def solve(
    client: Optional[SolverClient],
    scenario: Scenario,
    *,
    prior_plan: Optional[Plan] = None,
    pins: Optional[List[Pin]] = None,
    pin_default: Optional[str] = None,
    change_cost: Optional[float] = None,
    notes: Optional[List[str]] = None,
) -> PlanResult:
    lowered = lower(
        scenario,
        prior_plan=prior_plan,
        pins=pins,
        pin_default=pin_default,
        change_cost=change_cost,
    )
    if client is None:
        response = _offline_response(lowered.problem)
    else:
        response = client.solve(lowered.problem)
    solution = response.get("solution") or {}
    admission = solution.get("admission") or {}
    if solution.get("is_feasible") is not True:
        raise PlanningFailed(
            "solver returned no feasible plan",
            {"reason": admission.get("reason"), "violations": admission.get("violations")},
        )
    plan = placements_to_plan(solution.get("placements") or [])
    missing = set(lowered.container_ids) - set(plan)
    if missing:
        raise PlanningFailed("feasible answer did not place every container", sorted(missing))
    result = PlanResult(
        plan=plan,
        score=score(scenario, plan, prior=prior_plan),
        response=response,
        request=lowered.problem,
        pins=list(pins or []),
        notes=list(notes or []) + lowered.notes,
    )
    if not result.score.feasible:
        # The independent scorer disagrees with the solver's certificate: never
        # pass that plan on as if it were good.
        raise PlanningFailed("independent scorer rejected the solver's plan", result.score.hard_violations)
    return result


def _offline_response(problem: Dict[str, Any]) -> Dict[str, Any]:
    """An --offline dry run: the exact MIP answers instead of the hosted solver.
    It carries no hosted certificate, says so, and its plan still has to pass
    the independent scorer like any other."""
    from .optimum import solve_optimum

    plan, info = solve_optimum(problem)
    return {
        "engine": "exact MIP (offline dry run)",
        "solve_time_ms": int(info["seconds"] * 1000),
        "solution": {
            "is_feasible": True,
            "placements": [{"entity": c, "group": g} for c, g in sorted(plan.items())],
            "admission": {"reason": "offline dry run: no hosted certificate", "optimum": info},
        },
    }


def initial_plan(client: SolverClient, scenario: Scenario) -> PlanResult:
    return solve(client, scenario)


def certify(client: SolverClient, scenario: Scenario, plan: Plan) -> PlanResult:
    """Ask the solver to judge a plan it did not make: freeze every booking.

    The solver scores the frozen plan with the pack's rules and returns its
    certificate. A plan that breaks a rule which can be judged on the pinned
    set alone (lane, cutoff, capacity) is refused as `pinned_set_infeasible`,
    naming the rule and the containers, rather than being quietly repaired.
    """
    pins = [Pin(cid, "frozen", sid) for cid, sid in sorted(plan.items())]
    return solve(client, scenario, prior_plan=plan, pins=pins)


# -- continuous planning ----------------------------------------------------

@dataclass
class ReplanPolicy:
    freeze_days: int = 7          # cutoff within this many days of today -> booked, frozen
    change_cost: float = 25.0     # price of moving a container no event touched


def replan(
    client: Optional[SolverClient],
    scenario: Scenario,
    prior: Plan,
    *,
    disturbed: Set[str],
    forbidden: Optional[Dict[str, Set[str]]] = None,
    locked: Optional[Dict[str, str]] = None,
    policy: ReplanPolicy = ReplanPolicy(),
) -> PlanResult:
    """Repair `prior` after events.

    disturbed  containers an event touched; planned freely
    forbidden  container -> sailing ids it may not use (rolled, refused)
    locked     container -> sailing id a planner locked by hand
    Everything else on a sailing whose cutoff falls within `freeze_days` of
    today is frozen (booked with the carrier) as far as the carrier has
    acknowledged the space: a booking on space still pending is not booked
    yet. The rest prefers its prior sailing and moves only when that gains
    more than `change_cost`.
    """
    forbidden = forbidden or {}
    locked = locked or {}
    sailings = {s.id: s for s in scenario.active_sailings()}
    feu = {c.id: c.feu for c in scenario.containers}
    pins: List[Pin] = []
    notes: List[str] = []
    frozen_load: Dict[str, float] = {}

    def freeze(cid: str, sid: str, limit: float) -> bool:
        if frozen_load.get(sid, 0.0) + feu[cid] > limit + 1e-9:
            return False
        frozen_load[sid] = frozen_load.get(sid, 0.0) + feu[cid]
        return True

    for cid, sid in sorted(locked.items()):
        if sid not in sailings:
            raise PlanningFailed("locked to a sailing that is not available", {cid: sid})
        if not freeze(cid, sid, sailings[sid].ceiling_feu):
            raise PlanningFailed("locks exceed the space on sale", {cid: sid})
        pins.append(Pin(cid, "frozen", sid))
    for c in scenario.containers:
        cid = c.id
        if cid in locked:
            continue
        sid = prior.get(cid)
        if cid in disturbed or sid not in sailings:
            pass    # no pin: pin_default "free" plans it from scratch
        else:
            s = sailings[sid]
            booked = s.cutoff_day - scenario.today <= policy.freeze_days
            if booked and c.ready_day <= s.cutoff_day and freeze(cid, sid, float(s.confirmed_feu)):
                pins.append(Pin(cid, "frozen", sid))
            else:
                if booked:
                    notes.append(f"{cid}: on {sid} near cutoff but not within acknowledged space; left to the repair")
                pins.append(Pin(cid, "prefer"))
        for bad in sorted(forbidden.get(cid, ())):
            if bad in sailings:
                pins.append(Pin(cid, "forbid", bad))
    known = {c.id for c in scenario.containers}
    return solve(
        client,
        scenario,
        prior_plan={cid: sid for cid, sid in prior.items() if cid in known},
        pins=pins,
        pin_default="free",
        change_cost=policy.change_cost,
        notes=notes,
    )


def pin_summary(pins: List[Pin]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for p in pins:
        out[p.mode] = out.get(p.mode, 0) + 1
    return out


def spot_by_sailing(scenario: Scenario, plan: Plan) -> Dict[str, float]:
    load = sailing_loads(scenario, plan)
    return {
        s.id: round(load.get(s.id, 0.0) - s.allocation_feu, 1)
        for s in scenario.active_sailings()
        if load.get(s.id, 0.0) > s.allocation_feu
    }


__all__ = [
    "PlanResult", "PlanningFailed", "ReplanPolicy", "SolverError",
    "certify", "initial_plan", "pin_summary", "replan", "solve", "spot_by_sailing",
]
