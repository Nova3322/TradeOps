from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_control_plane.authorization import (
    KNOWN_ACTIONS,
    ROLE_ACTIONS,
    AuthorizationRequest,
)
from trading_control_plane.commands import CommandChannel


def test_system_admin_does_not_inherit_reviewer_or_treasury_actions() -> None:
    actions = ROLE_ACTIONS["SYSTEM_ADMIN"]

    assert "ACT-PROPOSAL-APPROVE" not in actions
    assert "ACT-TRANSFER-APPROVE" not in actions
    assert "ACT-LABEL-REVOKE" in actions


def test_known_actions_are_derived_from_explicit_role_policy() -> None:
    assert "ACT-PROPOSAL-APPROVE" in KNOWN_ACTIONS
    assert "ACT-DIRECT-ORDER-SEND" not in KNOWN_ACTIONS


def test_authorization_request_requires_aware_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        AuthorizationRequest(
            principal_id=uuid4(),
            action_id="ACT-PROPOSAL-VIEW",
            object_type="ProposalVersion",
            object_id="proposal-1:v1",
            object_version=1,
            organization_id="org-1",
            channel=CommandChannel.WEB,
            requested_at=datetime.now(),
        )

    request = AuthorizationRequest(
        principal_id=uuid4(),
        action_id="ACT-PROPOSAL-VIEW",
        object_type="ProposalVersion",
        object_id="proposal-1:v1",
        object_version=1,
        organization_id="org-1",
        channel=CommandChannel.WEB,
        requested_at=datetime.now(UTC),
    )
    assert request.online is True
