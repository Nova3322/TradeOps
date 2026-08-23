from __future__ import annotations

from trading_control_plane.domain import Role
from trading_control_plane.rejections import reject
from trading_control_plane.request_context import current_api_client_context

TEAM_SETUP_ACTIONS = frozenset(
    {
        "team.view",
        "team.manage",
        "user.manage",
        "role.manage",
        "venue.view",
        "account.manage",
        "account.credentials.manage",
        "system.view",
        "view",
        "proposal.view",
        "operations.view",
        "results.view",
        "capital.view",
        "risk_policy.manage",
        "signal.view",
        "signal.manage",
        "opportunity.view",
        "notification.view",
        "notification.manage",
    }
)

API_CLIENT_HUMAN_ONLY_ACTIONS = frozenset(
    {
        "team.manage",
        "user.manage",
        "role.manage",
        "account.manage",
        "account.credentials.manage",
        "signal.manage",
        "notification.manage",
        "risk_policy.manage",
        "risk.restore.request",
        "risk.restore.review",
        "risk.restore.execute",
        "capital.fact.record",
        "capital.propose",
        "capital.submit",
        "capital.review",
        "capital.authorize",
        "capital.execute",
        "capital.reconcile",
        "capital.policy.manage",
        "capital.automation.evaluate",
        "authorization.issue",
        "sender.manage",
    }
)

API_CLIENT_ALLOWED_BUSINESS_ACTIONS = frozenset(
    {
        "view",
        "team.view",
        "opportunity.view",
        "proposal.view",
        "proposal.create",
        "proposal.submit",
        "proposal.review",
        "operations.view",
        "system.view",
        "venue.view",
        "results.view",
        "signal.view",
        "notification.view",
        "capital.view",
    }
)

ROLE_ACTIONS: dict[Role, frozenset[str]] = {
    Role.OBSERVER: frozenset(
        {
            "view",
            "opportunity.view",
            "proposal.view",
            "operations.view",
            "system.view",
            "venue.view",
            "results.view",
            "signal.view",
            "notification.view",
        }
    ),
    Role.PROPOSER: frozenset(
        {
            "view",
            "opportunity.view",
            "proposal.view",
            "proposal.create",
            "proposal.submit",
            "signal.view",
            "notification.view",
        }
    ),
    Role.REVIEWER: frozenset(
        {
            "view",
            "proposal.view",
            "proposal.review",
            "system.view",
            "risk.restore.review",
            "risk.restore.execute",
            "signal.view",
            "notification.view",
        }
    ),
    Role.OPERATOR: frozenset(
        {
            "view",
            "proposal.view",
            "operations.view",
            "system.view",
            "venue.view",
            "results.view",
            "risk.decide",
            "authorization.issue",
            "order.prepare",
            "venue.record",
            "reconcile",
            "sender.manage",
            "risk.tighten",
            "risk.restore.request",
            "signal.view",
            "notification.view",
        }
    ),
    Role.TREASURY_ADMIN: frozenset(
        {
            "capital.view",
            "capital.fact.record",
            "capital.propose",
            "capital.submit",
            "capital.review",
            "capital.authorize",
            "capital.execute",
            "capital.reconcile",
            "capital.policy.manage",
            "capital.automation.evaluate",
            "notification.view",
        }
    ),
    Role.SYSTEM_ADMIN: frozenset({"*"}),
}


def require_human_web_session(detail: str) -> None:
    if current_api_client_context() is not None:
        reject("HUMAN_WEB_CONFIRMATION_REQUIRED", detail)
