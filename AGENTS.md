# AGENTS.md

Orientation for AI coding agents working in this repository. Humans should
start with the [README](README.md).

## What this repository is

A public, self-contained example: booking ocean containers onto carrier
sailings with the **hosted Helixor Solver**, called over its REST API.

- It contains **no solver**. The model is a config-only solver pack in
  `pack/`; the Python package `ocean_routing/` is a client that lowers business
  records into requests, applies events, and scores results independently.
- There are no submodules and no private dependencies. Python needs `httpx`
  and `pyyaml`; `ortools` (exact MIP) is an optional yardstick.
- Hosted solves need `SOLVER_URL` and `SOLVER_TOKEN` for a Helixor account
  whose organization holds a Helixor Solver license. Everything else runs
  offline.

## Map

| Path | Role |
|---|---|
| `pack/pack.yaml`, `pack/manifests/ocean_container_routing.yaml` | the model: decision, 8 rules with weights, pinning, search budget, UX surfaces, `ui.visualizations`; `pack.yaml` `surfaces` ships `pack/ui` |
| `pack/datasets/two_lane_week.demo.json` | demo request the pack ships (`ocean_routing/demo.py`, `scripts/build_demo_dataset.py`): one peak week, every promise keepable only with spot space; must carry every field the rules name |
| `ocean_routing/model.py` | `Container`, `Sailing`, `Scenario`, plan file IO |
| `ocean_routing/lowering.py` | records to request: `on_time_sailings`, `capacity_feu`, `confirmed_feu`, `committed_feu`, prior plan, pins |
| `ocean_routing/client.py` | REST client: publish pack, read manifest, solve |
| `ocean_routing/planner.py` | initial plan, adopt (certify with everything frozen), replan pin policy, `--offline` MIP path |
| `ocean_routing/events.py` | event types, validation, `apply_events`, `depart` |
| `ocean_routing/space.py` | carrier actions (request, hold, withdraw, release) and the carrier simulator |
| `ocean_routing/scorer.py` | independent scorer and `WEIGHTS` |
| `ocean_routing/optimum.py` | exact MIP of the same model (OR-Tools) |
| `ocean_routing/pinning.py` | pinning scenarios: one disruption, one pin choice per run |
| `ocean_routing/report.py` | renders the manifest's `ui.visualizations` as one HTML page |
| `ui/src/surface.js`, `ui/src/surface.css` | the pack's Solver UX surfaces: the same views, in the browser; CSS shared with the report |
| `ocean_routing/surfaces.py`, `pack/ui/*.html` | builds `pack/ui/ocean.{problem,solution}.html` from `ui/src` (`python scripts/build_surfaces.py`); committed, and the pack build refuses stale ones |
| `ocean_routing/cli.py` | `python -m ocean_routing <command>` |
| `data/scenario.json` | generated scenario (`python -m ocean_routing.generate`, seed 7, deterministic) |
| `data/events/replay.json` | five event cycles for `continuous` |
| `docs/guide.html`, `docs/TUTORIAL.md`, `README.md` | illustrated guide, step-by-step tutorial, command reference and measured results |
| `out/`, `dist/` | generated, gitignored; never treat them as source or as current |

## Commands

```bash
pip install -r requirements.txt
pytest -q                                              # offline tests, about 30 s
SOLVER_LIVE=1 pytest -q tests/test_live_solver.py      # hosted; publishes the pack to the account
python -m ocean_routing --help
python -m ocean_routing optimum                        # offline, needs ortools
python -m ocean_routing continuous --start out/plan-optimum.json --offline
```

`--scenario` is a top-level option and goes before the command. The README's
"Start here" table lists every command, what it needs and what it writes.

## Invariants: keep these true

1. **Weights agree.** The manifest rule weights and `scorer.WEIGHTS`;
   `tests/test_model.py::test_weights_agree_across_manifest_and_scorer`
   checks them.
2. **A new rule lands in every engine.** Manifest (and its `data_schema`
   columns), lowering (any derived field), `scorer.py`, `optimum.py`, the
   demo dataset, and a test. The scorer must stay independent:
   it reads records, never solver output or the certificate.
3. **Pack versions are immutable.** Any change under `pack/` needs a new
   `version` in both `pack/pack.yaml` and the manifest. Re-publishing a version
   with different content is refused: `409 solver_pack_version_conflict` from the current server, `422 solver_pack_rejected` from earlier releases.
4. **The solver is generic.** Express behaviour as manifest rules over fields
   the request carries. Never add ocean-specific logic to the request shape or
   expect it from the solver.
5. **Fail closed.** A plan is accepted only when the certificate is feasible,
   every container is placed, and the independent scorer agrees. Unknown
   events, ids or event types are refused, not skipped. Never add a silent
   fallback.
6. **Label every non-hosted result.** `--offline` repairs are the exact MIP,
   written to `out/continuous-offline/` as `cycle-N-offline.json`; they never
   stand in for hosted results.
7. **The carrier simulator is hidden from planning.** `Scenario.simulation`
   is read only by `CarrierSimulator`; lowering never sends it, and a test
   checks that.
8. **Space acknowledgement.** Near-cutoff freezes apply only within
   `confirmed_feu`. A rejection caps the sailing at what the carrier accepted.

9. **Surfaces agree with the report.** `ui/src/surface.js` implements the same
   contract measures as `report.py`; `tests/test_surfaces.py` runs it under
   Node and checks its KPIs against the report. Rebuild `pack/ui` after any
   change to `ui/src` and bump the pack version.

## Documentation rules

- Every number in the docs comes from a real run of the current code, scored
  by the independent scorer. After a change that moves results, rerun, then
  update `README.md` (Results), `docs/TUTORIAL.md` and `docs/guide.html`
  together.
- The repository is public. Do not describe how the hosted solver works
  internally, and do not name third-party companies or products in prose.
- Everything in the data is invented except the UN/LOCODE port codes.
