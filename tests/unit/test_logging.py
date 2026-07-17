import json
import logging

from trading_control_plane.logging import JsonFormatter


def test_formatter_emits_allowlisted_context_only() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=12,
        msg="transaction failed",
        args=(),
        exc_info=None,
    )
    record.event = "command_transaction_failed"
    record.command_type = "capability.disable.v1"
    record.secret = "must-not-leak"  # noqa: S105 - verifies secret-shaped fields are omitted

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "command_transaction_failed"
    assert payload["command_type"] == "capability.disable.v1"
    assert "secret" not in payload
