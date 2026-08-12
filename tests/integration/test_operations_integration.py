from __future__ import annotations

import base64
import json

from trading_control_plane.config import Settings
from trading_control_plane.database import REQUIRED_SCHEMA_REVISION, Database
from trading_control_plane.operations import build_diagnostic_report


def test_doctor_reads_schema_and_closed_gates_without_secrets(database: Database) -> None:
    secret = "doctor-database-session-secret-value"  # noqa: S105 - inert fixture
    key = base64.urlsafe_b64encode(b"doctor-integration-key-32-bytes!"[:32]).decode().rstrip("=")
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        session_signing_secret=secret,
        credential_encryption_key=key,
        _env_file=None,
    )

    report = build_diagnostic_report(settings, database=database)
    serialized = json.dumps(report, sort_keys=True)

    assert report["status"] == "READY"
    assert report["database"] == {
        "checked": True,
        "status": "READY",
        "error_code": None,
        "required_schema_revision": REQUIRED_SCHEMA_REVISION,
        "observed_schema_revision": REQUIRED_SCHEMA_REVISION,
    }
    assert set(report["dangerous_controls"]["database_gates"].values()) == {"DISABLED"}
    assert report["dangerous_controls"]["default_safe"] is True
    assert secret not in serialized
    assert key not in serialized
