"""Write pack/datasets/two_lane_week.demo.json from the generator (see ocean_routing/demo.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocean_routing.demo import write  # noqa: E402

if __name__ == "__main__":
    print(write())
