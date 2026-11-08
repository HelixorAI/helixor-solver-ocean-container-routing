"""Build the pack's Solver UX surfaces from ui/src into pack/ui.

Each surface is one self-contained HTML page (inline script and style, no
network) that the Solver UX hosts in a sandboxed iframe and feeds the
problem, the solution and the manifest by postMessage; see the protocol at the
top of ui/src/surface.js. The built pages are committed under pack/ui because
pack versions are immutable: `pack.build` refuses to package stale ones.

    python scripts/build_surfaces.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ui/src"
OUT = ROOT / "pack/ui"
ROLES = {"problem": "ocean.problem.html", "solution": "ocean.solution.html"}

# The host's sandbox already withholds the app origin and its storage; this
# policy also keeps the page off the network whatever host renders it.
CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:"

SURFACE_CSS = """
html{overflow-y:hidden}body{background:transparent}main.surface{max-width:none;margin:0;padding:0}
main.surface section{margin:0 0 12px}main.surface section:last-child{margin-bottom:0}
.err{color:var(--critical);margin:0;padding:12px}
"""


def render(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown surface role {role!r}")
    css = (SRC / "surface.css").read_text() + SURFACE_CSS
    js = f"const ROLE = {json.dumps(role)};\n" + (SRC / "surface.js").read_text()
    if "</script" in js.lower() or "</style" in css.lower():
        raise ValueError("surface script and style must not contain their own closing tag")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{CSP}\">"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Ocean Routing {role.title()}</title><style>{css}</style></head>"
            f"<body><main class='surface' id='app' aria-live='polite'></main>"
            f"<div id='tip' role='tooltip'></div><script>{js}</script></body></html>\n")


def built() -> Dict[str, str]:
    """Pack-relative path -> page, as the sources produce them now."""
    return {f"ui/{name}": render(role) for role, name in ROLES.items()}


def stale() -> list[str]:
    return [rel for rel, page in built().items()
            if not (ROOT / "pack" / rel).is_file() or (ROOT / "pack" / rel).read_text() != page]


def write() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = []
    for rel, page in built().items():
        path = ROOT / "pack" / rel
        path.write_text(page)
        paths.append(path)
    return paths
