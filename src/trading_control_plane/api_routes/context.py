from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI


@dataclass(frozen=True, slots=True)
class ApiRouteContext:
    """Per-application dependency references; no state is copied or independently owned."""

    app: FastAPI
    dependencies: dict[str, Any]

    def require(self, name: str) -> Any:
        try:
            return self.dependencies[name]
        except KeyError as exc:
            raise RuntimeError(f"API route dependency is missing: {name}") from exc
