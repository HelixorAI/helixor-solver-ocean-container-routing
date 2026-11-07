"""Offline tests: lowering, scoring, events, pack and the three models agreeing.

The live hosted test is tests/test_live_solver.py (opt-in via SOLVER_URL).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import yaml

from ocean_routing.events import InvalidEvent, apply_events, depart
from ocean_routing.generate import generate
from ocean_routing.lowering import Pin, lower
from ocean_routing.model import Container, Sailing, Scenario
from ocean_routing.pack import FILES, build
from ocean_routing.scorer import WEIGHTS, score

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "pack/manifests/ocean_container_routing.yaml").read_text())


def tiny() -> Scenario:
    sailings = [
        Sailing("A-W0", "A", "S1", "V", "1", "X-Y", 0, cutoff_day=3, etd_day=5, eta_day=20, allocation_feu=2, spot_feu=1),
        Sailing("B-W0", "B", "S2", "V", "1", "X-Y", 0, cutoff_day=4, etd_day=6, eta_day=15, allocation_feu=1, spot_feu=2),
        Sailing("A-W1", "A", "S1", "V", "2", "X-Y", 1, cutoff_day=10, etd_day=12, eta_day=27, allocation_feu=2, spot_feu=1),
    ]
    containers = [
        Container("C1", "P1", "v", "X-Y", "40HC", ready_day=1, promise_day=18),
        Container("C2", "P1", "v", "X-Y", "40HC", ready_day=2, promise_day=22),
        Container("C3", "P2", "v", "X-Y", "20GP", ready_day=8, promise_day=25),
    ]
    return Scenario("tiny", "2026-11-09", 2, {"A": "Alpha", "B": "Beta"}, sailings, containers)


def test_scorer_counts_each_business_term():
    s = tiny()
    plan = {"C1": "B-W0", "C2": "A-W0", "C3": "A-W1"}
    sc = score(s, plan)
    assert sc.feasible
    assert (sc.on_time, sc.late_containers, sc.days_late) == (2, 1, 2)      # C3: eta 27 > promise 25
    # B-W0 carries 1/1, A-W0 1/2, A-W1 0.5/2 -> 2.5 of 5 FEU used
    assert sc.used_allocation_feu == 2.5 and sc.unused_allocation_feu == 2.5
    assert sc.spot_feu == 0
    assert sc.weighted_penalty == 1000 * 1 + 100 * 2.5 + 5 * 2


def test_scorer_flags_hard_breaches_without_trusting_any_solver():
    s = tiny()
    sc = score(s, {"C1": "A-W0", "C2": "A-W0", "C3": "A-W0"})
    assert not sc.feasible
    assert any("ready day 8 after cutoff 3" in v for v in sc.hard_violations)
    sc = score(s, {"C1": "B-W0", "C2": "B-W0"})
    assert "C3: not booked" in sc.hard_violations


def test_overflow_beyond_allocation_is_spot():
    s = tiny()
    sc = score(s, {"C1": "B-W0", "C2": "B-W0", "C3": "A-W1"})
    assert sc.spot_feu == 1.0          # B-W0: 2 FEU on a 1 FEU allocation


def test_lowering_derives_on_time_sailings_and_drops_cancelled():
    s = tiny()
    s.sailing("A-W1").status = "cancelled"
    problem = lower(s).problem
    assert [x["id"] for x in problem["sailings"]] == ["A-W0", "B-W0"]
    by_id = {c["id"]: c for c in problem["containers"]}
    assert by_id["C1"]["on_time_sailings"] == ["B-W0"]
    assert by_id["C2"]["on_time_sailings"] == ["A-W0", "B-W0"]
    assert problem["sailings"][1]["capacity_feu"] == 3


def test_lowering_reports_stale_prior_plan_entries():
    s = tiny()
    s.sailing("A-W1").status = "cancelled"
    low = lower(s, prior_plan={"C3": "A-W1", "C9": "A-W0", "C1": "B-W0"}, pins=[Pin("C1", "frozen", "B-W0")])
    assert low.problem["prior_plan"] == {"C1": "B-W0"}
    assert low.problem["pins"] == [{"entity": "C1", "mode": "frozen", "group": "B-W0"}]
    assert any("C9" in n for n in low.notes) and any("A-W1" in n for n in low.notes)


def test_pin_modes_are_the_manifests():
    assert set(MANIFEST["decision"]["pinning"]["modes"]) == {"frozen", "forbid", "prefer"}
    with pytest.raises(ValueError):
        Pin("C1", "free")


def test_events_disturb_what_they_touch():
    s = tiny()
    plan = {"C1": "B-W0", "C2": "A-W0", "C3": "A-W1"}
    world, d = apply_events(s, plan, [
        {"type": "sailing_cancelled", "sailing": "A-W0"},
        {"type": "container_rolled", "container": "C1"},
        {"type": "container_locked", "container": "C3", "sailing": "A-W1"},
    ])
    assert world.sailing("A-W0").status == "cancelled"
    assert d.disturbed == {"C1", "C2"}
    assert d.forbidden == {"C1": {"B-W0"}}
    assert d.locked == {"C3": "A-W1"}
    assert s.sailing("A-W0").status == "scheduled"     # the input world is not mutated


def test_events_refuse_unknown_records():
    s = tiny()
    with pytest.raises(InvalidEvent):
        apply_events(s, {}, [{"type": "sailing_delayed", "sailing": "NOPE", "days": 2}])
    with pytest.raises(InvalidEvent):
        apply_events(s, {}, [{"type": "teleport", "container": "C1"}])
    with pytest.raises(InvalidEvent):
        apply_events(s, {}, [{"type": "containers_added", "containers": [
            {"id": "C1", "po": "P", "vendor": "v", "lane": "X-Y", "size": "40HC", "ready_day": 1, "promise_day": 9}]}])


def test_departure_removes_sailed_containers_and_sailings():
    s = tiny()
    world, plan, log = depart(s, {"C1": "B-W0", "C2": "A-W0", "C3": "A-W1"}, today=6)
    assert {x.id for x in world.sailings} == {"B-W0", "A-W1"}      # A-W0 left on day 5
    assert {c.id for c in world.containers} == {"C1", "C3"}
    assert plan == {"C1": "B-W0", "C3": "A-W1"}


def test_weights_agree_across_manifest_and_scorer():
    rules = MANIFEST["constraints"]
    assert rules["on_time_delivery"]["weight"] == WEIGHTS["late_containers"]
    assert rules["allocation_utilization"]["weight"] == WEIGHTS["unused_allocation_feu"]
    assert rules["minimize_spot"]["weight"] == WEIGHTS["spot_feu"]
    assert rules["days_late"]["weight"] == WEIGHTS["days_late"]
    assert rules["unconfirmed_space"]["weight"] == WEIGHTS["unconfirmed_feu"]
    assert rules["new_space_request"]["weight"] == WEIGHTS["new_request_feu"]


def test_pack_is_config_only_and_reproducible(tmp_path):
    a = build(tmp_path / "a")
    b = build(tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as zf:
        assert sorted(zf.namelist()) == sorted(FILES)
        assert not [n for n in zf.namelist() if n.endswith((".py", ".js", ".so", ".jar", ".class"))]


def test_generator_is_deterministic():
    assert generate(seed=7).to_dict() == generate(seed=7).to_dict()
    assert Scenario.load(ROOT / "data/scenario.json").to_dict() == generate(seed=7).to_dict()


def test_exact_optimum_matches_independent_score():
    pytest.importorskip("ortools")
    from ocean_routing.optimum import solve_optimum

    s = Scenario.load(ROOT / "data/scenario.json")
    plan, info = solve_optimum(lower(s).problem, time_limit_s=60)
    assert info["proven_optimal"]
    assert score(s, plan).weighted_penalty == pytest.approx(info["objective"])


def test_report_kpis_are_the_manifest_rules_and_match_the_scorer():
    from ocean_routing.report import _measure, Bound, render_visualizations

    s = Scenario.load(ROOT / "data/scenario.json")
    problem = lower(s).problem
    t = tiny()
    plan = {"C1": "B-W0", "C2": "B-W0", "C3": "A-W1"}       # B-W0 1 FEU over allocation
    sc = score(t, plan)
    b = Bound(MANIFEST, lower(t).problem, plan)
    assert _measure(b, "on_time_delivery", "violated_count") == sc.late_containers
    assert _measure(b, "allocation_utilization", "fill_share") == pytest.approx(sc.utilization_pct / 100)
    assert _measure(b, "minimize_spot", "excess") == sc.spot_feu
    assert _measure(b, "days_late", "excess") == sc.days_late
    assert _measure(b, "unconfirmed_space", "excess") == sc.unconfirmed_feu
    assert _measure(b, "new_space_request", "excess") == sc.new_request_feu
    # every declared visualization renders on the full scenario too
    full_plan = {c.id: next(x.id for x in s.active_sailings() if x.lane == c.lane and c.ready_day <= x.cutoff_day)
                 for c in s.containers}
    declared = [v["id"] for v in MANIFEST["ui"]["visualizations"]]
    assert [v["id"] for v, _ in render_visualizations(MANIFEST, problem, full_plan)] == declared


def test_pin_check_catches_moved_frozen_and_forbidden_sailings():
    from ocean_routing.pinning import check_pins

    pins = [Pin("C1", "frozen", "A-W0"), Pin("C2", "forbid", "B-W0")]
    assert check_pins({"C1": "A-W0", "C2": "A-W0"}, pins) == []
    assert len(check_pins({"C1": "B-W0", "C2": "B-W0"}, pins)) == 2


# -- space beyond the allocation ----------------------------------------------

def test_space_beyond_allocation_is_priced_until_the_carrier_acknowledges_it():
    s = tiny()
    plan = {"C1": "B-W0", "C2": "B-W0", "C3": "A-W1"}       # B-W0: 2 FEU on a 1 FEU allocation
    sc = score(s, plan)
    assert (sc.spot_feu, sc.unconfirmed_feu, sc.new_request_feu) == (1.0, 1.0, 1.0)
    world, _ = apply_events(s, plan, [{"type": "space_requested", "sailing": "B-W0", "feu": 1}])
    sc = score(world, plan)
    assert (sc.unconfirmed_feu, sc.new_request_feu) == (1.0, 0.0)       # asked, not yet answered
    world, _ = apply_events(world, plan, [{"type": "space_confirmed", "sailing": "B-W0", "feu": 1}])
    sc = score(world, plan)
    assert (sc.spot_feu, sc.unconfirmed_feu, sc.new_request_feu) == (1.0, 0.0, 0.0)
    assert lower(world).problem["sailings"][1]["confirmed_feu"] == 2.0


def test_space_events_check_the_lifecycle():
    s = tiny()
    with pytest.raises(InvalidEvent):          # B-W0 has 2 FEU on sale beyond its allocation
        apply_events(s, {}, [{"type": "space_requested", "sailing": "B-W0", "feu": 3}])
    with pytest.raises(InvalidEvent):          # nothing pending to confirm
        apply_events(s, {}, [{"type": "space_confirmed", "sailing": "B-W0", "feu": 1}])
    with pytest.raises(InvalidEvent):          # nothing held to release
        apply_events(s, {}, [{"type": "space_released", "sailing": "B-W0", "feu": 1}])
    world, _ = apply_events(s, {}, [{"type": "space_requested", "sailing": "B-W0", "feu": 2},
                                    {"type": "space_confirmed", "sailing": "B-W0", "feu": 0.5},
                                    {"type": "space_rejected", "sailing": "B-W0", "feu": 1.5}])
    b = world.sailing("B-W0")
    assert (b.confirmed_feu, b.pending_feu, b.ceiling_feu) == (1.5, 0.0, 1.5)   # capped at what was accepted


def test_carrier_simulator_answers_from_hidden_limits_only():
    from ocean_routing.space import CarrierSimulator

    s = tiny()
    s.simulation = {"carrier_accepts_extra_feu": {"B-W0": 0.5}}
    world, _ = apply_events(s, {}, [{"type": "space_requested", "sailing": "B-W0", "feu": 2}])
    assert CarrierSimulator(world).respond(world) == [
        {"type": "space_confirmed", "sailing": "B-W0", "feu": 0.5},
        {"type": "space_rejected", "sailing": "B-W0", "feu": 1.5},
    ]
    assert "simulation" not in json.dumps(lower(world).problem)
    assert "accepts" not in json.dumps(lower(world).problem)


def test_freed_space_is_held_until_close_to_cutoff():
    from ocean_routing.space import carrier_actions

    s = tiny()
    s.sailing("A-W1").confirmed_feu = 3.0             # 1 FEU beyond the allocation acknowledged
    held = carrier_actions(s, {"C1": "B-W0", "C2": "A-W0", "C3": "A-W1"}, release_days=2)
    assert held.events == [] and held.held == {"A-W1": 1.0}
    s.today = 9                                       # A-W1 cutoff is day 10
    released = carrier_actions(s, {"C1": "B-W0", "C2": "A-W0", "C3": "A-W1"}, release_days=2)
    assert released.events == [{"type": "space_released", "sailing": "A-W1", "feu": 1.0}]
    asks = carrier_actions(s, {"C1": "B-W0", "C2": "B-W0", "C3": "A-W1"})
    assert {"type": "space_requested", "sailing": "B-W0", "feu": 1.0} in asks.events
