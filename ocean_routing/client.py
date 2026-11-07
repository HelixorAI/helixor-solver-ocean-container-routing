"""Thin client for the hosted Helixor Solver REST API.

Only documented endpoints are used: solver-pack validation/upload, manifest
lookup and solve. Errors are raised with the server's typed body intact; this
client never retries with different inputs or substitutes a default answer.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

PROBLEM_TYPE = "ocean_container_routing"


class SolverError(RuntimeError):
    def __init__(self, operation: str, status: int, body: Any):
        self.operation = operation
        self.status = status
        self.body = body
        super().__init__(f"{operation} failed ({status}): {body}")


class SolverClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    @classmethod
    def from_env(cls) -> "SolverClient":
        missing = [k for k in ("SOLVER_URL", "SOLVER_TOKEN") if not os.environ.get(k)]
        if missing:
            raise SystemExit(
                f"Missing {', '.join(missing)}. Copy .env.example to .env, fill in the Solver API origin "
                "and a token for your Helixor account (its organization needs a Helixor Solver license), and load it (set -a && . ./.env && set +a)."
            )
        return cls(os.environ["SOLVER_URL"], os.environ["SOLVER_TOKEN"])

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SolverClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _call(self, operation: str, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        response = self._http.request(method, f"{self.base_url}{path}", **kwargs)
        if not response.is_success:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            raise SolverError(operation, response.status_code, body)
        return response.json()

    # -- solver packs ------------------------------------------------------

    def validate_pack(self, archive: Path) -> Dict[str, Any]:
        data = archive.read_bytes()
        return self._call(
            "validate pack", "POST", "/api/v1/solver-packs/validations",
            content=data, headers={"Content-Type": "application/zip"},
            params={"expected_sha256": hashlib.sha256(data).hexdigest()},
        )

    def publish_pack(self, archive: Path, *, activate: bool = True) -> Dict[str, Any]:
        data = archive.read_bytes()
        return self._call(
            "publish pack", "POST", "/api/v1/solver-packs",
            content=data, headers={"Content-Type": "application/zip"},
            params={"activate": str(activate).lower(), "expected_sha256": hashlib.sha256(data).hexdigest()},
        )

    # -- runtime -----------------------------------------------------------

    def manifest(self, problem_type: str = PROBLEM_TYPE) -> Dict[str, Any]:
        return self._call("manifest lookup", "GET", f"/api/v1/manifests/{problem_type}")

    def solve(
        self,
        problem: Dict[str, Any],
        *,
        problem_type: str = PROBLEM_TYPE,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body = {
            "problem_type": problem_type,
            "problem": problem,
            "solver": {"mode": "local"},
            "options": options or {},
        }
        return self._call("solve", "POST", f"/api/v1/solve/{problem_type}", json=body)
