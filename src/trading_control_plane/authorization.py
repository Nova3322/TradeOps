from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.iam_models import (
    ActionAssurance,
    AuthorizationDecision,
    ExplicitDeny,
    IdentityPrincipal,
    PermissionScope,
    RoleAssignment,
)

POLICY_VERSION = "IAM-POLICY-2026-07-18"


class RiskTier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskEngineStatus(StrEnum):
    PASSED = "PASSED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class SystemRiskState(StrEnum):
    NORMAL = "NORMAL"
    NO_NEW_POSITION = "NO_NEW_POSITION"
    NO_PYRAMID = "NO_PYRAMID"
    REDUCE_ONLY = "REDUCE_ONLY"
    KILL_SWITCH = "KILL_SWITCH"
    UNKNOWN = "UNKNOWN"


class AuthorizationResult(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "OBSERVER": frozenset(
        {
            "ACT-CANDIDATE-VIEW",
            "ACT-PROPOSAL-VIEW",
            "ACT-POSITION-VIEW",
            "ACT-FUNDING-VIEW",
            "ACT-AUDIT-VIEW",
        }
    ),
    "PROPOSER": frozenset(
        {
            "ACT-CANDIDATE-VIEW",
            "ACT-MANUAL-DRAFT-CREATE",
            "ACT-MANUAL-DRAFT-EDIT",
            "ACT-MANUAL-SUBMIT",
            "ACT-PROPOSAL-VIEW",
            "ACT-PROPOSAL-CANCEL-DRAFT",
            "ACT-PROPOSAL-CANCEL-FROZEN",
            "ACT-POSITION-VIEW",
            "ACT-FUNDING-VIEW",
            "ACT-AUDIT-VIEW",
        }
    ),
    "REVIEWER": frozenset(
        {
            "ACT-CANDIDATE-VIEW",
            "ACT-PROPOSAL-VIEW",
            "ACT-PROPOSAL-APPROVE",
            "ACT-PROPOSAL-REJECT",
            "ACT-PROPOSAL-RETURN",
            "ACT-PROPOSAL-CANCEL-FROZEN",
            "ACT-POSITION-VIEW",
            "ACT-FUNDING-VIEW",
            "ACT-AUDIT-VIEW",
        }
    ),
    "OPERATOR": frozenset(
        {
            "ACT-CANDIDATE-VIEW",
            "ACT-PROPOSAL-VIEW",
            "ACT-PROPOSAL-CANCEL-FROZEN",
            "ACT-POSITION-VIEW",
            "ACT-AUTO-ADD-DISABLE",
            "ACT-NEW-ENTRY-PAUSE",
            "ACT-EMERGENCY-REDUCE",
            "ACT-POSITION-EXIT",
            "ACT-STOP-TIGHTEN",
            "ACT-INCIDENT-ACK",
            "ACT-RECONCILIATION-RUN",
            "ACT-FUNDING-VIEW",
            "ACT-AUDIT-VIEW",
            "ACT-AUDIT-EXPORT",
        }
    ),
    "RISK_ADMIN": frozenset(
        {
            "ACT-CANDIDATE-VIEW",
            "ACT-PROPOSAL-VIEW",
            "ACT-POSITION-VIEW",
            "ACT-AUTO-ADD-DISABLE",
            "ACT-NEW-ENTRY-PAUSE",
            "ACT-EMERGENCY-REDUCE",
            "ACT-POSITION-EXIT",
            "ACT-STOP-TIGHTEN",
            "ACT-RISK-POLICY-PROPOSE",
            "ACT-RISK-POLICY-APPROVE",
            "ACT-RISK-STATE-RESTORE",
            "ACT-INCIDENT-ACK",
            "ACT-RECONCILIATION-RUN",
            "ACT-FUNDING-VIEW",
            "ACT-AUDIT-VIEW",
            "ACT-AUDIT-EXPORT",
        }
    ),
    "TREASURY_ADMIN": frozenset(
        {
            "ACT-INCIDENT-ACK",
            "ACT-RECONCILIATION-RUN",
            "ACT-FUNDING-VIEW",
            "ACT-TRANSFER-PROPOSE",
            "ACT-TRANSFER-APPROVE",
            "ACT-TRANSFER-EXECUTE",
            "ACT-AUDIT-VIEW",
            "ACT-AUDIT-EXPORT",
        }
    ),
    "SYSTEM_ADMIN": frozenset(
        {
            "ACT-INCIDENT-ACK",
            "ACT-USER-VIEW",
            "ACT-LABEL-GRANT",
            "ACT-LABEL-REVOKE",
            "ACT-SESSION-REVOKE",
            "ACT-MFA-RESET",
            "ACT-AUDIT-VIEW",
            "ACT-AUDIT-EXPORT",
        }
    ),
}

KNOWN_ACTIONS = frozenset().union(*ROLE_ACTIONS.values())
REVIEW_ACTIONS = frozenset(
    {
        "ACT-PROPOSAL-APPROVE",
        "ACT-PROPOSAL-REJECT",
        "ACT-PROPOSAL-RETURN",
    }
)
SENSITIVE_ACTIONS = frozenset(
    {
        "ACT-PROPOSAL-APPROVE",
        "ACT-EMERGENCY-REDUCE",
        "ACT-POSITION-EXIT",
        "ACT-RISK-POLICY-APPROVE",
        "ACT-RISK-STATE-RESTORE",
        "ACT-TRANSFER-PROPOSE",
        "ACT-TRANSFER-APPROVE",
        "ACT-TRANSFER-EXECUTE",
        "ACT-LABEL-GRANT",
        "ACT-MFA-RESET",
        "ACT-AUDIT-EXPORT",
    }
)


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: UUID = Field(default_factory=uuid4)
    principal_id: UUID
    action_id: str = Field(min_length=3, max_length=160)
    object_type: str = Field(min_length=1, max_length=120)
    object_id: str = Field(min_length=1, max_length=255)
    object_version: int = Field(ge=1)
    organization_id: str = Field(min_length=1, max_length=120)
    account_id: str | None = Field(default=None, max_length=160)
    venue: str | None = Field(default=None, max_length=80)
    sector: str | None = Field(default=None, max_length=80)
    risk_tier: RiskTier | None = None
    channel: CommandChannel
    online: bool = True
    device_ref: str | None = Field(default=None, max_length=255)
    assurance_id: UUID | None = None
    resource_creator_id: UUID | None = None
    resource_creator_service_principal: str | None = Field(default=None, max_length=255)
    resource_status: str | None = Field(default=None, max_length=80)
    resource_valid_until: datetime | None = None
    risk_engine_status: RiskEngineStatus = RiskEngineStatus.UNKNOWN
    system_risk_state: SystemRiskState = SystemRiskState.UNKNOWN
    requested_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> AuthorizationRequest:
        if self.requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        if self.resource_valid_until is not None and self.resource_valid_until.tzinfo is None:
            raise ValueError("resource_valid_until must be timezone-aware")
        return self


class AuthorizationEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    result: AuthorizationResult
    reason_code: str
    required_quorum: int | None = None
    matched_assignment_ids: tuple[UUID, ...] = ()
    matched_deny_ids: tuple[UUID, ...] = ()


class AuthorizationEvaluator:
    def evaluate(
        self,
        session: Session,
        envelope: CommandEnvelope,
        request: AuthorizationRequest,
    ) -> AuthorizationEvaluation:
        now = datetime.now(UTC)
        if envelope.actor_id != str(request.principal_id):
            return self._persist(
                session, envelope, request, now, AuthorizationResult.DENY, "ACTOR_MISMATCH"
            )

        principal = session.get(IdentityPrincipal, request.principal_id)
        if (
            principal is None
            or principal.status != "ACTIVE"
            or principal.organization_id != request.organization_id
        ):
            return self._persist(
                session,
                envelope,
                request,
                now,
                AuthorizationResult.DENY,
                "PRINCIPAL_INACTIVE_OR_UNKNOWN",
            )
        if principal.principal_type != "HUMAN":
            return self._persist(
                session,
                envelope,
                request,
                now,
                AuthorizationResult.DENY,
                "HUMAN_ACTION_REQUIRED",
            )
        if request.action_id not in KNOWN_ACTIONS:
            return self._persist(
                session, envelope, request, now, AuthorizationResult.DENY, "ACTION_UNKNOWN"
            )
        if not request.online:
            return self._persist(
                session, envelope, request, now, AuthorizationResult.DENY, "OFFLINE_DENIED"
            )

        assignments = list(
            session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.principal_id == request.principal_id,
                    RoleAssignment.organization_id == request.organization_id,
                    RoleAssignment.revoked_at.is_(None),
                    RoleAssignment.valid_from <= now,
                    or_(RoleAssignment.valid_until.is_(None), RoleAssignment.valid_until > now),
                )
            ).scalars()
        )
        action_assignments = [
            assignment
            for assignment in assignments
            if request.action_id in ROLE_ACTIONS.get(assignment.role_key, frozenset())
        ]
        if not action_assignments:
            return self._persist(
                session, envelope, request, now, AuthorizationResult.DENY, "ROLE_NOT_ALLOWED"
            )

        assignment_ids = [assignment.assignment_id for assignment in action_assignments]
        scopes = list(
            session.execute(
                select(PermissionScope).where(PermissionScope.assignment_id.in_(assignment_ids))
            ).scalars()
        )
        matching_assignment_ids = tuple(
            sorted(
                {scope.assignment_id for scope in scopes if self._scope_matches(scope, request)},
                key=str,
            )
        )
        if not matching_assignment_ids:
            return self._persist(
                session, envelope, request, now, AuthorizationResult.DENY, "SCOPE_MISMATCH"
            )

        active_denies = list(
            session.execute(
                select(ExplicitDeny).where(
                    ExplicitDeny.principal_id == request.principal_id,
                    ExplicitDeny.revoked_at.is_(None),
                    ExplicitDeny.valid_from <= now,
                    or_(ExplicitDeny.valid_until.is_(None), ExplicitDeny.valid_until > now),
                )
            ).scalars()
        )
        matching_deny_ids = tuple(
            sorted(
                (deny.deny_id for deny in active_denies if self._deny_matches(deny, request)),
                key=str,
            )
        )
        if matching_deny_ids:
            return self._persist(
                session,
                envelope,
                request,
                now,
                AuthorizationResult.DENY,
                "EXPLICIT_DENY",
                matching_assignment_ids,
                matching_deny_ids,
            )

        is_self_review = (
            request.action_id in REVIEW_ACTIONS
            and request.resource_creator_id == request.principal_id
        )
        if (
            request.action_id in REVIEW_ACTIONS
            and request.resource_creator_id is None
            and request.resource_creator_service_principal is None
        ):
            return self._persist(
                session,
                envelope,
                request,
                now,
                AuthorizationResult.DENY,
                "RESOURCE_CREATOR_REQUIRED",
                matching_assignment_ids,
            )
        if is_self_review:
            return self._persist(
                session,
                envelope,
                request,
                now,
                AuthorizationResult.DENY,
                "SELF_REVIEW_FORBIDDEN",
                matching_assignment_ids,
                is_self_review=True,
            )

        quorum = self._required_quorum(request)
        proposal_gate_error = self._proposal_gate_error(request, now)
        if proposal_gate_error is not None:
            return self._persist(
                session,
                envelope,
                request,
                now,
                AuthorizationResult.DENY,
                proposal_gate_error,
                matching_assignment_ids,
                required_quorum=quorum,
            )

        assurance_error = self._consume_assurance(session, envelope, request, now)
        if assurance_error is not None:
            return self._persist(
                session,
                envelope,
                request,
                now,
                AuthorizationResult.DENY,
                assurance_error,
                matching_assignment_ids,
                required_quorum=quorum,
            )

        return self._persist(
            session,
            envelope,
            request,
            now,
            AuthorizationResult.ALLOW,
            "ALLOWED",
            matching_assignment_ids,
            required_quorum=quorum,
        )

    @staticmethod
    def _scope_matches(scope: PermissionScope, request: AuthorizationRequest) -> bool:
        return (
            scope.organization_id == request.organization_id
            and (scope.account_id is None or scope.account_id == request.account_id)
            and (scope.venue is None or scope.venue == request.venue)
            and (scope.sector is None or scope.sector == request.sector)
            and (
                scope.risk_tier is None
                or (request.risk_tier is not None and scope.risk_tier == request.risk_tier.value)
            )
            and (scope.action_id is None or scope.action_id == request.action_id)
            and (scope.channel is None or scope.channel == request.channel.value)
        )

    @staticmethod
    def _deny_matches(deny: ExplicitDeny, request: AuthorizationRequest) -> bool:
        return (
            deny.organization_id == request.organization_id
            and deny.action_id == request.action_id
            and (deny.account_id is None or deny.account_id == request.account_id)
            and (deny.venue is None or deny.venue == request.venue)
            and (deny.sector is None or deny.sector == request.sector)
            and (
                deny.risk_tier is None
                or (request.risk_tier is not None and deny.risk_tier == request.risk_tier.value)
            )
            and (deny.channel is None or deny.channel == request.channel.value)
        )

    @staticmethod
    def _required_quorum(request: AuthorizationRequest) -> int | None:
        if request.action_id != "ACT-PROPOSAL-APPROVE" or request.risk_tier is None:
            return None
        return 2 if request.risk_tier is RiskTier.HIGH else 1

    @staticmethod
    def _proposal_gate_error(request: AuthorizationRequest, now: datetime) -> str | None:
        if request.action_id != "ACT-PROPOSAL-APPROVE":
            return None
        if request.risk_tier is None:
            return "RISK_TIER_REQUIRED"
        if request.resource_status != "FROZEN":
            return "RESOURCE_NOT_APPROVABLE"
        if request.resource_valid_until is None or request.resource_valid_until <= now:
            return "RESOURCE_EXPIRED"
        if request.risk_engine_status is not RiskEngineStatus.PASSED:
            return "RISK_ENGINE_NOT_PASSED"
        if request.system_risk_state is not SystemRiskState.NORMAL:
            return "SYSTEM_RISK_STATE_DENY"
        return None

    @staticmethod
    def _consume_assurance(
        session: Session,
        envelope: CommandEnvelope,
        request: AuthorizationRequest,
        now: datetime,
    ) -> str | None:
        if request.action_id not in SENSITIVE_ACTIONS:
            return None
        if request.assurance_id is None:
            return "MFA_REQUIRED"
        if request.device_ref is None:
            return "DEVICE_ASSURANCE_REQUIRED"
        assurance = session.execute(
            select(ActionAssurance)
            .where(ActionAssurance.assurance_id == request.assurance_id)
            .with_for_update()
        ).scalar_one_or_none()
        if assurance is None:
            return "MFA_REQUIRED"
        if assurance.status != "VERIFIED":
            return "MFA_REVOKED"
        if (
            assurance.assurance_method != "PASSKEY_WEBAUTHN"
            or assurance.assurance_level != "ACTION_STEP_UP"
        ):
            return "MFA_ASSURANCE_INSUFFICIENT"
        if assurance.used_at is not None:
            return "MFA_ALREADY_USED"
        if assurance.issued_at > now or assurance.expires_at <= now:
            return "MFA_EXPIRED"
        if (
            assurance.principal_id != request.principal_id
            or assurance.auth_context_ref != envelope.auth_context_ref
            or assurance.device_ref != request.device_ref
            or assurance.channel != request.channel.value
            or assurance.action_id != request.action_id
            or assurance.object_type != request.object_type
            or assurance.object_id != request.object_id
            or assurance.object_version != request.object_version
        ):
            return "MFA_BINDING_MISMATCH"
        assurance.used_at = now
        return None

    @staticmethod
    def _persist(
        session: Session,
        envelope: CommandEnvelope,
        request: AuthorizationRequest,
        now: datetime,
        result: AuthorizationResult,
        reason_code: str,
        matched_assignment_ids: tuple[UUID, ...] = (),
        matched_deny_ids: tuple[UUID, ...] = (),
        *,
        is_self_review: bool = False,
        required_quorum: int | None = None,
    ) -> AuthorizationEvaluation:
        decision_id = uuid4()
        context = request.model_dump(mode="json")
        session.add(
            AuthorizationDecision(
                decision_id=decision_id,
                request_id=request.request_id,
                command_id=envelope.command_id,
                principal_id=request.principal_id,
                action_id=request.action_id,
                object_type=request.object_type,
                object_id=request.object_id,
                object_version=request.object_version,
                organization_id=request.organization_id,
                account_id=request.account_id,
                venue=request.venue,
                sector=request.sector,
                risk_tier=request.risk_tier.value if request.risk_tier else None,
                channel=request.channel.value,
                device_ref=request.device_ref,
                auth_context_ref=envelope.auth_context_ref,
                result=result.value,
                reason_code=reason_code,
                is_self_review=is_self_review,
                required_quorum=required_quorum,
                matched_assignment_ids=[str(value) for value in matched_assignment_ids],
                matched_deny_ids=[str(value) for value in matched_deny_ids],
                policy_version=POLICY_VERSION,
                request_hash=hash_json(context),
                request_context=context,
                requested_at=request.requested_at,
                decided_at=now,
            )
        )
        return AuthorizationEvaluation(
            decision_id=decision_id,
            result=result,
            reason_code=reason_code,
            required_quorum=required_quorum,
            matched_assignment_ids=matched_assignment_ids,
            matched_deny_ids=matched_deny_ids,
        )


class AuthorizationEvaluationService:
    """Internal testable command adapter; it does not execute the requested action."""

    command_type = "authorization.evaluate.v1"

    def __init__(self, evaluator: AuthorizationEvaluator | None = None) -> None:
        self._evaluator = evaluator or AuthorizationEvaluator()

    def evaluate(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        request = AuthorizationRequest.model_validate(envelope.payload)
        if envelope.object_type != request.object_type or envelope.object_id != request.object_id:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "command object binding differs")
        if envelope.expected_version != request.object_version:
            raise CommandRejected("VERSION_BINDING_MISMATCH", "command version binding differs")

        evaluation = self._evaluator.evaluate(session, envelope, request)
        allowed = evaluation.result is AuthorizationResult.ALLOW
        return CommandOutcome(
            status=CommandStatus.COMPLETED if allowed else CommandStatus.REJECTED,
            object_type=request.object_type,
            object_id=request.object_id,
            object_version=request.object_version,
            error_code=None if allowed else evaluation.reason_code,
            data={
                "decision_id": str(evaluation.decision_id),
                "authorization_result": evaluation.result.value,
                "reason_code": evaluation.reason_code,
                "required_quorum": evaluation.required_quorum,
            },
            events=(
                DomainEvent(
                    event_type="AuthorizationAllowed" if allowed else "AuthorizationDenied",
                    aggregate_type=request.object_type,
                    aggregate_id=request.object_id,
                    payload={
                        "decision_id": str(evaluation.decision_id),
                        "action_id": request.action_id,
                        "result": evaluation.result.value,
                        "reason_code": evaluation.reason_code,
                        "required_quorum": evaluation.required_quorum,
                        "policy_version": POLICY_VERSION,
                    },
                ),
            ),
        )
