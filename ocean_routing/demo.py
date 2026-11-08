"""Build the pack's demo request, pack/datasets/two_lane_week.demo.json.

One peak week on the two Los Angeles lanes, taken from the synthetic
generator: containers ready in week 1 and the sailings of weeks 1 to 3 (week 3
has no allocation, only space on sale).

The generator's promise dates are drawn from a fixed lead time and are often
earlier than any sailing a container can catch, so a slice of it carries
late containers no plan can avoid. The demo instead quotes each promise the
way a forwarder would: from a sailing the container can make (one of its
first two by arrival), plus 0 to 3 days of slack. Every container can then
arrive on time, and the peak's demand exceeds the allocation on the early
sailings, so keeping every promise means buying spot space instead of rolling
cargo to a later sailing.

    python scripts/build_demo_dataset.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict

from .generate import generate
from .lowering import lower
from .model import Scenario

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "pack/datasets/two_lane_week.demo.json"
LANES = ("CNSHA-USLAX", "CNYTN-USLAX")
SEED = 10
DEMAND_FACTOR = 1.5
WEEK = 1


def demo_scenario(seed: int = SEED, demand_factor: float = DEMAND_FACTOR) -> Scenario:
    full = generate(seed, weeks=WEEK + 1, demand_factor=demand_factor)
    sailings = [s for s in full.sailings if s.lane in LANES and s.week >= WEEK]
    containers = [c for c in full.containers if c.lane in LANES and WEEK * 7 <= c.ready_day < (WEEK + 1) * 7]
    rng = random.Random(seed * 1000 + 2)
    for c in containers:
        catchable = sorted((s for s in sailings if s.lane == c.lane and c.ready_day <= s.cutoff_day),
                           key=lambda s: (s.eta_day, s.id))
        if not catchable:
            raise ValueError(f"{c.id}: no sailing on {c.lane} after ready day {c.ready_day}")
        quoted = catchable[min(rng.randint(0, 1), len(catchable) - 1)]
        c.promise_day = quoted.eta_day + rng.randint(0, 3)
    return Scenario(f"demo-{'-'.join(LANES)}-week{WEEK}", full.window_start, 1, full.carriers, sailings, containers,
                    metadata={"seed": seed, "demand_factor": demand_factor, "generator": "ocean_routing.demo"})


def demo_request() -> Dict[str, Any]:
    return lower(demo_scenario()).problem


def write(path: Path = OUT) -> Path:
    path.write_text(json.dumps(demo_request()) + "\n")
    return path
