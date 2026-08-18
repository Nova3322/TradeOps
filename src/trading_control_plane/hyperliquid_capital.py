"""Compatibility import for the isolated Hyperliquid capital adapter.

The implementation lives under ``trading_control_plane.adapters`` so venue
SDK, signing, and native HTTP details do not live in the control-plane layer.
"""

from trading_control_plane.adapters.hyperliquid_capital import *  # noqa: F403
