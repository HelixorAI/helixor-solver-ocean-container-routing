"""Write pack/ui/ocean.{problem,solution}.html from ui/src."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocean_routing.surfaces import write  # noqa: E402

if __name__ == "__main__":
    for path in write():
        print(path)
