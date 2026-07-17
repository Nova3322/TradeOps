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
