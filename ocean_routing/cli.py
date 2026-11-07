"""Command line for the example. Run `python -m ocean_routing --help`."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import planner
from .client import SolverClient, SolverError
from .events import apply_events, depart, load_cycles
from .lowering import lower
from .model import Plan, Scenario, load_plan, plan_from_rows, save_plan
from .scorer import score
from .space import CarrierSimulator, SpaceActions, carrier_actions

OUT = Path("out")


def _client() -> SolverClient:
    return SolverClient.from_env()


class _NoClient:
    """Stands in for the hosted client in an --offline dry run."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _report_failure(exc: Exception) -> int:
    if isinstance(exc, SolverError):
        print(f"solver refused ({exc.status}):")
        print(json.dumps(exc.body, indent=1)[:4000])
        return 2
    if isinstance(exc, planner.PlanningFailed):
        print(f"no admissible plan: {exc.args[0]}")
        print(json.dumps(exc.details, indent=1, default=str)[:4000])
        return 3
    raise exc


# -- commands -------------------------------------------------------------

def cmd_publish_pack(args: argparse.Namespace) -> int:
    from .pack import build

    archive = build()
    with _client() as client:
        report = client.validate_pack(archive)["validation"]
        print(f"validation: {report['status']} ({len(report['errors'])} errors, {len(report['warnings'])} warnings)")
        record = client.publish_pack(archive, activate=True)["solver_pack"]
        identity = record["identity"]
        print(f"published {identity['pack_id']} {identity['version']}: {record['status']}  sha256={identity['sha256']}")
        manifest = client.manifest()
        print(f"manifest {manifest['problem_type']}: rules {sorted(manifest['constraints'])}")
    return 0


def print_actions(actions: SpaceActions, indent: str = "  ") -> None:
    for e in actions.events:
        verb = {"space_requested": "request", "space_withdrawn": "withdraw", "space_released": "release"}[e["type"]]
        print(f"{indent}carrier action: {verb} {e['feu']:g} FEU on {e['sailing']}")
    if actions.held:
        print(f"{indent}holding {sum(actions.held.values()):g} FEU of acknowledged space for later on "
              f"{len(actions.held)} sailing(s): {', '.join(sorted(actions.held))}")


def cmd_plan(args: argparse.Namespace) -> int:
    scenario = Scenario.load(args.scenario)
    with _client() as client:
        try:
            result = planner.initial_plan(client, scenario)
        except (SolverError, planner.PlanningFailed) as exc:
            return _report_failure(exc)
    save_plan(result.plan, args.out, source="helixor hosted solver", extra={
        "score": result.score.to_dict(), "solve_ms": result.solve_ms, "certificate": result.certificate,
    })
    print(f"solved in {result.solve_ms} ms -> {args.out}")
    print(result.score.line())
    print(f"certificate: {result.certificate.get('reason')}")
    print_actions(carrier_actions(scenario, result.plan), indent="")
    return 0


def cmd_optimum(args: argparse.Namespace) -> int:
    try:
        from .optimum import solve_optimum
    except ImportError:
        print("the exact optimum needs ortools: pip install ortools", file=sys.stderr)
        return 1
    scenario = Scenario.load(args.scenario)
    plan, info = solve_optimum(lower(scenario).problem, time_limit_s=args.time_limit)
    sc = score(scenario, plan)
    save_plan(plan, args.out, source="exact MIP (ortools)", extra={"optimum": info, "score": sc.to_dict()})
    print(f"exact MIP: {'proven optimal' if info['proven_optimal'] else 'best found'} in {info['seconds']} s -> {args.out}")
    print(sc.line())
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    scenario = Scenario.load(args.scenario)
    sc = score(scenario, load_plan(args.plan))
    print(sc.line())
    for v in sc.hard_violations[:20]:
        print(f"  HARD {v}")
    return 0 if sc.feasible else 1


def read_external_plan(path: Path) -> Plan:
    """A plan made elsewhere: our bookings format, or bare [{entity, group}]
    placements as both solvers emit them."""
    raw = json.loads(path.read_text())
    if isinstance(raw, dict) and "bookings" in raw:
        return plan_from_rows(raw["bookings"])
    rows = raw.get("placements") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: expected 'bookings' or 'placements'")
    return plan_from_rows([{"container": r["entity"], "sailing": r["group"]} for r in rows])


def adopt(client: SolverClient, scenario: Scenario, plan: Plan) -> planner.PlanResult:
    """Take over a plan another planning system produced.

    1. every id must resolve (an unknown container or sailing is a hard stop),
    2. the independent scorer judges it,
    3. the hosted solver certifies it with every booking frozen.
    """
    unknown_c = sorted(set(plan) - {c.id for c in scenario.containers})
    unknown_s = sorted({sid for sid in plan.values()} - {s.id for s in scenario.active_sailings()})
    unbooked = sorted({c.id for c in scenario.containers} - set(plan))
    if unknown_c or unknown_s or unbooked:
        raise planner.PlanningFailed("external plan does not match the scenario", {
            "unknown_containers": unknown_c, "unknown_or_cancelled_sailings": unknown_s, "unbooked_containers": unbooked,
        })
    return planner.certify(client, scenario, plan)


def cmd_adopt(args: argparse.Namespace) -> int:
    scenario = Scenario.load(args.scenario)
    plan = read_external_plan(args.plan)
    independent = score(scenario, plan)
    print(f"independent score: {independent.line()}")
    with _client() as client:
        try:
            result = adopt(client, scenario, plan)
        except (SolverError, planner.PlanningFailed) as exc:
            return _report_failure(exc)
    print(f"hosted certificate: feasible={result.certificate.get('is_feasible')}  {result.certificate.get('reason')}")
    if result.plan != plan:
        print("ERROR: the solver moved a frozen booking")
        return 4
    save_plan(plan, args.out, source=f"adopted from {args.plan}", extra={"score": independent.to_dict(), "certificate": result.certificate})
    print(f"adopted -> {args.out}")
    print_actions(carrier_actions(scenario, plan), indent="")
    return 0


def _row(label: str, sc, seconds: Optional[float]) -> Dict[str, Any]:
    return {
        "solver": label, "feasible": sc.feasible, "late": sc.late_containers, "days_late": sc.days_late,
        "on_time_pct": round(sc.on_time_pct, 1), "utilization_pct": round(sc.utilization_pct, 1),
        "unused_feu": sc.unused_allocation_feu, "spot_feu": sc.spot_feu,
        "unconfirmed_feu": sc.unconfirmed_feu, "new_request_feu": sc.new_request_feu, "penalty": sc.weighted_penalty,
        "moved": sc.changed_from_prior, "seconds": seconds,
    }


def cmd_continuous(args: argparse.Namespace) -> int:
    if args.offline and args.compare:
        print("--compare needs the hosted solver; it cannot be combined with --offline", file=sys.stderr)
        return 2
    scenario = Scenario.load(args.scenario)
    plan = read_external_plan(args.start)
    # Offline dry runs never overwrite (or pass for) the hosted results.
    out_dir = args.out or OUT / ("continuous-offline" if args.offline else "continuous")
    out_dir.mkdir(parents=True, exist_ok=True)
    policy = planner.ReplanPolicy(freeze_days=args.freeze_days, change_cost=args.change_cost)
    summary: List[Dict[str, Any]] = []
    carriers = CarrierSimulator(scenario)
    with (_NoClient() if args.offline else _client()) as client:
        start = score(scenario, plan)
        print(f"start plan ({args.start}): {start.line()}")
        # The start plan's space beyond the allocation goes to the carriers now.
        opening = carrier_actions(scenario, plan, release_days=args.release_days)
        print_actions(opening)
        scenario, _ = apply_events(scenario, plan, opening.events)
        for n, cycle in enumerate(load_cycles(args.events), 1):
            print(f"\n=== cycle {n}: day {cycle.today} - {cycle.label}")
            world, prior, log = depart(scenario, plan, cycle.today)
            answers = carriers.respond(world)
            world, answered = apply_events(world, prior, answers)
            world, disruption = apply_events(world, prior, cycle.events)
            for line in log + answered.log + disruption.log:
                print(f"  {line}")
            before = score(world, {cid: sid for cid, sid in prior.items() if cid in {c.id for c in world.containers}})
            print(f"  prior plan on the changed world: {'feasible' if before.feasible else 'BROKEN'} "
                  f"({len(before.hard_violations)} hard violations)  penalty={before.weighted_penalty:,.0f}")
            started = time.perf_counter()
            try:
                result = planner.replan(
                    None if args.offline else client, world, prior,
                    disturbed=disruption.disturbed, forbidden=disruption.forbidden,
                    locked=disruption.locked, policy=policy,
                )
            except (SolverError, planner.PlanningFailed) as exc:
                return _report_failure(exc)
            seconds = round(time.perf_counter() - started, 1)
            pins = planner.pin_summary(result.pins)
            free = len(world.containers) - pins.get("frozen", 0) - pins.get("prefer", 0) - pins.get("forbid", 0)
            print(f"  pins: frozen={pins.get('frozen', 0)} prefer={pins.get('prefer', 0)} "
                  f"forbid={pins.get('forbid', 0)} free={free}")
            for note in result.notes:
                print(f"  note: {note}")
            label = "exact MIP repair (offline)" if args.offline else "helixor repair"
            print(f"  {label} ({seconds}s): {result.score.line()}")
            rows = [_row(label, result.score, seconds)]
            (out_dir / f"cycle-{n}-request.json").write_text(json.dumps({"problem": result.request}, indent=1))
            save_plan(result.plan, out_dir / f"cycle-{n}-{'offline' if args.offline else 'helixor'}.json", source=label,
                      extra={"today": cycle.today, "score": result.score.to_dict(), "pins": pins, "certificate": result.certificate})
            if args.compare and not args.offline:
                rows += compare_cycle(client, world, prior, result.request, out_dir, n)
            actions = carrier_actions(world, result.plan, release_days=args.release_days)
            print_actions(actions)
            world, _ = apply_events(world, result.plan, actions.events)
            space = {
                "confirmed_feu": sum(e["feu"] for e in answers if e["type"] == "space_confirmed"),
                "rejected_feu": sum(e["feu"] for e in answers if e["type"] == "space_rejected"),
                **actions.summary(),
            }
            summary.append({"cycle": n, "today": cycle.today, "label": cycle.label,
                            "events": answered.log + disruption.log, "space": space,
                            "pins": {**pins, "free": free}, "rows": rows})
            scenario, plan = world, result.plan
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(f"\nwrote {out_dir}/summary.json")
    return 0


def compare_cycle(client: SolverClient, world: Scenario, prior: Plan, request: Dict[str, Any],
                  out_dir: Path, n: int) -> List[Dict[str, Any]]:
    """The same repair request through the exact MIP, plus a from-scratch
    hosted solve that ignores the prior plan (churn)."""
    rows: List[Dict[str, Any]] = []
    live_prior = {cid: sid for cid, sid in prior.items() if cid in {c.id for c in world.containers}}
    try:
        from .optimum import solve_optimum
        plan, info = solve_optimum(request)
        sc = score(world, plan, prior=live_prior)
        rows.append(_row("exact optimum (same pins)", sc, info["seconds"]))
        print(f"  exact optimum ({info['seconds']}s): {sc.line()}")
    except ImportError:
        print("  exact optimum skipped: pip install ortools")
    started = time.perf_counter()
    scratch = planner.solve(client, world)
    sc = score(world, scratch.plan, prior=live_prior)
    rows.append(_row("helixor from scratch (no pins)", sc, round(time.perf_counter() - started, 1)))
    print(f"  from scratch, no prior plan: {sc.line()}")
    return rows


def cmd_pinning(args: argparse.Namespace) -> int:
    from . import pinning

    scenario = Scenario.load(args.scenario)
    base = read_external_plan(args.base)
    world = pinning.disrupted_world(scenario, base)
    known = {c.id for c in world.containers}
    base = {cid: sid for cid, sid in base.items() if cid in known}
    print(f"base plan {args.base}; carrier blanks {pinning.BLANKED}")
    rows = []
    with _client() as client:
        for case in pinning.CASES:
            try:
                row = pinning.run_case(client, world, base, case, with_optimum=not args.no_optimum)
            except (SolverError, planner.PlanningFailed) as exc:
                return _report_failure(exc)
            print(f"  {row.case}: {row.outcome}  penalty={row.penalty}  moved={row.moved}  ({row.seconds}s)")
            rows.append(row)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps([r.to_dict() for r in rows], indent=1) + "\n")
    print()
    print(pinning.table(rows))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report import build_report, load_json

    plan = read_external_plan(args.plan)
    if args.request:
        raw = json.loads(args.request.read_text())
        problem = raw.get("problem", raw)
    else:
        problem = lower(Scenario.load(args.scenario)).problem
    unknown = sorted(set(plan) - {c["id"] for c in problem["containers"]})
    if unknown:
        raise SystemExit(f"plan books containers the request does not have: {unknown[:10]}")
    page = build_report(
        problem, plan,
        title=args.title or f"Plan: {args.plan}",
        subtitle=f"{len(problem['containers'])} containers on {len(problem['sailings'])} sailings. "
                 "Charts are the pack manifest's declared ui.visualizations.",
        continuous=load_json(args.continuous), pinning=load_json(args.pinning),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(f"wrote {args.out}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ocean_routing", description=__doc__)
    parser.add_argument("--scenario", type=Path, default=Path("data/scenario.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("publish-pack", help="build, validate, upload and activate the solver pack")

    p = sub.add_parser("plan", help="initial plan on the hosted solver")
    p.add_argument("--out", type=Path, default=OUT / "plan-helixor.json")

    p = sub.add_parser("optimum", help="initial plan with the exact MIP, a proven optimum (needs ortools)")
    p.add_argument("--out", type=Path, default=OUT / "plan-optimum.json")
    p.add_argument("--time-limit", type=float, default=120.0)

    p = sub.add_parser("score", help="score any plan with the independent scorer")
    p.add_argument("plan", type=Path)

    p = sub.add_parser("adopt", help="validate and certify a plan made by another system")
    p.add_argument("plan", type=Path)
    p.add_argument("--out", type=Path, default=OUT / "plan-adopted.json")

    p = sub.add_parser("continuous", help="replay event cycles, repairing the plan each time")
    p.add_argument("--start", type=Path, required=True, help="plan to start from (ours or adopted)")
    p.add_argument("--events", type=Path, default=Path("data/events/replay.json"))
    p.add_argument("--out", type=Path, default=None,
                   help="output folder (default out/continuous, or out/continuous-offline with --offline)")
    p.add_argument("--freeze-days", type=int, default=7)
    p.add_argument("--change-cost", type=float, default=25.0)
    p.add_argument("--compare", action="store_true", help="also run the exact MIP and a from-scratch solve")
    p.add_argument("--release-days", type=int, default=2,
                   help="give held extra space back when the sailing's cutoff is this close")
    p.add_argument("--offline", action="store_true",
                   help="repair with the exact MIP instead of the hosted solver (a dry run without a token)")

    p = sub.add_parser("pinning", help="one disruption, one pin choice per run: what each pin mode does")
    p.add_argument("--base", type=Path, default=OUT / "plan-adopted.json")
    p.add_argument("--out", type=Path, default=OUT / "pinning.json")
    p.add_argument("--no-optimum", action="store_true", help="skip the exact MIP column")

    p = sub.add_parser("report", help="render the manifest's visualizations for a plan as one HTML page")
    p.add_argument("plan", type=Path)
    p.add_argument("--request", type=Path, help="solver request the plan answers (default: the scenario)")
    p.add_argument("--continuous", type=Path, default=OUT / "continuous/summary.json")
    p.add_argument("--pinning", type=Path, default=OUT / "pinning.json")
    p.add_argument("--title")
    p.add_argument("-o", "--out", type=Path, default=OUT / "report.html")

    args = parser.parse_args(argv)
    handlers = {
        "publish-pack": cmd_publish_pack, "plan": cmd_plan, "optimum": cmd_optimum,
        "score": cmd_score, "adopt": cmd_adopt, "continuous": cmd_continuous,
        "pinning": cmd_pinning, "report": cmd_report,
    }
    return handlers[args.command](args)
