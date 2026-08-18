"""Compatibility import for the isolated Binance capital adapter.

New control-plane code imports the adapter boundary through ``api_core``.  The
old module path remains temporarily for downstream callers and tests while the
capital workflow equivalence gate is completed.
"""

from trading_control_plane.adapters.binance_capital import *  # noqa: F403
