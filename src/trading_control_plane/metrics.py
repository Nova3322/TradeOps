from prometheus_client import Counter, Gauge

DATABASE_READY = Gauge(
    "trading_database_ready",
    "1 when PostgreSQL and the mandatory disabled capability gates are ready.",
)

RISK_RESULTS = Counter(
    "trading_risk_results_total",
    "Deterministic risk results.",
    labelnames=("result",),
)

INTENT_TRANSITIONS = Counter(
    "trading_order_intent_transitions_total",
    "OrderIntent current-state transitions.",
    labelnames=("from_status", "to_status"),
)

RECONCILIATION_RESULTS = Counter(
    "trading_reconciliation_results_total",
    "Reconciliation results by current disposition.",
    labelnames=("status",),
)

FENCING_REJECTIONS = Counter(
    "trading_sender_fencing_rejections_total",
    "Rejected stale, expired, or superseded sender tokens.",
)

PROTECTION_ISSUES = Counter(
    "trading_protection_issues_total",
    "Positions recorded without complete protection coverage.",
)
