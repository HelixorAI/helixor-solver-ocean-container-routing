"""Build the config-only solver pack archive from pack/ into dist/."""
from __future__ import annotations

import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "pack"
FILES = [
    "pack.yaml",
    "manifests/ocean_container_routing.yaml",
    "datasets/two_lane_week.demo.json",
]


def build(out_dir: Path = ROOT / "dist") -> Path:
    descriptor = yaml.safe_load((PACK / "pack.yaml").read_text())
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{descriptor['id']}-{descriptor['version']}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in FILES:
            src = PACK / rel
            if not src.is_file():
                raise SystemExit(f"pack file missing: {src}")
            info = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))   # reproducible bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, src.read_bytes())
    return archive
