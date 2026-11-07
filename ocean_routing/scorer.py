"""Independent scorer: judges any plan against the scenario, whoever produced it.

It reads only the domain records (never a solver's own report), so the same
numbers can be quoted for a plan from the Helixor solver, from the exact
optimum, or from a planner's spreadsheet. The weighted
total uses the weights declared in the pack manifest.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .model import Plan, Scenario

WEIGHTS = {
    "late_containers": 1000.0,
    "unused_allocation_feu": 100.0,
    "spot_feu": 40.0,
    "days_late": 5.0,
    "unconfirmed_feu": 30.0,
    "new_request_feu": 10.0,
}


@dataclass
class Score:
    feasible: bool
    hard_violations: List[str]
    containers: int
    on_time: int
    late_containers: int
    on_time_pct: float
    days_late: int
    allocated_feu: float
    used_allocation_feu: float
    unused_allocation_feu: float
    utilization_pct: float
    spot_feu: float
    unconfirmed_feu: float
    new_request_feu: float
    weighted_penalty: float
    weekly_utilization: Dict[int, float] = field(default_factory=dict)
    changed_from_prior: Optional[int] = None

    def to_dict(self) -> Dict:
        return asdict(self)

    def line(self) -> str:
        changed = "" if self.changed_from_prior is None else f"  moved={self.changed_from_prior}"
        return (
            f"{'FEASIBLE' if self.feasible else 'INFEASIBLE'}  on-time {self.on_time}/{self.containers} "
            f"({self.on_time_pct:.1f}%)  late={self.late_containers} ({self.days_late} d)  "
            f"allocation {self.used_allocation_feu:g}/{self.allocated_feu:g} FEU ({self.utilization_pct:.1f}%)  "
            f"spot={self.spot_feu:g} FEU  unconfirmed={self.unconfirmed_feu:g}  new-request={self.new_request_feu:g}  penalty={self.weighted_penalty:,.0f}{changed}"
        )


def sailing_loads(scenario: Scenario, plan: Plan) -> Dict[str, float]:
    feu = {c.id: c.feu for c in scenario.containers}
    load: Dict[str, float] = defaultdict(float)
    for cid, sid in plan.items():
        if cid in feu:
            load[sid] += feu[cid]
    return dict(load)


def score(scenario: Scenario, plan: Plan, *, prior: Optional[Plan] = None) -> Score:
    hard: List[str] = []
    sailings = {s.id: s for s in scenario.sailings}
    on_time = late = days_late = 0
    for c in scenario.containers:
        sid = plan.get(c.id)
        s = sailings.get(sid) if sid is not None else None
        if sid is None:
            hard.append(f"{c.id}: not booked")
            continue
        if s is None:
            hard.append(f"{c.id}: unknown sailing {sid}")
            continue
        if s.status == "cancelled":
            hard.append(f"{c.id}: on cancelled sailing {s.id}")
        if s.lane != c.lane:
            hard.append(f"{c.id}: lane {c.lane} on {s.id} ({s.lane})")
        if c.ready_day > s.cutoff_day:
            hard.append(f"{c.id}: ready day {c.ready_day} after cutoff {s.cutoff_day} of {s.id}")
        if s.eta_day <= c.promise_day:
            on_time += 1
        else:
            late += 1
            days_late += s.eta_day - c.promise_day
    extra = set(plan) - {c.id for c in scenario.containers}
    hard.extend(f"{cid}: booked but not in the scenario" for cid in sorted(extra))

    load = sailing_loads(scenario, plan)
    allocated = used = spot = unconfirmed = new_request = 0.0
    week_alloc: Dict[int, float] = defaultdict(float)
    week_used: Dict[int, float] = defaultdict(float)
    for s in scenario.active_sailings():
        on_board = load.get(s.id, 0.0)
        if on_board > s.ceiling_feu + 1e-9:
            hard.append(f"{s.id}: {on_board:g} FEU > allocation {s.allocation_feu:g} + spot {s.spot_feu:g}")
        unconfirmed += max(0.0, on_board - float(s.confirmed_feu))
        new_request += max(0.0, on_board - s.committed_feu)
        in_allocation = min(on_board, s.allocation_feu)
        allocated += s.allocation_feu
        used += in_allocation
        spot += on_board - in_allocation
        if s.allocation_feu > 0:
            week_alloc[s.week] += s.allocation_feu
            week_used[s.week] += in_allocation

    unused = allocated - used
    n = len(scenario.containers)
    penalty = (
        WEIGHTS["late_containers"] * late
        + WEIGHTS["unused_allocation_feu"] * unused
        + WEIGHTS["spot_feu"] * spot
        + WEIGHTS["days_late"] * days_late
        + WEIGHTS["unconfirmed_feu"] * unconfirmed
        + WEIGHTS["new_request_feu"] * new_request
    )
    changed = None
    if prior is not None:
        changed = sum(1 for cid, sid in plan.items() if cid in prior and prior[cid] != sid)
    return Score(
        feasible=not hard,
        hard_violations=hard,
        containers=n,
        on_time=on_time,
        late_containers=late,
        on_time_pct=100.0 * on_time / n if n else 100.0,
        days_late=days_late,
        allocated_feu=allocated,
        used_allocation_feu=used,
        unused_allocation_feu=unused,
        utilization_pct=100.0 * used / allocated if allocated else 100.0,
        spot_feu=spot,
        unconfirmed_feu=unconfirmed,
        new_request_feu=new_request,
        weighted_penalty=penalty,
        weekly_utilization={w: round(100.0 * week_used[w] / week_alloc[w], 1) for w in sorted(week_alloc)},
        changed_from_prior=changed,
    )
