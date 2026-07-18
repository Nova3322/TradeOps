import json
import logging
import sys

from trading_control_plane.logging import JsonFormatter, configure_logging


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


def test_formatter_reports_exception_type_without_traceback_payload() -> None:
    try:
        raise ValueError("sensitive detail")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=30,
        msg="safe failure",
        args=(),
        exc_info=exc_info,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["exception_type"] == "ValueError"
    assert "sensitive detail" not in payload.values()


def test_configure_logging_sets_safe_root_and_quiets_third_parties() -> None:
    configure_logging("INFO")

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
