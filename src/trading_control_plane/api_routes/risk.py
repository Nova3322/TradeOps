from __future__ import annotations

from trading_control_plane.api_core import (
    UUID,
    Any,
    DomainRejected,
    ReviewDecision,
    RiskControlChangeCreateRequest,
    RiskControlChangeExecuteRequest,
    RiskControlChangeReviewRequest,
    RiskControlDirectRestoreRequest,
    RiskPolicyConfigureRequest,
    SessionIdentity,
    _now,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


def register_risk_routes(context: ApiRouteContext) -> None:
    """Register risk routes against one application dependency context."""

    app = context.app
    dependencies = context.risk
    common = dependencies.common
    configured_risk_scopes = dependencies.configured_risk_scopes
    identity_dependency = common.identity
    require_capability = common.require_capability
    service = common.service
    token_service = dependencies.token_service

    @app.get("/api/risk-controls")
    def risk_controls(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "system.view")
        return service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(identity.user_id),
            require_live_scope=True,
            now=_now(),
        )

    @app.put("/api/risk-controls/policy")
    def configure_risk_policy(
        payload: RiskPolicyConfigureRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        policy_id = service().configure_risk_policy(
            actor_id=identity.user_id,
            version=payload.version,
            max_total_risk=payload.max_total_risk,
            max_account_risk=payload.max_account_risk,
            max_single_loss=payload.max_single_loss,
            max_consecutive_losses=payload.max_consecutive_losses,
            loss_cooldown=timedelta(seconds=payload.loss_cooldown_seconds),
            max_fact_age=timedelta(seconds=payload.max_fact_age_seconds),
            expected_revision=payload.expected_revision,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {"policy_id": str(policy_id)}

    @app.post("/api/risk-controls/restore-direct")
    def direct_risk_control_restore(
        payload: RiskControlDirectRestoreRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        current = service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(identity.user_id),
            require_live_scope=True,
            now=now,
        )
        policy_id = UUID(str(current["policy"]["policy_id"]))
        policy_revision = int(current["policy"]["revision"])
        token_service.verify_action_grant(
            payload.action_grant,
            user_id=identity.user_id,
            action="risk.restore.direct",
            object_id=policy_id,
            object_version=policy_revision,
            now=now,
        )
        restored_policy_id = service().direct_restore_risk_controls(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            configured_scopes=configured_risk_scopes(identity.user_id),
            require_live_scope=True,
            now=now,
        )
        return {"policy_id": str(restored_policy_id)}

    @app.post("/api/risk-controls/restores")
    @app.post("/api/risk-controls/changes")
    def create_risk_control_restore(
        payload: RiskControlChangeCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        request_id = service().create_risk_control_change_request(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            restore_auto_add=payload.restore_auto_add,
            change_type=payload.change_type,
            requested_policy=payload.requested_policy,
            configured_scopes=configured_risk_scopes(identity.user_id),
            require_live_scope=True,
            now=_now(),
        )
        return service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(identity.user_id),
            require_live_scope=True,
            now=_now(),
        ) | {"request_id": str(request_id)}

    @app.post("/api/risk-controls/restores/{request_id}/reviews")
    @app.post("/api/risk-controls/changes/{request_id}/reviews")
    def review_risk_control_restore(
        request_id: UUID,
        payload: RiskControlChangeReviewRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        if payload.decision == "APPROVE":
            if payload.action_grant is None:
                raise DomainRejected(
                    "ACTION_GRANT_REQUIRED",
                    "risk restoration approval requires action-level step-up",
                )
            token_service.verify_action_grant(
                payload.action_grant,
                user_id=identity.user_id,
                action="risk.restore.review",
                object_id=request_id,
                object_version=payload.expected_version,
                now=now,
            )
        result = service().review_risk_control_change_request(
            request_id,
            identity.user_id,
            ReviewDecision(payload.decision),
            payload.reason,
            payload.expected_version,
            payload.idempotency_key,
            now=now,
        )
        return {"request_id": str(request_id), "status": result.value}

    @app.post("/api/risk-controls/restores/{request_id}/execute")
    @app.post("/api/risk-controls/changes/{request_id}/execute")
    def execute_risk_control_restore(
        request_id: UUID,
        payload: RiskControlChangeExecuteRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        token_service.verify_action_grant(
            payload.action_grant,
            user_id=identity.user_id,
            action="risk.restore.execute",
            object_id=request_id,
            object_version=payload.expected_version,
            now=now,
        )
        policy_id = service().execute_risk_control_change_request(
            request_id,
            identity.user_id,
            payload.expected_version,
            payload.idempotency_key,
            configured_risk_scopes(identity.user_id),
            require_live_scope=True,
            now=now,
        )
        return {"request_id": str(request_id), "policy_id": str(policy_id)}
