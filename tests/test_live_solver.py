"""Live test against a hosted (or locally run) Helixor Solver. Opt-in:

    SOLVER_URL=... SOLVER_TOKEN=... pytest tests/test_live_solver.py

It publishes the pack, solves the bundled demo dataset and the full scenario,
and checks the solver's certificate against the independent scorer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ocean_routing import planner
from ocean_routing.client import SolverClient, SolverError
from ocean_routing.lowering import Pin
from ocean_routing.model import Scenario
from ocean_routing.pack import build
from ocean_routing.scorer import score

ROOT = Path(__file__).resolve().parent.parent

# Opt-in: these tests publish the pack and run hosted solves on your account.
pytestmark = pytest.mark.skipif(
    not (os.environ.get("SOLVER_LIVE") == "1" and os.environ.get("SOLVER_URL") and os.environ.get("SOLVER_TOKEN")),
    reason="set SOLVER_LIVE=1, SOLVER_URL and SOLVER_TOKEN to run against a hosted solver",
)


@pytest.fixture(scope="module")
def client():
    with SolverClient.from_env() as c:
        c.publish_pack(build(ROOT / "dist"))
        yield c


def _certificate_penalty(result: planner.PlanResult) -> float:
    return sum(v["weighted_penalty"] for v in result.certificate.get("violations") or [] if not v["is_hard"])


def test_demo_dataset_solves(client):
    problem = json.loads((ROOT / "pack/datasets/two_lane_week.demo.json").read_text())
    response = client.solve(problem)
    assert response["solution"]["is_feasible"] is True
    assert response["solution"]["entities_placed"] == len(problem["containers"])


def test_initial_plan_certificate_matches_independent_score(client):
    s = Scenario.load(ROOT / "data/scenario.json")
    result = planner.initial_plan(client, s)
    assert result.score.feasible
    assert _certificate_penalty(result) == pytest.approx(result.score.weighted_penalty)


def test_frozen_pins_hold_and_forbid_is_respected(client):
    s = Scenario.load(ROOT / "data/scenario.json")
    base = planner.initial_plan(client, s).plan
    frozen = sorted(base)[:20]
    victim = sorted(base)[25]
    pins = [Pin(c, "frozen", base[c]) for c in frozen] + [Pin(victim, "forbid", base[victim])]
    result = planner.solve(client, s, prior_plan=base, pins=pins, pin_default="prefer", change_cost=25.0)
    assert all(result.plan[c] == base[c] for c in frozen)
    assert result.plan[victim] != base[victim]


def test_infeasible_pins_fail_closed(client):
    s = Scenario.load(ROOT / "data/scenario.json")
    def eligible(sailing):
        return [c for c in s.containers if c.lane == sailing.lane and c.ready_day <= sailing.cutoff_day]

    sailing = next(x for x in s.active_sailings()
                   if sum(c.feu for c in eligible(x)) > x.allocation_feu + x.spot_feu + 1)
    lane = eligible(sailing)
    too_many, feu = [], 0.0
    for c in lane:                       # FEU, not a count: a 20' box is 0.5
        too_many.append(c)
        feu += c.feu
        if feu > sailing.allocation_feu + sailing.spot_feu + 1:
            break
    assert feu > sailing.allocation_feu + sailing.spot_feu, "scenario has too little cargo for this test"
    pins = [Pin(c.id, "frozen", sailing.id) for c in too_many]
    with pytest.raises(SolverError) as refused:
        planner.solve(client, s, pins=pins)
    assert refused.value.status == 422
    assert "pinned_set_infeasible" in json.dumps(refused.value.body)
