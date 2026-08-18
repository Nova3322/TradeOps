"""Isolated exchange capability adapters.

Only modules in this package may import CCXT/CCXT Pro or venue SDKs.  The
control-plane domain consumes the stable contracts exported here.
"""

from trading_control_plane.adapters.capital import (
    CallableCapitalBackend,
    CapitalAdapter,
    CapitalCredential,
    CapitalOperation,
    CapitalScope,
    CcxtUnifiedCapitalBackend,
    build_ccxt_capital_backend,
)
from trading_control_plane.adapters.facts import (
    CcxtProFactAdapter,
    ExchangeFactEvent,
    ExchangeFactSnapshot,
    FactAdapterRegistry,
    FactAdapterScope,
    FactStreamSupervisor,
)

__all__ = [
    "CallableCapitalBackend",
    "CapitalAdapter",
    "CapitalCredential",
    "CapitalOperation",
    "CapitalScope",
    "CcxtProFactAdapter",
    "CcxtUnifiedCapitalBackend",
    "ExchangeFactEvent",
    "ExchangeFactSnapshot",
    "FactAdapterRegistry",
    "FactAdapterScope",
    "FactStreamSupervisor",
    "build_ccxt_capital_backend",
]
