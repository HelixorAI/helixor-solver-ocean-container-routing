"""Render out/continuous/summary.json as the README's markdown tables."""
import json
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "out/continuous/summary.json")
for cycle in json.loads(path.read_text()):
    pins = cycle["pins"]
    print(f"**Cycle {cycle['cycle']}, day {cycle['today']}:** {cycle['label']}. "
          f"Pins: {pins.get('frozen', 0)} frozen, {pins.get('prefer', 0)} prefer, "
          f"{pins.get('forbid', 0)} forbid, {pins['free']} free.\n")
    space = cycle.get("space")
    if space:
        print(f"Carriers answered: {space['confirmed_feu']:g} FEU confirmed, {space['rejected_feu']:g} FEU rejected. "
              f"After the repair: requested {space['requested_feu']:g} FEU, withdrew {space['withdrawn_feu']:g}, "
              f"released {space['released_feu']:g}, holding {space['held_idle_feu']:g} FEU idle for later.\n")
    print("| Engine | Penalty | Late (days) | Allocation used | Spot FEU | Unacknowledged FEU | Moved | Time |")
    print("|---|---|---|---|---|---|---|---|")
    for r in cycle["rows"]:
        moved = "" if r["moved"] is None else r["moved"]
        secs = "" if r["seconds"] is None else f"{r['seconds']} s"
        print(f"| {r['solver']} | {r['penalty']:,.0f} | {r['late']} ({r['days_late']}) | "
              f"{r['utilization_pct']}% | {r['spot_feu']:g} | {r.get('unconfirmed_feu', 0):g} | {moved} | {secs} |")
    print()
