"""Deterministic synthetic scenario: one customer, three lanes, four carriers.

The shape follows a routing matrix + carrier allocation workbook: per lane the
customer's weekly FEU allocation is split across contracted carriers by ratio,
each carrier runs one weekly service on the lane with its own sailing day and
transit time, and carriers sell extra spot space on the same sailings.

Every carrier, vessel, vendor and PO here is invented. Ports use UN/LOCODEs.
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import Dict, List

from .model import Container, Scenario, Sailing

CARRIERS = {
    "ATLS": "Atlas Line",
    "BORS": "Borealis Shipping",
    "COBT": "Cobalt Ocean",
    "DORM": "Dorado Marine",
}

# lane -> weekly allocated FEU and the contracted services on it:
# (carrier, service, sailing weekday 0=Mon, transit days, allocation ratio)
LANES: Dict[str, Dict] = {
    "CNSHA-USLAX": {
        "weekly_feu": 14,
        "services": [
            ("ATLS", "PSX", 1, 14, 0.45),
            ("BORS", "TP1", 3, 16, 0.35),
            ("COBT", "CPX", 5, 19, 0.20),
        ],
        "promise_lead": (17, 27),
    },
    "CNYTN-USLAX": {
        "weekly_feu": 8,
        "services": [
            ("BORS", "TP2", 2, 15, 0.60),
            ("DORM", "DPS", 4, 18, 0.40),
        ],
        "promise_lead": (18, 28),
    },
    "CNNGB-USSAV": {
        "weekly_feu": 10,
        "services": [
            ("ATLS", "AEX", 2, 31, 0.40),
            ("COBT", "SVX", 6, 35, 0.30),
            ("DORM", "DEC", 0, 33, 0.30),
        ],
        "promise_lead": (34, 46),
    },
}

VENDORS = ["Harbor Textiles", "Lumen Home", "Northpine Furniture", "Orbit Toys", "Quarry Ceramics", "Tidewater Apparel"]
VESSELS = ["Aurora", "Bayard", "Calypso", "Delphine", "Everest", "Fjord", "Galena", "Halcyon"]

CUTOFF_DAYS_BEFORE_ETD = 2


def generate(seed: int = 7, weeks: int = 6, demand_factor: float = 1.0) -> Scenario:
    """Containers ready in weeks [0, weeks); sailings run two weeks longer so
    late-window cargo still has somewhere to go. Allocation covers the window
    plus one week; the last week is spot-only space."""
    rng = random.Random(seed)
    sailing_weeks = weeks + 2
    sailings: List[Sailing] = []
    for lane, spec in LANES.items():
        for carrier, service, weekday, transit, ratio in spec["services"]:
            for week in range(sailing_weeks):
                etd = week * 7 + weekday
                allocation = round(spec["weekly_feu"] * ratio) if week <= weeks else 0
                spot = rng.choice([2, 3, 4, 4, 5, 6]) + (4 if week >= weeks else 0)
                vessel = VESSELS[(week + len(sailings)) % len(VESSELS)]
                sailings.append(Sailing(
                    id=f"{carrier}-{service}-W{week:02d}",
                    carrier=carrier,
                    service=service,
                    vessel=f"{CARRIERS[carrier].split()[0]} {vessel}",
                    voyage=f"{service}{week + 46:02d}E",
                    lane=lane,
                    week=week,
                    cutoff_day=etd - CUTOFF_DAYS_BEFORE_ETD,
                    etd_day=etd,
                    eta_day=etd + transit + rng.choice([0, 0, 0, 1, 2]),
                    allocation_feu=float(allocation),
                    spot_feu=float(spot),
                ))

    containers: List[Container] = []
    po_counter = 4500
    for lane, spec in LANES.items():
        lo, hi = spec["promise_lead"]
        for week in range(weeks):
            target_feu = spec["weekly_feu"] * demand_factor * rng.uniform(0.8, 1.25)
            feu = 0.0
            while feu < target_feu:
                size = "20GP" if rng.random() < 0.15 else rng.choice(["40HC", "40HC", "40HC", "40GP"])
                ready = week * 7 + rng.randint(0, 6)
                po_counter += 1
                containers.append(Container(
                    id=f"{lane[:5]}-{len(containers) + 1:04d}",
                    po=f"PO{po_counter}",
                    vendor=rng.choice(VENDORS),
                    lane=lane,
                    size=size,
                    ready_day=ready,
                    promise_day=ready + rng.randint(lo, hi),
                ))
                feu += 0.5 if size == "20GP" else 1.0

    # How much extra each carrier will really accept beyond the allocation, for
    # the carrier simulator. Drawn from its own stream so the records above do
    # not change with it. The planner never sees these numbers.
    behaviour = random.Random(seed * 1000 + 1)
    accepts = {s.id: float(min(s.spot_feu, behaviour.choice([0, 1, 1, 2, 3, s.spot_feu]))) for s in sailings}

    return Scenario(
        name=f"synthetic-seed{seed}",
        window_start="2026-11-09",
        weeks=weeks,
        carriers=dict(CARRIERS),
        sailings=sailings,
        containers=containers,
        metadata={"seed": seed, "demand_factor": demand_factor, "generator": "ocean_routing.generate"},
        simulation={"carrier_accepts_extra_feu": accepts},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--weeks", type=int, default=6)
    parser.add_argument("--demand-factor", type=float, default=1.0)
    parser.add_argument("--out", type=Path, default=Path("data/scenario.json"))
    args = parser.parse_args()
    scenario = generate(args.seed, args.weeks, args.demand_factor)
    scenario.save(args.out)
    feu = sum(c.feu for c in scenario.containers)
    alloc = sum(s.allocation_feu for s in scenario.sailings)
    print(f"wrote {args.out}: {len(scenario.containers)} containers ({feu:g} FEU), "
          f"{len(scenario.sailings)} sailings, {alloc:g} FEU allocated")


if __name__ == "__main__":
    main()
