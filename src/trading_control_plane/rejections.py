from __future__ import annotations

from typing import NoReturn

from trading_control_plane.domain import DomainRejected


def reject(code: str, detail: str) -> NoReturn:
    raise DomainRejected(code, detail)
