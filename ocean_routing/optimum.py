"""Exact optimum of the same model, as a yardstick (needs `pip install ortools`).

Binary x[c, s] for every eligible pair (same lane, ready by cutoff), each
container booked once, sailing FEU within allocation + spot. Per sailing,
`unused >= allocation - load`, `spot >= load - allocation`,
`unconfirmed >= load - confirmed` and `new_request >= load - committed` (all
>= 0) are exact at the optimum because all are minimised. The objective is the pack
manifest's six weighted soft rules, plus an optional change cost against a
prior plan, so the integer program states the manifest's model exactly. A
proven optimum tells you how far any plan is from the best one possible; it is
not part of the hosted flow.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Dict, Tuple

from .lowering import lower
from .model import Plan, Scenario, save_plan
from .scorer import WEIGHTS, score


def solve_optimum(problem: Dict, *, time_limit_s: float = 120.0) -> Tuple[Plan, Dict]:
    """Solve the lowered request exactly, honouring its prior plan and pins the
    way the manifest declares them (frozen, forbid, prefer + change_cost)."""
    from ortools.linear_solver import pywraplp

    prior: Dict[str, str] = dict(problem.get("prior_plan") or {})
    frozen: Dict[str, str] = {}
    forbidden: Dict[str, set] = {}
    prefer = set()
    for pin in problem.get("pins") or []:
        if pin["mode"] == "frozen":
            frozen[pin["entity"]] = pin.get("group") or prior[pin["entity"]]
        elif pin["mode"] == "forbid":
            forbidden.setdefault(pin["entity"], set()).add(pin["group"])
        else:
            prefer.add(pin["entity"])
    if problem.get("pin_default") == "prefer":
        prefer |= set(prior) - set(frozen)
    change_cost = float(problem.get("change_cost") or 0.0)
    solver = pywraplp.Solver.CreateSolver("SCIP") or pywraplp.Solver.CreateSolver("CBC")
    if solver is None:
        raise SystemExit("no MIP backend available in this ortools build")
    solver.SetTimeLimit(int(time_limit_s * 1000))
    objective = solver.Objective()
    x: Dict[Tuple[str, str], object] = {}
    load: Dict[str, list] = {s["id"]: [] for s in problem["sailings"]}
    for c in problem["containers"]:
        eligible = [
            s for s in problem["sailings"]
            if s["lane"] == c["lane"] and c["ready_day"] <= s["cutoff_day"]
        ]
        if c["id"] in frozen:
            eligible = [s for s in eligible if s["id"] == frozen[c["id"]]]
        if c["id"] in forbidden:
            eligible = [s for s in eligible if s["id"] not in forbidden[c["id"]]]
        if not eligible:
            raise SystemExit(f"{c['id']}: no eligible sailing; the instance is infeasible")
        row = solver.Constraint(1, 1)
        for s in eligible:
            var = solver.BoolVar(f"x[{c['id']},{s['id']}]")
            x[(c["id"], s["id"])] = var
            row.SetCoefficient(var, 1)
            days_late = max(0, s["eta_day"] - c["promise_day"])
            cost = WEIGHTS["days_late"] * days_late + (WEIGHTS["late_containers"] if days_late else 0.0)
            if c["id"] in prefer and prior.get(c["id"]) not in (None, s["id"]):
                cost += change_cost
            objective.SetCoefficient(var, cost)
            load[s["id"]].append((var, c["feu"]))
    for s in problem["sailings"]:
        terms = load[s["id"]]
        cap = solver.Constraint(0, s["capacity_feu"])
        unused = solver.NumVar(0, solver.infinity(), f"unused[{s['id']}]")
        spot = solver.NumVar(0, solver.infinity(), f"spot[{s['id']}]")
        # unused + load >= allocation ;  load - spot <= allocation
        u_row = solver.Constraint(s["allocation_feu"], solver.infinity())
        s_row = solver.Constraint(-solver.infinity(), s["allocation_feu"])
        u_row.SetCoefficient(unused, 1)
        s_row.SetCoefficient(spot, -1)
        for var, feu in terms:
            cap.SetCoefficient(var, feu)
            u_row.SetCoefficient(var, feu)
            s_row.SetCoefficient(var, feu)
        objective.SetCoefficient(unused, WEIGHTS["unused_allocation_feu"])
        objective.SetCoefficient(spot, WEIGHTS["spot_feu"])
        # load - unconfirmed <= confirmed ;  load - new_request <= committed
        for name, bound, weight in (("unconfirmed", s["confirmed_feu"], WEIGHTS["unconfirmed_feu"]),
                                    ("new_request", s["committed_feu"], WEIGHTS["new_request_feu"])):
            over = solver.NumVar(0, solver.infinity(), f"{name}[{s['id']}]")
            row = solver.Constraint(-solver.infinity(), bound)
            row.SetCoefficient(over, -1)
            for var, feu in terms:
                row.SetCoefficient(var, feu)
            objective.SetCoefficient(over, weight)
    objective.SetMinimization()
    started = time.perf_counter()
    status = solver.Solve()
    elapsed = time.perf_counter() - started
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        raise SystemExit(f"MIP finished without a solution (status {status})")
    plan: Plan = {cid: sid for (cid, sid), var in x.items() if var.solution_value() > 0.5}
    info = {
        "proven_optimal": status == pywraplp.Solver.OPTIMAL,
        "objective": round(objective.Value(), 3),
        "best_bound": round(objective.BestBound(), 3),
        "seconds": round(elapsed, 2),
        "backend": solver.SolverVersion(),
    }
    return plan, info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=Path("data/scenario.json"))
    parser.add_argument("--out", type=Path, default=Path("out/plan-optimum.json"))
    parser.add_argument("--time-limit", type=float, default=120.0)
    args = parser.parse_args()
    scenario = Scenario.load(args.scenario)
    plan, info = solve_optimum(lower(scenario).problem, time_limit_s=args.time_limit)
    sc = score(scenario, plan)
    save_plan(plan, args.out, source="exact MIP (ortools)", extra={"optimum": info, "score": sc.to_dict()})
    print(f"{'optimal' if info['proven_optimal'] else 'best found'} in {info['seconds']}s  bound={info['best_bound']:,.0f}")
    print(sc.line())


if __name__ == "__main__":
    main()
