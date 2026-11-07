"""Pinning scenarios: one disrupted world, one base plan, one pin choice per run.

Every scenario starts from the same place: the base plan (by default the plan
adopted from another planning system) and the same carrier event, a blanked
sailing that leaves its containers without a booking. Only the pins change, so
the table shows what each pin mode does on its own:

    no prior plan       re-plan from scratch
    prefer, cost 0      a warm start; any move is free
    prefer, cost 25     move only when the move gains more than 25
    prefer, cost 1000   move only for displaced containers or a late one saved
    freeze week 0       bookings on week-0 sailings are with the carrier
    forbid a sailing    the carrier refuses one sailing's containers
    over-full freeze    frozen pins exceed a sailing's space: refused, not repaired

Every row is checked by the independent scorer, and pins are checked against
the returned plan: a frozen container that moved, or a container left on a
forbidden sailing, fails the run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import planner
from .client import SolverClient, SolverError
from .events import apply_events
from .lowering import Pin, lower
from .model import Plan, Scenario
from .scorer import score

BLANKED = "BORS-TP1-W01"
REFUSED = "COBT-CPX-W01"
OVERFILLED = "ATLS-PSX-W01"


@dataclass
class Case:
    name: str
    why: str
    prior: bool = True
    pins: Callable[[Scenario, Plan], List[Pin]] = lambda world, base: []
    pin_default: Optional[str] = "prefer"
    change_cost: Optional[float] = 25.0
    expect_refusal: bool = False


def _freeze_week0(world: Scenario, base: Plan) -> List[Pin]:
    week0 = {s.id for s in world.active_sailings() if s.week == 0}
    return [Pin(cid, "frozen", sid) for cid, sid in sorted(base.items()) if sid in week0]


def _forbid_refused(world: Scenario, base: Plan) -> List[Pin]:
    return [Pin(cid, "forbid", REFUSED) for cid, sid in sorted(base.items()) if sid == REFUSED]


def _overfill(world: Scenario, base: Plan) -> List[Pin]:
    sailing = world.sailing(OVERFILLED)
    room = sailing.allocation_feu + sailing.spot_feu
    pins, feu = [], 0.0
    for c in world.containers:
        if c.lane == sailing.lane and c.ready_day <= sailing.cutoff_day and feu <= room:
            pins.append(Pin(c.id, "frozen", sailing.id))
            feu += c.feu
    return pins


CASES = [
    Case("no prior plan", "re-plan from scratch", prior=False, pin_default=None, change_cost=None),
    Case("prefer, change cost 0", "warm start, moves are free", change_cost=0.0),
    Case("prefer, change cost 25", "the example's default", change_cost=25.0),
    Case("prefer, change cost 1000", "move only what must move", change_cost=1000.0),
    Case("freeze week 0, prefer the rest", "week-0 bookings are with the carrier", pins=_freeze_week0),
    Case(f"forbid {REFUSED}, prefer the rest", "the carrier refuses that sailing's containers", pins=_forbid_refused),
    Case(f"freeze too much onto {OVERFILLED}", "frozen pins exceed the space on sale",
         pins=_overfill, expect_refusal=True),
]


@dataclass
class Row:
    case: str
    why: str
    pins: Dict[str, int] = field(default_factory=dict)
    pin_default: Optional[str] = None
    change_cost: Optional[float] = None
    outcome: str = ""
    penalty: Optional[float] = None
    late: Optional[int] = None
    days_late: Optional[int] = None
    utilization_pct: Optional[float] = None
    spot_feu: Optional[float] = None
    moved: Optional[int] = None
    optimum_penalty: Optional[float] = None
    seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def disrupted_world(scenario: Scenario, base: Plan) -> Scenario:
    world, _ = apply_events(scenario, base, [{"type": "sailing_cancelled", "sailing": BLANKED}])
    return world


def check_pins(plan: Plan, pins: List[Pin]) -> List[str]:
    broken = []
    for p in pins:
        if p.mode == "frozen" and plan.get(p.container) != p.sailing:
            broken.append(f"{p.container} frozen to {p.sailing} but planned on {plan.get(p.container)}")
        if p.mode == "forbid" and plan.get(p.container) == p.sailing:
            broken.append(f"{p.container} left on forbidden {p.sailing}")
    return broken


def run_case(client: SolverClient, world: Scenario, base: Plan, case: Case, with_optimum: bool = True) -> Row:
    pins = case.pins(world, base)
    prior = base if case.prior else None
    row = Row(case.name, case.why, planner.pin_summary(pins), case.pin_default if case.prior else None,
              case.change_cost if case.prior else None)
    started = time.perf_counter()
    try:
        result = planner.solve(client, world, prior_plan=prior, pins=pins,
                               pin_default=case.pin_default, change_cost=case.change_cost)
    except SolverError as exc:
        row.seconds = round(time.perf_counter() - started, 1)
        body = exc.body if isinstance(exc.body, dict) else {}
        missing = ", ".join(body.get("missing") or [])
        row.outcome = f"refused {exc.status} {body.get('failure_code', '')}: {missing}".strip(": ")
        if not case.expect_refusal:
            raise
        return row
    row.seconds = round(time.perf_counter() - started, 1)
    if case.expect_refusal:
        raise planner.PlanningFailed("solver accepted pins that should be refused", row.pins)
    broken = check_pins(result.plan, pins)
    if broken:
        raise planner.PlanningFailed(f"{case.name}: the solver broke a pin", broken)
    sc = score(world, result.plan, prior=base)
    row.outcome = "feasible, pins held" if pins else "feasible"
    row.penalty, row.late, row.days_late = sc.weighted_penalty, sc.late_containers, sc.days_late
    row.utilization_pct, row.spot_feu, row.moved = round(sc.utilization_pct, 1), sc.spot_feu, sc.changed_from_prior
    if with_optimum:
        try:
            from .optimum import solve_optimum

            optimum, _ = solve_optimum(lower(world, prior_plan=prior, pins=pins, pin_default=case.pin_default,
                                             change_cost=case.change_cost).problem)
            row.optimum_penalty = score(world, optimum).weighted_penalty
        except ImportError:
            pass
    return row


def describe_pins(r: Row) -> str:
    parts = [f"{n} {m}" for m, n in sorted(r.pins.items())]
    if r.pin_default:
        cost = "" if r.change_cost is None else f", change cost {r.change_cost:g}"
        parts.append(f"{'others' if parts else 'all'} {r.pin_default}{cost}")
    return "; ".join(parts) or "none (no prior plan)"


def table(rows: List[Row]) -> str:
    lines = ["| Scenario | Pins | Outcome | Penalty | Late (days) | Allocation used | Spot FEU | Moved | Optimum, same pins | Time |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        pins = describe_pins(r)
        num = lambda v, f="{:,.0f}": "" if v is None else f.format(v)
        late = "" if r.late is None else f"{r.late} ({r.days_late})"
        lines.append(f"| {r.case} | {pins} | {r.outcome} | {num(r.penalty)} | {late} | "
                     f"{num(r.utilization_pct, '{}%')} | {num(r.spot_feu, '{:g}')} | {num(r.moved, '{}')} | "
                     f"{num(r.optimum_penalty)} | {num(r.seconds, '{} s')} |")
    return "\n".join(lines)
