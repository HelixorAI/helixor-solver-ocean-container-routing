"""The pack's Solver UX surfaces: built from source, shipped in the pack, and
reporting the same KPIs as the report page and the independent scorer.

The parity test runs ui/src/surface.js under Node with a minimal stand-in for
the browser host, sends it the render message the Solver UX sends, and reads
the KPI tiles back. It is skipped when Node is not installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from ocean_routing.lowering import lower
from ocean_routing.model import Scenario
from ocean_routing.pack import FILES, build
from ocean_routing.report import Bound, _measure
from ocean_routing.surfaces import CSP, ROLES, built, stale

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "pack/manifests/ocean_container_routing.yaml").read_text())
DESCRIPTOR = yaml.safe_load((ROOT / "pack/pack.yaml").read_text())

HARNESS = r"""
const fs = require("fs");
const [role, payloadPath] = process.argv.slice(2);
const page = fs.readFileSync(0, "utf8");
const script = page.slice(page.indexOf("<script>") + 8, page.lastIndexOf("</script>"));
const nodes = {};
const node = (id) => (nodes[id] ||= { id, innerHTML: "", style: {}, dataset: {},
  getBoundingClientRect: () => ({ bottom: 100, height: 100 }) });
const posted = [];
let onMessage = null;
const parent = { postMessage: (m) => posted.push(m) };
const window = { addEventListener: (t, f) => { if (t === "message") onMessage = f; }, scrollY: 0 };
const document = { getElementById: node, addEventListener: () => {}, documentElement: { dataset: {} } };
class ResizeObserver { observe() {} }
new Function("parent", "window", "document", "ResizeObserver", "innerWidth", script)(
  parent, window, document, ResizeObserver, 1024);
const payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
onMessage({ source: parent, data: { schema: "helixor.solver-surface.v1", type: "render", role, ...payload } });
process.stdout.write(JSON.stringify({ html: node("app").innerHTML, posted }));
"""


def _run_surface(tmp_path: Path, role: str, payload: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    (tmp_path / "payload.json").write_text(json.dumps(payload))
    (tmp_path / "harness.js").write_text(HARNESS)
    page = built()[f"ui/{ROLES[role]}"]
    out = subprocess.run([node, str(tmp_path / "harness.js"), role, str(tmp_path / "payload.json")],
                         input=page, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_built_surfaces_are_committed_and_current():
    assert stale() == [], "run python scripts/build_surfaces.py"


def test_surfaces_ship_in_the_pack_and_are_declared(tmp_path):
    declared = {s["role"]: s["uri"] for s in DESCRIPTOR["surfaces"]}
    assert declared == {role: f"ui/{name}" for role, name in ROLES.items()}
    assert {s["problem_type"] for s in DESCRIPTOR["surfaces"]} == {MANIFEST["problem_type"]}
    assert DESCRIPTOR["version"] == MANIFEST["version"]
    names = zipfile.ZipFile(build(tmp_path)).namelist()
    assert set(declared.values()) <= set(names) and set(FILES) == set(names)


def test_surfaces_are_self_contained():
    for page in built().values():
        assert CSP in page
        # no external resource of any kind: everything is inline
        assert not re.search(r"""(src|href)\s*=\s*['"]?(https?:)?//""", page)
        assert "fetch(" not in page and "XMLHttpRequest" not in page


def _plans(s: Scenario) -> dict:
    first = {c.id: next(x.id for x in s.active_sailings() if x.lane == c.lane and c.ready_day <= x.cutoff_day)
             for c in s.containers}
    # every container of a lane on that lane's first sailing: far over the
    # allocation, onto spot and unacknowledged space, with empty sailings left
    crowded = {c.id: next(x.id for x in s.active_sailings() if x.lane == c.lane) for c in s.containers}
    partial = dict(sorted(first.items())[::2])          # half the containers unplaced
    return {"first eligible": first, "crowded": crowded, "partial": partial}


@pytest.mark.parametrize("case", ["first eligible", "crowded", "partial"])
def test_solution_surface_kpis_match_the_report(tmp_path, case):
    s = Scenario.load(ROOT / "data/scenario.json")
    problem = lower(s).problem
    plan = _plans(s)[case]
    solution = {"solution": {"placements": [{"entity": e, "group": g} for e, g in plan.items()]}}
    out = _run_surface(tmp_path, "solution", {"manifest": MANIFEST, "problem": problem, "solution": solution})
    assert [m["type"] for m in out["posted"]] == ["ready", "resize"], out["posted"]
    html = out["html"]
    assert [v["id"] for v in MANIFEST["ui"]["visualizations"]] == re.findall(r"data-viz='([^']+)'", html)
    tiles = re.findall(r"<div class='label'>([^<]+)</div><div class='value'>(?:<span[^>]*>!</span>)?([^<]+)</div>", html)
    b = Bound(MANIFEST, problem, plan)
    metrics = next(v for v in MANIFEST["ui"]["visualizations"] if v["type"] == "kpi")["metrics"]
    assert len(tiles) == len(metrics)
    for (label, shown), m in zip(tiles, metrics):
        assert label == m["label"]
        expected = _measure(b, m["rule"], m["measure"])
        number = float(shown.split()[0].rstrip("%").replace(",", ""))
        if m.get("format") == "percent":
            assert number == pytest.approx(expected * 100, abs=0.05)
        else:
            assert number == pytest.approx(expected, abs=0.05)


def test_problem_surface_renders_without_a_plan(tmp_path):
    problem = json.loads((ROOT / "pack/datasets/two_lane_week.demo.json").read_text())
    out = _run_surface(tmp_path, "problem", {"manifest": MANIFEST, "problem": problem, "solution": None})
    assert [m["type"] for m in out["posted"]] == ["ready", "resize"], out["posted"]
    shown = re.findall(r"data-viz='([^']+)'", out["html"])
    assert shown == [v["id"] for v in MANIFEST["ui"]["visualizations"] if v["type"] != "table"]
    assert f"<div class='label'>Containers</div><div class='value'>{len(problem['containers'])}</div>" in out["html"]


def test_solution_surface_refuses_a_result_without_placements(tmp_path):
    problem = json.loads((ROOT / "pack/datasets/two_lane_week.demo.json").read_text())
    out = _run_surface(tmp_path, "solution", {"manifest": MANIFEST, "problem": problem, "solution": {"status": "failed"}})
    assert {"type": "error", "message": "solution carries no placements"}.items() <= out["posted"][1].items()


# -- the demo request the pack ships ------------------------------------------

def test_demo_dataset_is_reproducible_from_its_builder():
    from ocean_routing.demo import demo_request

    committed = json.loads((ROOT / "pack/datasets/two_lane_week.demo.json").read_text())
    assert committed == demo_request(), "run python scripts/build_demo_dataset.py"


def test_demo_keeps_every_promise_only_by_buying_spot_space():
    pytest.importorskip("ortools")
    from ocean_routing.demo import demo_scenario
    from ocean_routing.optimum import solve_optimum
    from ocean_routing.scorer import score

    s = demo_scenario()
    plan, info = solve_optimum(lower(s).problem, time_limit_s=60)
    sc = score(s, plan)
    assert info["proven_optimal"] and sc.late_containers == 0 and sc.spot_feu > 0
    for sailing in s.sailings:
        sailing.spot_feu = 0.0
    with pytest.raises(SystemExit):        # the allocation alone cannot carry the week
        solve_optimum(lower(s).problem, time_limit_s=60)


@pytest.mark.parametrize("break_it, message", [
    (lambda p, sol: p["containers"][1].pop("feu"), "'feu' is not a number"),
    (lambda p, sol: p["sailings"][0].pop("allocation_feu"), "'allocation_feu' is not a number"),
    (lambda p, sol: sol["placements"].append({"entity": p["containers"][0]["id"], "group": "NO-SUCH-SAILING"}),
     "placement names unknown sailings NO-SUCH-SAILING"),
])
def test_solution_surface_fails_closed_on_records_it_cannot_bind(tmp_path, break_it, message):
    problem = json.loads((ROOT / "pack/datasets/two_lane_week.demo.json").read_text())
    solution = {"placements": [{"entity": c["id"], "group": c["on_time_sailings"][0]} for c in problem["containers"][1:]]}
    break_it(problem, solution)
    out = _run_surface(tmp_path, "solution", {"manifest": MANIFEST, "problem": problem, "solution": solution})
    assert out["posted"][1]["type"] == "error" and message in out["posted"][1]["message"], out["posted"]
