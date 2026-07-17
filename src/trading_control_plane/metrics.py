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
