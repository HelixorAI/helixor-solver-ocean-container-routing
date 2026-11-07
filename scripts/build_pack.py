"""Build dist/<pack>-<version>.zip and print its sha256."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocean_routing.pack import build  # noqa: E402

if __name__ == "__main__":
    path = build()
    print(f"{path}  sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
