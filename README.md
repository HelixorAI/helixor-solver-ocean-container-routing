# Ocean container routing on the hosted Helixor Solver

This example plans a customer's ocean containers onto carrier sailings. Each
carrier allocates the customer a weekly amount of space (FEU) on its sailings,
and spot space can be bought beyond that allocation. The plan has three goals,
ranked:

1. **Deliver on time.** Get as many containers as possible to the destination
   port by their promise date.
2. **Use the allocation.** Fill 100% of the space each carrier allocated each
   week.
3. **Buy little spot space.** Book as few containers as possible beyond the
   allocation.

When demand is above the allocation, the plan has to go beyond it: it can
over-allocate (ask a carrier to take more than the allocation) or book spot
space on the carrier's website. Either way, the space is only the customer's
once the carrier **acknowledges** it, and the carrier may reject the request.
Space that frees up is **held**, not handed straight back, because a later
change may need it. The example models all of this; see
[Space beyond the allocation](#space-beyond-the-allocation).

After the first plan, the example shows how to keep the plan current as events
arrive:

- **Carrier events:** blank sailings, delays, rolled containers, allocation
  cuts or increases, spot space selling out, and the carrier's answers to
  space requests (confirmed or rejected).
- **Vendor events:** containers added or cancelled, expedites, cargo-ready
  slips, equipment changes, and a peak-season surge that needs space well
  beyond the allocation.
- **Planner locks.**

The example covers three operations:

- **Repair:** fix the current plan after an event.
- **Pinning:** freeze some bookings or forbid some sailings.
- **Continuous planning:** roll the planning window forward cycle by cycle.

It also shows how to take over a plan that another planning system produced and
plan continuously from it.

## Start here

| If you want to | Read |
|---|---|
| understand the business problem and see each scenario as a diagram | [`docs/guide.html`](docs/guide.html) (download and open it in a browser), or [`docs/guide.pdf`](docs/guide.pdf) to read or share |
| follow the demo step by step, with every rule and file explained | [`docs/TUTORIAL.md`](docs/TUTORIAL.md) |
| run the commands and see the measured results | this README |
| change the code with an AI agent | [`AGENTS.md`](AGENTS.md) |

### Terms

| Term | Meaning |
|---|---|
| FEU | forty-foot equivalent unit: a 40' container (`40GP` standard, `40HC` high cube) is 1 FEU, a 20' container (`20GP`) is 0.5 |
| lane | an origin-destination port pair (POL-POD, port of loading to port of discharge), for example `CNSHA-USLAX` |
| sailing | one weekly voyage of one carrier service, for example `ATLS-PSX-W03`: carrier ATLS, service PSX, week 3 |
| cutoff, ETD, ETA | last day the yard takes cargo for a sailing; its departure day; its arrival day. All are day numbers from the start of the window (day 0 = Monday 2026-11-09) |
| allocation | weekly space a carrier contracted to the customer on a sailing |
| spot | space beyond the allocation, bought at market rates, up to what the carrier has on sale |
| blank sailing | a sailing the carrier cancels (event `sailing_cancelled`) |
| rolled container | a container the carrier refuses to load on its booked sailing (event `container_rolled`) |
| solver pack | a versioned zip of configuration (manifest, rules, weights, demo dataset) you publish to the solver; [`pack/`](pack/) |
| lowering | turning your business records into the solver request; [`ocean_routing/lowering.py`](ocean_routing/lowering.py) |
| certificate | the solver's account of a returned plan: feasible or not, and the penalty of each rule |
| repair | a new plan after events, starting from the previous plan and moving as little as possible |
| pin | a per-container instruction in a repair: `frozen` to a sailing, `forbid` a sailing, `prefer` the prior sailing, or none (free) |
| change cost | the price of moving a `prefer` container, in the same units as the rule weights |

### What needs a license

Hosted solves need a Helixor account whose organization holds a **Helixor
Solver license**. Everything else runs locally. Commands, in the order to run
them:

| # | Command | Needs | Writes | About |
|---|---|---|---|---|
| 1 | `pytest -q` | Python | | 30 s |
| 2 | `python -m ocean_routing optimum` | `ortools` | `out/plan-optimum.json` | 1 s |
| 3 | `python -m ocean_routing score out/plan-optimum.json` | | | 1 s |
| 4 | `python -m ocean_routing continuous --start out/plan-optimum.json --offline` | `ortools` | `out/continuous-offline/` | 5 s |
| 5 | `python -m ocean_routing report out/plan-optimum.json -o out/report.html` | | `out/report.html` | 1 s |
| 6 | `python -m ocean_routing publish-pack` | **license** | `dist/*.zip`, the pack on your account | 5 s |
| 7 | `python -m ocean_routing plan` | **license** | `out/plan-helixor.json` | 1 min |
| 8 | `python -m ocean_routing adopt out/plan-optimum.json` | **license**, step 2 | `out/plan-adopted.json` | 5 s |
| 9 | `python -m ocean_routing continuous --start out/plan-adopted.json --compare` | **license**, step 8 | `out/continuous/` | 10 min |
| 10 | `python -m ocean_routing pinning` | **license**, step 8 | `out/pinning.json` | 7 min |

Steps 1 to 5 need no license and no network. Step
4 is a dry run: the exact MIP does the repairs, its output says so, and it is
written to its own folder so it never stands in for hosted results.

`--scenario <file>` is a top-level option and goes before the command:
`python -m ocean_routing --scenario my.json plan`. Every command has `--help`.

Everything runs through the hosted solver's documented REST API. The model is a
config-only **solver pack**: a manifest (rules, pinning, search budget, UX
surface and declared visualizations) and a demo dataset, with no code. Python is used only to lower business records into the request, apply
events, and score results independently.

An exact MIP ([`ocean_routing/optimum.py`](ocean_routing/optimum.py)) reads
the exact JSON request the hosted solver receives and gives the proven optimum
as a yardstick.

## The model

| Solver concept | Domain | Notes |
|---|---|---|
| entity | container | `lane` (POL-POD), `feu` (40' = 1, 20' = 0.5), `ready_day`, `promise_day` |
| group | sailing | `lane`, `cutoff_day`, `eta_day`, `allocation_feu`, `capacity_feu` = allocation + spot on sale, `confirmed_feu` = space the carrier acknowledged, `committed_feu` = confirmed + requested |
| eligibility | same lane; cargo ready by cutoff | These rules restrict where a container can go. The search never breaks them. |
| hard | FEU on a sailing ≤ allocation + spot on sale | |
| soft, weight 1000 | container arrives after its promise date | `assigned_in_entity_set` against `on_time_sailings`, which is derived from the schedule |
| soft, weight 100 | FEU of allocation left unused | `aggregate sum >= allocation_feu`, judged on empty sailings too |
| soft, weight 40 | FEU booked beyond the allocation (spot) | `aggregate sum <= allocation_feu` |
| soft, weight 30 | FEU booked beyond the space the carrier acknowledged | `aggregate sum <= confirmed_feu`: the carrier may still reject it |
| soft, weight 10 | FEU booked beyond confirmed + requested space | `aggregate sum <= committed_feu`: a new request has to go to the carrier |
| soft, weight 5 | days late | `group_fits eta_day <= promise_day`; breaks ties between late options |

The weights make the ranking explicit:

- No amount of utilisation is worth one late container.
- Moving 1 FEU from spot onto unused allocation saves 140 (100 for the unused
  allocation plus 40 for the spot space), so it always pays.
- An FEU beyond the allocation costs 40 on space the carrier has acknowledged,
  70 on space requested but not yet answered, and 80 on space still to
  request. The plan fills held space first and asks for new space last. It
  still over-allocates whenever that saves a late container (1,000).

## Space beyond the allocation

Each sailing tracks the customer's space:

| State | Meaning |
|---|---|
| allocation | the contract; acknowledged by definition |
| confirmed | acknowledged space: the allocation plus extra the carrier accepted |
| pending | requested (over-allocation, or a spot booking on the carrier's website) and not yet answered |
| space on sale | the most the customer can ask for on the sailing |

After every plan, [`ocean_routing/space.py`](ocean_routing/space.py) works out
what to tell each carrier. Each decision is an event (`space_requested`,
`space_withdrawn`, `space_released`), applied through the same event path as
every other change:

- **Request** the gap when the plan needs more than is confirmed plus pending.
- **Hold** confirmed extra space the plan no longer needs, so a later surge,
  roll or blank sailing can use it.
- **Give back** held extra space, or withdraw a pending request, only once
  the sailing's cutoff is within `--release-days` (default 2).

The carriers answer at the start of the next cycle with `space_confirmed` or
`space_rejected`. In the replay, a **carrier simulator** gives those answers.
It works from acceptance limits in the scenario's `simulation` block, which
the planner and the solver request never see. Space a carrier rejects caps
that sailing at what it accepted, and the next repair moves whatever no longer
fits.

The pin policy follows the same logic. A booking near cutoff is frozen as
"with the carrier" only within acknowledged space. A booking on pending space
is not booked yet, so it stays open to the repair.

The manifest is [`pack/manifests/ocean_container_routing.yaml`](pack/manifests/ocean_container_routing.yaml).
It runs on the shared manifest assignment engine (`policy_id: booking_consolidation`)
and declares `decision.pinning` (frozen, forbid and prefer) so the same problem
type accepts a prior plan and pins.

**One modelling choice matters a lot.** Spot space is *overflow on the same
sailing*, not a separate "spot slot" that a container is assigned to. A model
with separate allocation and spot slots has twice the groups and many
equivalent moves (the same container on the same vessel, booked under a
different contract). On this instance, before the acknowledgement rules were
added, the separate-slot model's best hosted result was 3.5% above the
optimum, and the overflow model's 0.9%. It is
also how carriers bill spot space.

## Run it

You need a Helixor account with a Helixor Solver license and Python 3.10+.

1. **Sign up** at [helixor.dev](https://helixor.dev). Your account belongs to
   an organization.
2. **Get a Helixor Solver license** for your organization from your account.
   Hosted solves run only for an organization that holds one.
3. **Get the Solver API origin and a token** for your account, and put them in
   `.env` as `SOLVER_URL` and `SOLVER_TOKEN`. Python does not read `.env`
   itself: load it in each new shell with `set -a && . ./.env && set +a`.

This repository contains no solver: it is a client, a model and a dataset.
Without a license you can still build the exact MIP's plan
(`optimum`) and replay from it offline (`continuous --start
out/plan-optimum.json --offline`), with the exact MIP doing the repairs,
then score and report. Only the hosted solves need the license.

```bash
git clone https://github.com/HelixorAI/helixor-solver-ocean-container-routing.git
cd helixor-solver-ocean-container-routing
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # fill in SOLVER_URL and SOLVER_TOKEN
set -a && . ./.env && set +a
```

### 1. Publish the solver pack

This command builds `dist/ocean_container_routing-2.4.zip`, validates it,
uploads it and activates it. It then reads the manifest back.

Pack versions are immutable. Publishing the same version again with different
content is refused with `422 solver_pack_rejected`. If you change the pack,
bump `version` in both `pack/pack.yaml` and the manifest.

```bash
python -m ocean_routing publish-pack
```

### 2. Plan the window

[`data/scenario.json`](data/scenario.json) is the generated scenario. To
regenerate it, run `python -m ocean_routing.generate` (seed 7). It contains:

- 232 containers (213 FEU)
- 3 lanes
- 4 carriers
- 64 sailings: 8 services, one a week, weeks 0 to 7
- 224 FEU allocated, on the sailings of weeks 0 to 6

`python -m ocean_routing.generate` overwrites `data/scenario.json` unless you
pass `--out`. With seed 7 it reproduces the committed file byte for byte.

```bash
python -m ocean_routing plan                    # hosted solver -> out/plan-helixor.json
python -m ocean_routing score out/plan-helixor.json
```

The plan is accepted only when two checks pass:

- The solver's certificate says the plan is feasible.
- The independent scorer ([`ocean_routing/scorer.py`](ocean_routing/scorer.py))
  agrees.

An HTTP 200 alone is not accepted. If either check fails, the command exits
with the typed reasons.

#### Reading the output

Every plan is summarised on one line by the independent scorer:

```text
FEASIBLE  on-time 193/232 (83.2%)  late=39 (302 d)  allocation 206.5/224 FEU (92.2%)  spot=6.5 FEU  unconfirmed=6.5  new-request=6.5  penalty=42,780
```

| Field | Meaning |
|---|---|
| `FEASIBLE` / `INFEASIBLE` | whether every hard rule holds |
| `on-time` | containers arriving by their promise date |
| `late=39 (302 d)` | late containers, and their total days late |
| `allocation` | FEU loaded within the allocation, out of the allocation |
| `spot` | FEU beyond the allocation |
| `unconfirmed` | FEU beyond the space the carrier has acknowledged ("Unacknowledged FEU" in the tables) |
| `new-request` | FEU beyond acknowledged plus requested space: what still has to be requested |
| `penalty` | the weighted sum of the soft rules, without change cost |

Exit codes: 0 success; 1 the plan is infeasible (`score`) or a local engine
failed; 2 the solver refused the request (the typed reason is printed), or the options conflict; 3 no
admissible plan (the certificate says infeasible, a container is unplaced, or
the independent scorer rejects the plan); 4 the solver moved a frozen booking.

### 3. Take over a plan made by another planning system

This step takes the exact MIP's plan as the "existing" plan, as if it came
from the system you are migrating from. Bring your own plan instead, either as
`{"bookings": [{"container", "sailing"}]}` or as
`{"placements": [{"entity", "group"}]}`.

```bash
python -m ocean_routing optimum                 # -> out/plan-optimum.json
python -m ocean_routing adopt out/plan-optimum.json     # -> out/plan-adopted.json
```

`adopt` runs three checks before it accepts the plan:

1. **Id check.** Every container and sailing id in the plan must exist in the
   scenario. A missing booking, an unknown container or a cancelled sailing
   stops it with a list of the offending ids.
2. **Independent score.** The scorer judges the plan.
3. **Hosted certificate.** The plan is sent to the hosted solver with every
   booking **frozen**. The solver scores it under the pack's rules and returns
   its certificate.

If a plan breaks a rule that can be judged on the frozen bookings alone (lane,
cutoff or capacity), the solver refuses it with `422 pinned_set_infeasible`.
The refusal names the rule and the containers. The solver does not repair the
plan silently.

### 4. Plan continuously

```bash
python -m ocean_routing continuous --start out/plan-adopted.json --compare
```

This replays [`data/events/replay.json`](data/events/replay.json), five cycles
over the first three weeks. Cycle 4 is a peak-season surge: a vendor adds 10
FEU for Shanghai to Los Angeles on top of a 14 FEU weekly allocation, so the
plan needs space well beyond the allocation. Before cycle 1, the start plan's
space beyond the allocation is requested from the carriers. Each cycle then:

1. **Advances the clock.** Sailings that have departed leave the problem,
   together with their containers. Their utilisation is final.
2. **Takes the carriers' answers** to the space requested after the last plan:
   confirmed or rejected.
3. **Applies the events.** See [`ocean_routing/events.py`](ocean_routing/events.py).
   An event that names an unknown sailing or container is refused, not skipped.
4. **Scores the old plan against the changed world.** The old plan is often
   broken at this point.
5. **Repairs the plan with one hosted request**, built from the pin policy
   ([`ocean_routing/planner.py`](ocean_routing/planner.py)):

| Container state | Pin sent | Why |
|---|---|---|
| Locked by a planner | `frozen` to the chosen sailing | An explicit decision |
| On a sailing whose cutoff is ≤ 7 days away, still valid, and within acknowledged space | `frozen` | Booked with the carrier; moving it means re-booking |
| Touched by an event (blank sailing, delay, roll, expedite, ready slip, size change, new) | none (`pin_default: free`) | Must be re-planned |
| Rolled off a sailing | `forbid` that sailing | The carrier will not take it |
| Everything else | `prefer`, `change_cost: 25` | Stays put unless moving it gains more than 25. Saving one late container (1,000) or 1 FEU of unused allocation (100) is worth a move; shaving four days of lateness off a container that is late anyway is not |

6. **Tells the carriers** what the repaired plan needs: requests, holds and
   releases.

`--offline` runs the same replay with the exact MIP doing the repairs instead
of the hosted solver. It is a dry run that needs no token. Its output says so,
it is written to `out/continuous-offline/`, and it cannot be combined with
`--compare`.

With `--compare`, each cycle also sends **the same request** to:

- **The exact MIP**, with the same pins and change cost.
- **The hosted solver from scratch**, with no prior plan. This shows how much a
  re-plan from scratch would move.

The comparison is written to `out/continuous/summary.json`.

### 5. See what each pin mode does

```bash
python -m ocean_routing pinning               # -> out/pinning.json
```

This holds everything fixed except the pins. It uses the same base plan (the
adopted one) and the same disruption: the carrier blanks `BORS-TP1-W01`. Each
run makes one pin choice:

- no prior plan;
- `prefer` with change costs of 0, 25 and 1000;
- week-0 bookings frozen;
- one sailing forbidden;
- an over-full freeze that must be refused.

Each run is scored independently, and each pin is checked against the
returned plan. The results and what they show are in the
[tutorial, section 8](docs/TUTORIAL.md#8-pins-deciding-what-may-change). In
short:

- **Re-planning from scratch** moved 92 containers and had the highest penalty.
- **`prefer` with change cost 25** moved 27.
- **`prefer` with change cost 1000** moved only the 5 displaced containers and
  reached the optimum.
- **The over-full freeze** was refused with `422 pinned_set_infeasible`, naming
  the rule and the sailing.

### 6. Chart a plan

```bash
python -m ocean_routing report out/plan-adopted.json -o out/report.html
```

This writes one self-contained HTML page with:

- KPI tiles;
- load against allocation and spot for each sailing;
- allocation used by week and carrier;
- the sailing schedule;
- the late containers.

When the continuous and pinning results exist, it adds them too. The charts
are the manifest's declared `ui.visualizations`. Each KPI names a rule and is
evaluated from that rule's contract, and a test checks every KPI against the
independent scorer. Run it and open the page in a browser.

The pack ships the same views as two Solver UX surfaces (`pack/ui`, built from
`ui/src` with `python scripts/build_surfaces.py`): the `problem` surface under
Data Input > Visualize, and the `solution` surface in the solution panel. Each
is a self-contained page the Solver UX shows in a sandboxed frame; it has no
network access and never sees your token.

## Results

Measured on 2026-11-06 on macOS (Apple silicon), CPU only, with pack 2.2.
(2.3 differs only in the demo dataset, 2.4 only in its Solver UX surfaces.)

- Hosted path: the Helixor Solver server run locally, behind the same REST API
  as the hosted service. Requests send `"solver": {"mode": "local"}`, which
  asks the server to solve on its own CPU rather than a remote GPU worker.
- MIP: OR-Tools 9.15 (SCIP).

All numbers come from the independent scorer. The penalty includes the two
acknowledgement rules.

### Initial plan (232 containers)

At the start, only the allocation is acknowledged, so every FEU beyond it
still has to be requested.

| Engine | Penalty | Late containers (days) | Allocation used | Spot FEU | Time |
|---|---|---|---|---|---|
| Exact MIP (proven optimum) | 42,740 | 39 (294) | 206.5 / 224 (92.2%) | 6.5 | 0.2 s |
| **Helixor hosted solver** | **43,250** | **39 (324)** | **204.5 / 224 (91.3%)** | **8.5** | **53 s** |

- **On-time count.** The hosted plan has as many on-time containers as the
  proven optimum (193 of 232). That is the top-ranked goal.
- **Total penalty.** The hosted plan is 1.2% above the optimum: 30 more days
  late in total, and 2 FEU more beyond the allocation.
- **Why 39 containers are late.** Their promise dates are shorter than every
  sailing on their lane, so no plan can deliver them on time.

### Continuous planning

This run starts from an existing plan that `adopt` certified. Before cycle 1 it requests that plan's 6.5 FEU beyond the
allocation. It then replays the five event cycles, and the carrier simulator
answers the space requests at the start of each cycle.

- **Moved** counts containers whose sailing differs from the previous plan.
- **Unacknowledged FEU** is space the plan uses that the carrier has not
  acknowledged.
- The penalty excludes the change cost.

**Cycle 1, day 3:** blank sailing, vendor adds cargo, an expedite, extra
allocation. The carriers confirmed 4.5 FEU and rejected 2. Pins: 43 frozen,
176 prefer, 12 free.

| Engine | Penalty | Late (days) | Allocation used | Spot FEU | Unacknowledged FEU | Moved | Time |
|---|---|---|---|---|---|---|---|
| **Helixor repair** | **47,510** | **44 (342)** | **97.0%** | **17** | **13** | **37** | **53.3 s** |
| Exact optimum (same pins) | 45,160 | 42 (290) | 97.3% | 16.5 | 12.5 | 13 | 0.04 s |
| Helixor from scratch | 48,730 | 45 (296) | 95.8% | 19.5 | 15.5 | 85 | 54.4 s |

**Cycle 2, day 8:** carrier delay and roll, cargo-ready slips, a cancellation,
an equipment change, an allocation cut. The carriers confirmed 11.5 FEU and
rejected 1.5. Pins: 45 frozen, 153 prefer, 1 forbid, 14 free.

| Engine | Penalty | Late (days) | Allocation used | Spot FEU | Unacknowledged FEU | Moved | Time |
|---|---|---|---|---|---|---|---|
| **Helixor repair** | **55,130** | **52 (382)** | **97.8%** | **17.5** | **3** | **43** | **55.1 s** |
| Exact optimum (same pins) | 50,120 | 47 (362) | 97.5% | 18 | 3.5 | 20 | 0.04 s |
| Helixor from scratch | 51,420 | 48 (374) | 97.0% | 19 | 6 | 93 | 52.1 s |

**Cycle 3, day 15:** spot space sold out on one sailing, a planner lock, more
vendor cargo. The carriers confirmed 1.5 FEU and rejected 1.5. Pins: 56
frozen, 118 prefer, 8 free.

| Engine | Penalty | Late (days) | Allocation used | Spot FEU | Unacknowledged FEU | Moved | Time |
|---|---|---|---|---|---|---|---|
| **Helixor repair** | **50,055** | **47 (379)** | **98.7%** | **19** | **5** | **26** | **38.1 s** |
| Exact optimum (same pins) | 47,920 | 45 (352) | 98.7% | 19 | 5 | 6 | 0.01 s |
| Helixor from scratch | 51,100 | 48 (332) | 98.0% | 20 | 8.5 | 82 | 47.1 s |

**Cycle 4, day 17: peak-season surge.** A vendor adds 10 FEU for Shanghai to
Los Angeles, on top of a 14 FEU weekly allocation. The carriers confirmed 4
FEU of earlier requests. Pins: 53 frozen, 116 prefer, 10 free.

| Engine | Penalty | Late (days) | Allocation used | Spot FEU | Unacknowledged FEU | Moved | Time |
|---|---|---|---|---|---|---|---|
| **Helixor repair** | **59,865** | **56 (441)** | **99.3%** | **28** | **11** | **25** | **36.7 s** |
| Exact optimum (same pins) | 56,895 | 53 (455) | 99.3% | 28 | 10 | 4 | 0.02 s |
| Helixor from scratch | 58,210 | 54 (462) | 98.6% | 29 | 13.5 | 91 | 48.2 s |

**Cycle 5, day 19:** the carriers answer the surge requests (5 FEU confirmed,
6 FEU rejected), and one surge container slips a day. Pins: 60 frozen, 115
prefer, 1 free.

| Engine | Penalty | Late (days) | Allocation used | Spot FEU | Unacknowledged FEU | Moved | Time |
|---|---|---|---|---|---|---|---|
| **Helixor repair** | **59,750** | **56 (468)** | **99.6%** | **28** | **6** | **31** | **28.9 s** |
| Exact optimum (same pins) | 55,725 | 52 (463) | 99.6% | 28 | 6 | 12 | 0.06 s |
| Helixor from scratch | 58,065 | 54 (537) | 100.0% | 27.5 | 7 | 99 | 48.0 s |

What the cycles show:

- **Over-demand needs spot space, and the plan buys it.** After the surge, the
  plan carries 28 FEU beyond the allocation (up from 19), while using 99% of
  the allocation. The carriers then rejected 6 of the 11 FEU requested for
  the surge. The cycle-5 repair moved the containers that no longer fit, and
  requested 6 FEU more.
- **Freed space is held, then reused or given back near cutoff.** After cycle
  2, the plan holds 1 FEU of acknowledged space it no longer needs on
  BORS-TP1-W05; the cycle-3 repair uses it again. The 0.5 FEU held on
  BORS-TP2-W02 since cycle 1 is released in cycle 3, once that sailing's
  cutoff is within two days.
- **Every cycle starts from a broken plan,** with 3 to 14 hard violations from
  events and rejections. Every repair returns a feasible plan, and the
  independent scorer confirms it.
- **Repair moves far less than re-planning.** The hosted repair moved 25 to 43
  containers per cycle. A from-scratch solve moved 82 to 99.
- **The hosted repair is 4.5% to 10% above that optimum**, with 2 to 5 more late
  containers, and it moves 2 to 6 times as many containers as the optimum. Before
  the acknowledgement rules, the same replay was within 0.2% to 2.3%. Switching
  the two new rules off on the cycle-2 request does not close the gap
  (9.4%). This is a known solver issue with repairs that must re-plan many
  containers, and a fix is planned. The example does not work around it.
- **From scratch beats the hosted repair on penalty in cycles 2, 4 and 5**,
  at the price of re-booking 91 to 99 containers. With the fix, the
  repair should be close to the optimum.

## Tests

```bash
pytest -q                                          # offline: lowering, scorer, events, pack, weights, MIP, report KPIs
SOLVER_LIVE=1 pytest -q tests/test_live_solver.py  # hosted: needs SOLVER_URL and SOLVER_TOKEN loaded
```

The live tests run only with `SOLVER_LIVE=1`, because they publish the pack to
your account and run hosted solves. They check four things on a real solver:

- The pack publishes.
- The demo dataset solves.
- The solver's certificate equals the independent score.
- Frozen pins hold, a forbid pin is respected, and over-capacity pins fail
  closed with `pinned_set_infeasible`.

The offline tests also guard against drift. If the weights in the manifest
and the scorer disagree, a test fails.

## Layout

```text
AGENTS.md                  orientation and rules for AI coding agents
pack/                      config-only solver pack (pack.yaml, manifest with UX + visualizations, demo dataset)
docs/guide.html            illustrated guide: the business problems, scenarios and diagrams
docs/guide.pdf             the same guide as a PDF (print guide.html to A4 to regenerate)
docs/TUTORIAL.md           step-by-step walkthrough of the demo
ocean_routing/
  model.py                 scenario records (containers, sailings) and plan io
  generate.py              deterministic synthetic scenario
  lowering.py              records -> solver request (entities, groups, prior_plan, pins)
  client.py                hosted REST client (packs, manifest, solve)
  planner.py               initial plan, certify, repair / continuous-planning pin policy
  events.py                carrier, vendor, planner and space events and what they disturb
  space.py                 carrier actions (request, hold, release) and the carrier simulator
  scorer.py                independent scorer (feasibility + KPIs + weighted penalty)
  optimum.py               exact MIP yardstick (optional, ortools)
  pinning.py               pinning scenarios: one disruption, one pin choice per run
  report.py                renders the manifest's declared visualizations as HTML
  surfaces.py              builds the pack's Solver UX surfaces (pack/ui) from ui/src
  cli.py                   python -m ocean_routing ...
data/scenario.json         generated scenario (seed 7)
data/events/replay.json    five event cycles for continuous planning, including a peak surge
scripts/build_pack.py      builds the pack zip (publish-pack does this for you)
scripts/build_surfaces.py  rebuilds pack/ui/*.html from ui/src (the pack build refuses stale ones)
ui/src/                    surface.js and surface.css: the Solver UX surfaces' source
scripts/summary_table.py   prints the continuous-planning tables from out/continuous/summary.json
tests/                     offline tests, and opt-in live tests
out/, dist/                generated; not committed
```

## Limits

- **The data is synthetic.** Every carrier, vessel, vendor and PO is invented.
  Only the ports are real UN/LOCODEs.
- **Search budget.** The hosted engine runs a 300,000-evaluation
  simulated-annealing search, declared in the manifest's `search` block. That
  takes about 50 s here. A smaller budget is faster and worse: before the
  acknowledgement rules, 100,000 evaluations scored 43,360 in 20 s.
- **Hosted repair quality.** Continuous-planning repairs land 4.5% to 10% above
  the same-pins optimum. This is a known solver issue, and a fix is planned
  (see Results). The exact MIP shows what the same request allows.
- **Pack policy.** Keep `policy_id: booking_consolidation`. With another
  policy id, the server may not apply the pack's own rules.
- **Solver UX charts.** The pack's surfaces need a Solver UX that hosts pack
  surfaces. They show the plan's views but not the continuous-planning and
  pinning replays; `python -m ocean_routing report` adds those.
- **Acceptance risk is one price.** Relying on unacknowledged space costs the
  same on every carrier. A carrier that rarely accepts over-allocation could
  carry a higher price per sailing, as one more lowered attribute.
- **What the example does not model:** money (contract versus spot rates), port
  or carrier preferences, transhipment, or equipment availability. Each would
  be one more declared rule over data the records carry.
