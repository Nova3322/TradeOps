from prometheus_client import Counter, Gauge, Histogram

COMMAND_RESULTS = Counter(
    "trading_command_results_total",
    "Durable command results by versioned command type and bounded result.",
    labelnames=("command_type", "result"),
)

COMMAND_DURATION = Histogram(
    "trading_command_duration_seconds",
    "Durable command transaction duration by versioned command type.",
    labelnames=("command_type",),
)

DATABASE_READY = Gauge(
    "trading_database_ready",
    "1 when the durable database and mandatory control gates are ready, otherwise 0.",
)

RISK_DECISIONS = Counter(
    "trading_risk_decisions_total",
    "Immutable proposal-precheck risk decisions by bounded result and primary reason.",
    labelnames=("result", "primary_reason"),
)

RISK_DECISION_DURATION = Histogram(
    "trading_risk_decision_duration_seconds",
    "Deterministic proposal-precheck evaluation and persistence duration.",
)

RISK_STALE_INPUTS = Counter(
    "trading_risk_stale_inputs_total",
    "Proposal prechecks that fail closed because at least one fact is stale.",
)

SYSTEM_RISK_STATE_TRANSITIONS = Counter(
    "trading_system_risk_state_transitions_total",
    "Automatic monotonic risk-state tightening transitions.",
    labelnames=("from_state", "to_state"),
)

AUTHORIZATION_ISSUANCE = Counter(
    "trading_authorization_issuance_total",
    "Successful TradingAuthorization issuance by bounded result and system risk state.",
    labelnames=("result", "system_risk_state"),
)

AUTHORIZATION_TIGHTENING = Counter(
    "trading_authorization_tightening_total",
    "Risk-decreasing authorization lifecycle commands by action and system risk state.",
    labelnames=("action", "system_risk_state"),
)

EXECUTION_RISK_DECISIONS = Counter(
    "trading_execution_risk_decisions_total",
    "Final order-precheck decisions by intent kind, bounded result, and primary reason.",
    labelnames=("intent_kind", "result", "primary_reason"),
)

RISK_RESERVATION_TRANSITIONS = Counter(
    "trading_risk_reservation_transitions_total",
    "Risk-reservation bucket migrations by bounded transition.",
    labelnames=("transition",),
)

EXECUTION_FACT_RESULTS = Counter(
    "trading_execution_fact_results_total",
    "Reconciliation fact outcomes by target order-intent status and bounded result.",
    labelnames=("target_status", "result"),
)

CAPABILITY_CERTIFICATE_ISSUANCE = Counter(
    "trading_capability_certificate_issuance_total",
    "Shadow-only capability certificate issuance by type, environment, and result.",
    labelnames=("certificate_type", "environment", "result"),
)

CAPABILITY_CERTIFICATE_TRANSITIONS = Counter(
    "trading_capability_certificate_transitions_total",
    "Monotonic certificate tightening transitions and propagated authorization invalidations.",
    labelnames=("action", "target_status", "result"),
)

CAPABILITY_CERTIFICATE_VALIDATIONS = Counter(
    "trading_capability_certificate_validations_total",
    "Durable exact-scope capability certificate validation results.",
    labelnames=("result", "primary_reason"),
)

SENDER_LEASE_OPERATIONS = Counter(
    "trading_sender_lease_operations_total",
    "Shadow sender-lease authority operations by bounded operation and result.",
    labelnames=("operation", "result"),
)

SENDER_LEASE_VALIDATIONS = Counter(
    "trading_sender_lease_validations_total",
    "Exact-scope current fencing-token validation results.",
    labelnames=("result", "primary_reason"),
)

SHADOW_DISPATCH_CLAIMS = Counter(
    "trading_shadow_dispatch_claims_total",
    "Non-dispatchable shadow intent claims by bounded result.",
    labelnames=("result",),
)
