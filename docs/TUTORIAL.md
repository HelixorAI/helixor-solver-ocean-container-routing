# Tutorial: planning ocean containers on the hosted Helixor Solver

This tutorial walks through the whole demo, from an empty terminal to a plan
that keeps itself current as carriers and vendors change things. Along the
way it explains each modelling decision and why it was made.

You will:

1. Look at the business records: containers, sailings and allocations.
2. Read the model: one manifest that declares every rule.
3. Publish it to the hosted solver as a solver pack.
4. Make a first plan, and check it with an independent scorer.
5. Compare it with the proven optimum.
6. Look at it: the charts declared in the manifest, rendered as one page.
7. Take over a plan that another planning system produced.
8. Control change with pins: freeze, forbid, prefer.
9. Ask carriers for space beyond the allocation, and live with their answers.
10. Repair the plan, cycle after cycle, as events arrive, including a
    peak-season surge.

Every number in this tutorial comes from a real run, scored by the independent
scorer in this repository. The "Results" section of the [README](../README.md)
says where and how they were measured.

Before you start:

- For the business story with diagrams of each scenario, read
  [`guide.html`](guide.html) first (download it and open it in a browser).
- The README's **Start here** section lists every command in order, what each
  one needs (only the hosted ones need a Helixor Solver license), where its
  output goes and how long it takes. It also defines the terms used here
  (FEU, lane, sailing, cutoff, allocation, spot, pin, change cost).
- The README's **Reading the output** explains the one-line plan summary every
  command prints.

---

## 1. The problem

A shipper (the customer) moves containers from ports in China to ports in the
United States. Each container belongs to a purchase order from a vendor. It has:

- a **lane**: the port of loading and the port of discharge, for example
  `CNSHA-USLAX` (Shanghai to Los Angeles);
- a **size**: a 40' box is one FEU (forty-foot equivalent unit), a 20' box is
  half;
- a **cargo-ready day**: the first day the vendor can deliver it to the
  terminal;
- a **promise day**: the day it is due at the destination port.

Carriers sell space on **sailings**. A sailing is one voyage of one vessel on
one service, and it has:

- a **cutoff day**: the last day a container can reach the terminal for it;
- a **departure day** (ETD) and an **arrival day** (ETA);
- the customer's **allocation**: FEU the carrier set aside for this customer
  on this sailing at contract rates;
- **spot space** on sale beyond the allocation, at market rates.

The planner books every container onto one sailing. A good plan, in order of
importance:

1. **delivers on time**: as many containers as possible arrive by their promise
   day;
2. **uses the allocation**: the customer pays for allocated space whether it
   uses it or not, so every allocated FEU should be filled;
3. **buys little spot space**.

Demand does not always fit the allocation. When it is higher, the planner can
**over-allocate** (ask a carrier to take more than the allocation) or book
**spot space** on the carrier's website. Neither is certain until the carrier
**acknowledges** it, and either can be rejected. When space frees up, the
planner does not hand it straight back: a later change may need it.

Then the world changes. A carrier blanks a sailing, rolls a container or cuts
an allocation. A vendor adds containers, cancels one, expedites one, or ships
late. The plan has to be repaired without re-booking everything with the
carriers.

## 2. The data

[`data/scenario.json`](../data/scenario.json) is a generated scenario:
[`ocean_routing/generate.py`](../ocean_routing/generate.py) with seed 7. Every
carrier, vessel, vendor and PO is invented; only the port codes are real.

- 232 containers, 213 FEU, on 3 lanes;
- 4 carriers running 8 weekly services: 64 sailings in weeks 0 to 7;
- 224 FEU of allocation, on the sailings of weeks 0 to 6. Week 7 sailings
  carry spot space only, so late cargo has somewhere to go.

A sailing:

```json
{"id": "ATLS-PSX-W03", "carrier": "ATLS", "service": "PSX", "vessel": "Atlas Galena",
 "voyage": "PSX49E", "lane": "CNSHA-USLAX", "week": 3, "cutoff_day": 20, "etd_day": 22,
 "eta_day": 36, "allocation_feu": 6.0, "spot_feu": 2.0, "status": "scheduled",
 "confirmed_feu": 6.0, "pending_feu": 0.0}
```

A container:

```json
{"id": "CNSHA-0001", "po": "PO4501", "vendor": "Lumen Home", "lane": "CNSHA-USLAX",
 "size": "40GP", "ready_day": 0, "promise_day": 24}
```

Days count from the start of the window (`window_start`, day 0). `today` is
the planning day; it moves forward in the continuous-planning replay.

## 3. The model

The whole model is one file:
[`pack/manifests/ocean_container_routing.yaml`](../pack/manifests/ocean_container_routing.yaml).
No code of any kind goes to the solver. This section reads it top
to bottom.

### The decision

```yaml
decision:
  shape: assignment
  entities: {name: containers, source: containers, identity: id}
  groups:   {name: sailings,   source: sailings,   identity: id}
  domain:
    multiplicity: one_to_many        # one sailing carries many containers
    total_over_entities: true        # every container must be booked
    total_over_groups: false         # a sailing may carry none
```

The solver assigns each **entity** (container) to one **group** (sailing).
This is bin packing with a calendar: the bins are sailings with dates and
capacities, and the items have sizes and deadlines.

### Three kinds of rule

The manifest separates rules by what breaking them means.

**Eligibility** rules say where a container *may* go. The solver never
considers anything else, so they cost no search effort:

```yaml
lane_match:             {type: eligibility, contract: assigned_equals,
                         parameters: {entity_attribute: lane, group_attribute: lane}}
cargo_ready_by_cutoff:  {type: eligibility, contract: fits,
                         parameters: {entity_attribute: ready_day, group_attribute: cutoff_day}}
```

**Hard** rules must hold in every plan the solver returns. There is one: the
FEU on a sailing stays within the space on sale (allocation plus spot).

```yaml
sailing_capacity:  {type: hard, contract: group_sum_max,
                    parameters: {entity_attribute: feu, group_attribute: capacity_feu}}
```

**Soft** rules are the business goals. Each has a weight, and the weights rank
them:

| Rule | Contract | Weight | Penalty |
|---|---|---|---|
| `on_time_delivery` | `assigned_in_entity_set` | 1000 | 1 per late container |
| `allocation_utilization` | `aggregate sum >= allocation_feu` (empty sailings judged too) | 100 | 1 per unused allocated FEU |
| `minimize_spot` | `aggregate sum <= allocation_feu` | 40 | 1 per FEU beyond the allocation |
| `unconfirmed_space` | `aggregate sum <= confirmed_feu` | 30 | 1 per FEU on space the carrier has not acknowledged |
| `new_space_request` | `aggregate sum <= committed_feu` | 10 | 1 per FEU that needs a new request to the carrier |
| `days_late` | `group_fits eta_day <= promise_day` | 5 | 1 per day late |

Read the weights as prices:

- No amount of utilisation or spot saving is worth one late container
  (1000).
- Moving 1 FEU from spot onto unused allocation saves 140: 100 for the
  allocation it fills and 40 for the spot it no longer buys. It always pays.
- An FEU beyond the allocation costs 40 on space the carrier already
  acknowledged, 70 on space requested but not yet answered (40 + 30), and 80
  on space still to request (40 + 30 + 10). The plan fills held space first
  and asks for new space last. Section 9 explains these two rules.
- Over-allocating to save a late container always pays: 80 is far below
  1,000.
- Days late break ties between plans that are late for the same number of
  containers.

### Three modelling choices that matter

**On time is a set, not a comparison.** Lateness depends on the pair (this
container, this sailing): the sailing's arrival day against the container's
promise day. The lowering step
([`ocean_routing/lowering.py`](../ocean_routing/lowering.py)) gives each
container the list of sailings that would deliver it on time:

```json
{"id": "CNSHA-0018", "lane": "CNSHA-USLAX", "size": "40GP", "feu": 1.0,
 "ready_day": 13, "promise_day": 32, "on_time_sailings": []}
```

The rule then only asks "is the chosen sailing in that list?". That list is
derived from the schedule and the promise date; the caller never chooses it.
An empty list, as here, means no sailing on the lane is fast enough. That
container will be late in every plan, and the scorer counts it.

**Spot is overflow, not a second slot.** A container is booked on a sailing.
FEU beyond the allocation on that sailing is spot. An earlier version of this
model gave each sailing two groups, "allocation" and "spot". It had twice the
groups and many moves that change nothing real (the same box on the same ship,
under a different contract). Before the acknowledgement rules were added, its
best hosted result was 3.5% above the optimum, against 0.9% for the overflow
model. It is also how carriers bill.

**Eligibility, not hard, for lane and cutoff.** As hard rules, the search
spends effort proposing moves it then rejects, and the starting point can be
infeasible. As eligibility rules, the solver never proposes them.

### Search

```yaml
search: {acceptor: simulated_annealing, max_evaluations: 300000}
```

This is a budget, declared with the model. 300,000 evaluations take about 50
seconds on this instance. A smaller budget is faster and worse: before the
acknowledgement rules, 100,000 evaluations took about 20 seconds and scored
43,360 instead of 42,825.

### Pinning

```yaml
pinning:
  modes: [frozen, forbid, prefer]
  granularity: entity
  prior_plan: true
  change_cost: {default: 25.0, min: 0.0}
```

This declares that a request may carry the previous plan and per-container
pins. Section 8 covers what each mode does.

### Visualizations

```yaml
ui:
  surfaces:
    solve: {enabled: true, primary: true, label: "Plan container sailings", ...}
  visualizations:
    - {id: plan_kpis, type: kpi, ...}
    - {id: sailing_load, type: stacked_bar, ...}
    - {id: weekly_utilization, type: bar, ...}
    - {id: schedule, type: gantt, ...}
    - {id: late_containers, type: table, ...}
```

`ui.surfaces.solve` makes the problem type appear on the Solver UX's solve
page. `ui.visualizations` declares the charts. Section 6 renders them, and the
pack's own UI surfaces render them inside the Solver UX.

## 4. Set up and publish the pack

You need Python 3.10+, and a Helixor account at
[helixor.dev](https://helixor.dev) whose organization holds a Helixor Solver
license. The hosted solves run only under that license; take the Solver API
origin and a token for your account from it.

This repository contains no solver. Without a license you can still build
the exact MIP's plan and replay from it offline (`continuous --start
out/plan-optimum.json --offline`, section 10), then score and report.

```bash
git clone https://github.com/HelixorAI/helixor-solver-ocean-container-routing.git
cd helixor-solver-ocean-container-routing
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # fill in SOLVER_URL and SOLVER_TOKEN
# Python does not read .env itself; load it in every new shell:
set -a && . ./.env && set +a
```

A **solver pack** is a zip of configuration:

```text
pack/
  pack.yaml                              id, version, policy, manifest and dataset list
  manifests/ocean_container_routing.yaml the model
  datasets/two_lane_week.demo.json       a small request to try in the Solver UX
  ui/ocean.problem.html                  Solver UX view of the containers and sailings
  ui/ocean.solution.html                 Solver UX view of a plan
```

The two `ui/` pages are the pack's `surfaces` (roles `problem` and
`solution`). Each is one self-contained HTML page built from `ui/src` by
`python scripts/build_surfaces.py`, with no network access. The Solver UX
fetches it from the pack and shows it in a sandboxed frame, sending it the
request's records, the solve result and the manifest. It renders the same
`ui.visualizations` as the report in section 6.

```bash
python -m ocean_routing publish-pack
```

This builds `dist/ocean_container_routing-2.4.zip` (byte-for-byte reproducible),
asks the solver to validate it, uploads it, activates it, and reads the
manifest back. A validation error stops it with the solver's reasons.

## 5. The first plan

```bash
python -m ocean_routing plan
python -m ocean_routing score out/plan-helixor.json
```

`plan` lowers the scenario into a request and sends it:

```text
POST /api/v1/solve/ocean_container_routing
{"problem_type": "ocean_container_routing",
 "problem": {"containers": [...], "sailings": [...]},
 "solver": {"mode": "local"}}
```

In the request, each sailing carries `capacity_feu` (allocation plus spot on
sale) instead of `spot_feu`; departed or cancelled sailings are left out.

The answer carries the placements and a **certificate**:

```json
{"is_feasible": true, "hard_penalty": 0.0, "soft_penalty": 50680.0,
 "reason": "feasible with penalty: on_time_delivery (penalized, penalty 47 x weight 1000); days_late (...)",
 "violations": [{"name": "on_time_delivery", "is_hard": false, "penalty": 47.0,
                 "weight": 1000.0, "weighted_penalty": 47000.0, ...}, ...]}
```

This certificate is from the cycle-3 repair in section 10, shown here because
it has every part a certificate can have. Its soft penalty, 50,680, is the
scorer's 50,055 plus the change cost of the 25 `prefer` bookings that moved
(25 × 25 = 625). That cycle moved 26 containers in all; the other one was free
(an event touched it), and free moves cost nothing. The scorer reports business penalty
only; the certificate prices the moves too.

The client accepts a plan only when three things hold:

1. the certificate says it is feasible;
2. every container is placed;
3. the **independent scorer**
   ([`ocean_routing/scorer.py`](../ocean_routing/scorer.py)) agrees it is
   feasible. The scorer reimplements the business rules directly from the
   records, without the solver.

An HTTP 200 alone is never accepted. If any check fails, the command exits with
the typed reasons; it never passes on a plan it could not verify.

Result on the scenario:

| Engine | Penalty | Late containers (days) | Allocation used | Spot FEU | Time |
|---|---|---|---|---|---|
| Exact MIP (proven optimum) | 42,740 | 39 (294) | 206.5 / 224 (92.2%) | 6.5 | 0.2 s |
| Helixor hosted solver | 43,250 | 39 (324) | 204.5 / 224 (91.3%) | 8.5 | 53 s |

The hosted plan has as many on-time containers as the optimum, which is the
top goal. It is 1.2% above the optimum on total penalty: 30 more days late in
total, and 2 FEU more beyond the allocation instead of on unused allocation.

Why are 39 containers late in every plan? Their `on_time_sailings` lists are
empty: no sailing on their lane is fast enough.

## 6. Compare and visualize

### A yardstick

[`ocean_routing/optimum.py`](../ocean_routing/optimum.py) reads the exact
request the hosted solver gets, solves it as a mixed-integer program, and
proves the optimum.

```bash
python -m ocean_routing optimum          # -> out/plan-optimum.json
```

A test checks that the manifest and the scorer use the same weights. If one
drifts, `pytest` fails.

### The report

```bash
python -m ocean_routing report out/plan-helixor.json -o out/report.html
```

This writes one self-contained HTML page (no external scripts, light and dark
themes, every chart with a table view). It renders the manifest's
`ui.visualizations`, so the charts come from the model's declarations:

- **Plan at a glance** (`type: kpi`): on time, late containers, allocation
  used, spot space and days late. Each metric names a *rule* and a *measure*:

  ```yaml
  - {rule: allocation_utilization, measure: fill_share, label: "Allocation used", format: percent}
  ```

  The renderer evaluates the measure from that rule's own contract and
  parameters (`aggregate sum >= allocation_feu`, empty sailings judged), so it
  needs no knowledge of ocean routing. A test checks that every KPI equals
  the independent scorer's number.
- **Load by sailing** (`type: stacked_bar`): for each sailing, allocation used,
  spot beyond the allocation, and allocation left unused, with the space on
  sale marked.
- **Allocation used by week and carrier** (`type: bar`).
- **Sailing schedule** (`type: gantt`): departure to arrival per lane; hollow
  bars carry nothing.
- **Late containers** (`type: table`): filtered by the `on_time_delivery` rule.

If `out/continuous/summary.json` or `out/pinning.json` exist, the page adds the
continuous-planning and pinning results (sections 8 and 9).

To chart any repaired plan against its own world, pass the request it
answered:

```bash
python -m ocean_routing report out/continuous/cycle-3-helixor.json \
    --request out/continuous/cycle-3-request.json -o out/report-cycle-3.html
```

> **What about the Solver UX?** The Solver UX shows this problem type on its
> solve page (that is what `ui.surfaces.solve` does) and the generic panels
> every problem gets: the certificate's hard and soft totals and the rule
> results. The charts come from the pack: **Visualize** under Data Input shows
> the `problem` surface for the loaded request, and the solution panel shows
> the `solution` surface for the plan. Both render these same declarations.
> The report command remains the way to see them outside the Solver UX, and
> it adds the replay sections.

## 7. Take over an existing plan

You may already have a plan, made by another planning system. Bring it as
`{"bookings": [{"container", "sailing"}]}` or as
`{"placements": [{"entity", "group"}]}`, and adopt it:

```bash
python -m ocean_routing adopt out/plan-optimum.json       # -> out/plan-adopted.json
```

`adopt` runs three checks before it accepts the plan:

1. **Ids.** Every container and sailing must exist in the scenario, and every
   container must be booked. It stops with the lists of offenders.
2. **Independent score.**
3. **Hosted certificate.** The plan goes to the solver with every booking
   `frozen`. The solver judges it under the pack's rules and returns the
   certificate without moving anything, with the same penalty the scorer
   gives.

If the plan breaks a rule that the frozen bookings alone decide (wrong lane,
cargo not ready, a sailing over capacity), the solver refuses with
`422 pinned_set_infeasible` and names the rule and the sailing. It does not
quietly fix the plan.

From here on, the adopted plan is the starting point.

## 8. Pins: deciding what may change

A request can carry the previous plan (`prior_plan`) and a pin for any
container:

| Pin | Meaning | Typical reason |
|---|---|---|
| `frozen` to a sailing | must stay on that sailing | booked with the carrier; a planner's decision |
| `forbid` a sailing | must not use that sailing | the carrier rolled or refused it |
| `prefer` | keep the prior sailing unless a move gains more than `change_cost` | everything stable |
| (none) | `pin_default` applies; `free` plans it from scratch | an event changed it |

`free` is not a pin mode in the manifest: it is the absence of a pin. The
manifest declares the three modes a request may send.

`change_cost` is a price, in the same units as the rule weights. At 25, a
move that saves a late container (1000) or 1 FEU of unused allocation (100)
pays; a move that shaves four days off a container that is late anyway
(4 × 5 = 20) does not.

### The pinning scenarios

```bash
python -m ocean_routing pinning
```

This holds everything fixed except the pins: the same base plan (the adopted
one), the same disruption (the carrier blanks `BORS-TP1-W01`, leaving 5 FEU of
containers without a booking), and one pin choice per run. Every result is
scored independently, and every pin is checked against the returned plan. A
frozen container that moved, or a container left on a forbidden sailing,
fails the run.

| Scenario | Pins | Outcome | Penalty | Late (days) | Allocation used | Spot FEU | Moved | Optimum, same pins | Time |
|---|---|---|---|---|---|---|---|---|---|
| no prior plan | none (no prior plan) | feasible | 44,610 | 40 (370) | 91.8% | 12 | 92 | 43,985 | 56.9 s |
| prefer, change cost 0 | all prefer, change cost 0 | feasible | 44,255 | 40 (353) | 92.5% | 10.5 | 68 | 43,985 | 64.9 s |
| prefer, change cost 25 | all prefer, change cost 25 | feasible | 44,195 | 40 (341) | 92.5% | 10.5 | 27 | 44,025 | 61.6 s |
| prefer, change cost 1000 | all prefer, change cost 1000 | feasible | 44,115 | 40 (307) | 92.2% | 11 | 5 | 44,115 | 59.4 s |
| freeze week 0, prefer the rest | 19 frozen; others prefer, change cost 25 | feasible, pins held | 44,235 | 40 (349) | 92.5% | 10.5 | 24 | 44,045 | 57.2 s |
| forbid COBT-CPX-W01, prefer the rest | 3 forbid; others prefer, change cost 25 | feasible, pins held | 44,220 | 40 (346) | 92.5% | 10.5 | 29 | 44,025 | 63.0 s |
| freeze too much onto ATLS-PSX-W01 | 11 frozen; others prefer, change cost 25 | refused 422 `pinned_set_infeasible`: sailing_capacity in ATLS-PSX-W01 | | | | | | | 0.0 s |

What it shows:

- **Re-planning from scratch is worst on both counts.** It moves 92
  containers, each one a re-booking with a carrier, and its penalty is the
  highest. Starting from a good plan helps the search as well as the
  business.
- **`change_cost` is the churn dial.** At 0 the prior plan is only a starting
  point: 68 moves. At 25: 27 moves. At 1000: 5 moves, exactly the 5
  containers the blanked sailing displaced, and the plan reaches the optimum
  for those pins. On this instance the penalty falls as the change cost
  rises, because the base plan was already close to optimal for the world
  before the blank, so staying put is usually right.
- **Pins have a price at the optimum, and the column shows it.** The
  "Optimum, same pins" column is the scorer's penalty for the exact MIP's
  plan under the same request (change cost excluded). With no pins it is
  43,985; a change cost of 25 or a forbid raises it to 44,025, the week-0
  freeze to 44,045, and a change cost of 1000 to 44,115. The hosted repair
  lands 170 to 625 above it, except at change cost 1000, where it matches.
- **Forbid moves exactly what it must.** The three containers on the refused
  sailing all left it, and each run checks that.
- **Impossible pins are refused, not repaired.** Freezing 11 containers
  (11 FEU) onto a sailing with 10 FEU on sale is refused before any
  search, naming the rule and the sailing.

## 9. Space beyond the allocation

### The lifecycle

The allocation is the contract, so the carrier has already acknowledged it.
Every FEU beyond it goes through a lifecycle on each sailing:

```text
          request                 carrier answers
 (none) ──────────▶ pending ──┬──▶ confirmed ──── held while a later change may need it
                              │                     │
                              └──▶ rejected         └── released close to cutoff
                                   (no more on that sailing)
```

The sailing record carries the state:

```json
{"id": "ATLS-PSX-W03", "allocation_feu": 6.0, "spot_feu": 2.0,
 "confirmed_feu": 6.0, "pending_feu": 0.0, ...}
```

The request to the solver carries `confirmed_feu`, and `committed_feu`
(confirmed plus pending).

### What the solver weighs

Two soft rules price relying on space that is not yet certain:

- `unconfirmed_space` (30 per FEU): load beyond `confirmed_feu`. The carrier
  may still say no.
- `new_space_request` (10 per FEU): load beyond `committed_feu`. A new request
  has to go out, and it is one more thing the carrier can refuse.

So among the ways to carry a container beyond the allocation, the plan
prefers, in order:

1. space it already holds;
2. space it has already asked for;
3. a new request.

It still goes beyond the allocation whenever that delivers a container on
time.

### What the planner tells the carriers

After every plan, [`ocean_routing/space.py`](../ocean_routing/space.py)
compares the plan's load on each sailing with its space and emits events:

| Situation | Action | Event |
|---|---|---|
| plan needs more than confirmed + pending | ask for the gap | `space_requested` |
| plan needs less than confirmed, cutoff far away | **hold** it: a surge, roll or blank sailing may need it | (none; reported as held) |
| plan needs less, cutoff within `--release-days` (2) | withdraw what is pending, then give back held space | `space_withdrawn`, `space_released` |

The answers come back as `space_confirmed` and `space_rejected`. Every one of
these events is checked against the sailing's state. A carrier cannot confirm
more than was asked; the planner cannot release space it never held or ask for
more than is on sale. An event that breaks these checks is refused, not
skipped.

A rejection caps the sailing at what the carrier accepted. The containers
that no longer fit break the hard capacity rule, and the next repair moves
them.

### Pins and acknowledgement

The replan policy freezes a booking near cutoff because it is "with the
carrier". That is only true within acknowledged space. A booking on pending
space is not booked yet, so the policy freezes bookings only up to
`confirmed_feu` and leaves the rest to the repair. The run reports those
containers in a note:
`CNNGB-0196: on DORM-DEC-W03 near cutoff but not within acknowledged space; left to the repair`.

### The carrier simulator

In the replay, a simulator plays the carriers. The scenario's `simulation`
block says how much extra each sailing's carrier will really take:

```json
"simulation": {"carrier_accepts_extra_feu": {"ATLS-PSX-W00": 0.0, "ATLS-PSX-W01": 4.0, ...}}
```

The planner never reads it, and lowering never sends it to the solver; a test
checks that. The plan is made on what carriers have said, not on what they
will say. Accepted space is confirmed. The rest is rejected, and that sailing
is capped.

## 10. Repair and continuous planning

The real loop: every few days, roll the clock forward, apply what happened,
repair.

```bash
python -m ocean_routing continuous --start out/plan-adopted.json --compare
```

This replays [`data/events/replay.json`](../data/events/replay.json): five
cycles over the first three weeks. Cycle 4 is a peak-season surge: a vendor
adds 10 FEU for Shanghai to Los Angeles on top of a 14 FEU weekly allocation,
more than the allocation can carry, so the plan has to buy space beyond it.

Before cycle 1, the start plan's space beyond the allocation is requested from
the carriers (section 9). Each cycle then:

1. **Advances the clock.** Sailings that departed leave the problem with their
   containers. Their utilisation is final.
2. **Takes the carriers' answers** from the simulator: `space_confirmed` or
   `space_rejected` for each pending request.
3. **Applies the events.** [`ocean_routing/events.py`](../ocean_routing/events.py)
   supports these types:

   | Event | Source | Effect |
   |---|---|---|
   | `sailing_cancelled` | carrier | its containers are disturbed |
   | `sailing_delayed` | carrier | ETD, ETA and cutoff shift; its containers are disturbed |
   | `allocation_changed`, `spot_changed` | carrier | the space changes |
   | `container_rolled` | carrier | the container is disturbed and forbidden that sailing |
   | `containers_added` | vendor | new containers, disturbed |
   | `container_cancelled` | vendor | the container leaves |
   | `container_expedited` | vendor | new promise day; disturbed |
   | `cargo_ready_changed`, `container_changed` | vendor | new ready day or size; disturbed |
   | `container_locked` | planner | frozen to the chosen sailing |
   | `space_confirmed`, `space_rejected` | carrier | a pending request is acknowledged or refused; a refusal caps the sailing |
   | `space_requested`, `space_withdrawn`, `space_released` | planner | ask for space, take back a request, give back held space |

   An event naming an unknown sailing or container, or an unknown event type,
   is refused, not skipped.
4. **Scores the old plan against the new world.** It is always broken here:
   3 to 14 hard violations per cycle, from events and rejections.
5. **Sends one repair request**, with pins from the replan policy
   ([`ocean_routing/planner.py`](../ocean_routing/planner.py)):
   - planner locks are `frozen`;
   - bookings on a sailing whose cutoff is within 7 days (`--freeze-days`) are
     `frozen`, because they are with the carrier, but only within
     acknowledged space;
   - containers an event touched get no pin and are planned freely;
   - a rolled container gets `forbid` for that sailing;
   - everything else is `prefer`, with `change_cost: 25` (`--change-cost`).
6. **Tells the carriers** what the repaired plan needs: requests, holds and
   releases (`--release-days`, default 2).

`--offline` runs the same replay with the exact MIP doing the repairs. It
needs no token and is labelled as a dry run: it carries no hosted
certificate, writes to `out/continuous-offline/` so it never stands in for
hosted results, and cannot be combined with `--compare`. Without a license,
start it from the exact MIP's plan: `--start out/plan-optimum.json`.

With `--compare`, the same request also goes to the exact MIP, and a from-scratch hosted solve shows the churn of re-planning. The
README has the full tables. In short (penalty without change cost, and
containers moved):

| Cycle | Carriers: confirmed / rejected FEU | Helixor repair | Exact optimum, same pins | Helixor from scratch |
|---|---|---|---|---|
| 1, day 3 | 4.5 / 2 | 47,510, moved 37 | 45,160, moved 13 | 48,730, moved 85 |
| 2, day 8 | 11.5 / 1.5 | 55,130, moved 43 | 50,120, moved 20 | 51,420, moved 93 |
| 3, day 15 | 1.5 / 1.5 | 50,055, moved 26 | 47,920, moved 6 | 51,100, moved 82 |
| 4, day 17 (surge) | 4 / 0 | 59,865, moved 25 | 56,895, moved 4 | 58,210, moved 91 |
| 5, day 19 | 5 / 6 | 59,750, moved 31 | 55,725, moved 12 | 58,065, moved 99 |

What it shows:

- **Over-demand is carried on spot space.** After the surge the plan uses 99%
  of the allocation and carries 28 FEU beyond it, up from 19. The carriers
  reject 6 of the 11 FEU requested for the surge; the cycle-5 repair moves the
  containers that no longer fit and requests 6 FEU more.
- **Freed space is held, not handed back at once.** After cycle 2 the plan
  holds 1 FEU it no longer needs on BORS-TP1-W05, and the cycle-3 repair uses
  it again. The 0.5 FEU held on BORS-TP2-W02 is released in cycle 3, once its
  cutoff is within two days.
- **Every repair is feasible**, and the independent scorer confirms it.
  Repair moves far less than re-planning: 25 to 43 containers against 82 to 99.
- **The hosted repair does not reach the proven optimum:** it is 4.5% to 10%
  above it, and moves 2 to 6 times
  as many containers. This is a known solver issue with repairs that must
  re-plan many containers, not a property of the model: switching the two
  acknowledgement rules off does not close the gap. A fix is planned, and the
  example does not work around it.

The freeze window, the change cost and the release window are where the
business decides how much churn and how much held space a better plan is
worth.

## 11. Make it yours

To plan your own containers:

1. **Map your records** to the two shapes in section 2. `ready_day`,
   `promise_day`, `cutoff_day`, `etd_day` and `eta_day` are day numbers from
   the start of your window. Add your own fields freely; the solver reads only
   the ones the rules name.
2. **Keep lowering on your side.** `on_time_sailings` and `capacity_feu` are
   derived in [`lowering.py`](../ocean_routing/lowering.py); keep that logic,
   or compute the same fields in your own system.
3. **Set the weights to your prices.** If spot space costs you more than
   leaving allocation unused, swap 100 and 40. Keep the test that checks the
   manifest, the scorer and any other engine agree.
4. **Choose your freeze window and change cost** from how your carriers handle
   re-bookings.
5. **Add a rule as a declaration.** Carrier preferences, contract against spot
   rates in money, or equipment availability would each be one more rule over
   fields your records carry. Add each with a test in the scorer first, then
   check the certificate agrees.

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `422 pinned_set_infeasible` | the frozen pins alone break a hard rule (capacity, lane, cutoff) | read `missing` in the body: it names the rule and the sailing; change the pins |
| `no admissible plan: independent scorer rejected the solver's plan` | the solver's certificate and the scorer disagree | treat as a defect; keep the request (`out/continuous/cycle-N-request.json`) and report it |
| the pack publishes but plans place nothing, or ignore your rules | the pack declares a different `policy_id` | keep `policy_id: booking_consolidation` |
| solves take about a minute | the declared search budget (300,000 evaluations) | lower `search.max_evaluations` for interactive use, and accept a slightly worse plan |
| hosted repairs 4–10% above the optimum, with many moves | a known solver issue with large repairs (section 10) | compare with `--compare`; the MIP plans show what is reachable |
| no license yet, but want to see the loop | | `continuous --start out/plan-optimum.json --offline` runs the replay with the exact MIP, labelled as a dry run |
| `pytest -q` fails against your solver after loading `.env` | it shouldn't: live tests run only with `SOLVER_LIVE=1` | run `SOLVER_LIVE=1 pytest -q tests/test_live_solver.py` only when you mean to publish and solve |
| `422 solver_pack_rejected`, immutable version conflict | a changed pack published under a version that already exists | bump `version` in `pack/pack.yaml` and the manifest |
