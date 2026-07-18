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

EXECUTION_FACT_BINDINGS = Counter(
    "trading_execution_fact_bindings_total",
    "Reconciliation-bound execution facts by kind, exact source, and bounded result.",
    labelnames=("fact_kind", "source_type", "result"),
)

EXECUTION_FACT_AUTHORITY_MODES = Counter(
    "trading_execution_fact_authority_modes_total",
    "Applied execution facts by original-lease or successor-lease reconciliation authority.",
    labelnames=("authority_mode", "result"),
)

EXECUTION_CANONICAL_FACT_BINDINGS = Counter(
    "trading_execution_canonical_fact_bindings_total",
    "Canonical venue facts bound to execution transitions by type and bounded result.",
    labelnames=("fact_type", "result"),
)

CAMPAIGN_ECONOMIC_BASELINES = Counter(
    "trading_campaign_economic_baselines_total",
    "Immutable Campaign economic-baseline outcomes by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_FILL_ECONOMIC_ENTRIES = Counter(
    "trading_campaign_fill_economic_entries_total",
    "Immutable Campaign fill-economic-entry outcomes by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_OPENING_FILL_PROJECTIONS = Counter(
    "trading_campaign_opening_fill_projections_total",
    "Rebuildable Campaign opening-fill projection outcomes by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_CURRENT_POSITION_BINDINGS = Counter(
    "trading_campaign_current_position_bindings_total",
    "Opening-only Campaign current-position binding outcomes by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS = Counter(
    "trading_campaign_funding_coverage_projections_total",
    "Reconciled Campaign funding scope/interval coverage outcomes by bounded result.",
    labelnames=("result",),
)

TARGET_POSITION_ARBITRATIONS = Counter(
    "trading_target_position_arbitrations_total",
    "Pure Campaign target-position arbitration outcomes by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_TARGET_POSITION_EVALUATIONS = Counter(
    "trading_campaign_target_position_evaluations_total",
    "Server-bound Campaign target-position evaluations by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_PROTECTION_EXIT_EVALUATIONS = Counter(
    "trading_campaign_protection_exit_evaluations_total",
    "Canonical Campaign protection-health exit evaluations by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_TARGET_FACT_RECORDINGS = Counter(
    "trading_campaign_target_fact_recordings_total",
    "Durable Campaign-owned target-position fact outcomes by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_REDUCTION_PLAN_EVALUATIONS = Counter(
    "trading_campaign_reduction_plan_evaluations_total",
    "Read-only Campaign reduction execution-plan outcomes by bounded result.",
    labelnames=("result",),
)

CAMPAIGN_REDUCTION_PLAN_PREPARATIONS = Counter(
    "trading_campaign_reduction_plan_preparations_total",
    "Immutable non-dispatchable Campaign reduction-plan preparations by bounded result.",
    labelnames=("result",),
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

RECONCILIATION_RUN_TRANSITIONS = Counter(
    "trading_reconciliation_run_transitions_total",
    "Durable execution-reconciliation transitions by bounded status and phase.",
    labelnames=("from_status", "to_status", "phase"),
)

RECONCILIATION_INPUTS = Counter(
    "trading_reconciliation_inputs_total",
    "Immutable reconciliation input snapshots by required source and collection result.",
    labelnames=("source_type", "collection_status"),
)

RECONCILIATION_FINDINGS = Counter(
    "trading_reconciliation_findings_total",
    "Immutable reconciliation finding events by severity and lifecycle fact.",
    labelnames=("severity", "disposition"),
)

VENUE_FACT_NORMALIZATIONS = Counter(
    "trading_venue_fact_normalizations_total",
    "Canonical private-venue facts normalized by bounded type and result.",
    labelnames=("fact_type", "result"),
)

VENUE_FACT_INPUT_LINKS = Counter(
    "trading_venue_fact_input_links_total",
    "Immutable reconciliation-input memberships by source and result.",
    labelnames=("source_type", "result"),
)

VENUE_CURRENT_PROJECTION_QUERIES = Counter(
    "trading_venue_current_projection_queries_total",
    "Read-only current venue projection queries by bounded projection, state, and freshness.",
    labelnames=("projection_type", "projection_state", "freshness"),
)

VENUE_CURRENT_PROJECTION_AGE = Histogram(
    "trading_venue_current_projection_age_seconds",
    "Age of current venue projection facts when a source row exists.",
    labelnames=("projection_type",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, 900, 3600),
)

CAPITAL_SCOPE_MANIFEST_REGISTRATIONS = Counter(
    "trading_capital_scope_manifest_registrations_total",
    "Immutable managed-capital account-universe manifests by bounded result.",
    labelnames=("result",),
)

PORTFOLIO_MTM_PROJECTION_QUERIES = Counter(
    "trading_portfolio_mtm_projection_queries_total",
    "Managed-scope portfolio MTM queries by bounded state and primary reason.",
    labelnames=("projection_state", "primary_reason"),
)

INSTRUMENT_CATALOG_REGISTRATIONS = Counter(
    "trading_instrument_catalog_registrations_total",
    "Immutable SHADOW-only instrument catalog registrations by bounded result.",
    labelnames=("result",),
)

INSTRUMENT_CATALOG_VALIDATIONS = Counter(
    "trading_instrument_catalog_validations_total",
    "Exact durable instrument classification validations by result and primary reason.",
    labelnames=("result", "primary_reason"),
)

PROTECTION_CAPABILITY_REGISTRATIONS = Counter(
    "trading_protection_capability_registrations_total",
    "Immutable SHADOW-only protection capability registrations by bounded result.",
    labelnames=("result",),
)

PROTECTION_CAPABILITY_VALIDATIONS = Counter(
    "trading_protection_capability_validations_total",
    "Exact durable native protection capability validations by result and primary reason.",
    labelnames=("result", "primary_reason"),
)

RISK_FACT_SET_REGISTRATIONS = Counter(
    "trading_risk_fact_set_registrations_total",
    "Immutable SHADOW-only complete risk fact-set registrations by bounded result.",
    labelnames=("result",),
)

RISK_FACT_SET_VALIDATIONS = Counter(
    "trading_risk_fact_set_validations_total",
    "Exact-scope durable risk fact-set validations by result and primary reason.",
    labelnames=("result", "primary_reason"),
)

STRATEGY_EVALUATION_REGISTRATIONS = Counter(
    "trading_strategy_evaluation_registrations_total",
    "Immutable SHADOW-only strategy evaluation registrations by bounded result.",
    labelnames=("result",),
)

STRATEGY_EVALUATION_VALIDATIONS = Counter(
    "trading_strategy_evaluation_validations_total",
    "Exact Campaign strategy evaluation validations by result and primary reason.",
    labelnames=("result", "primary_reason"),
)
