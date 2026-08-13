from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "trading_control_plane"


def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def test_service_and_query_facades_use_composition_without_implicit_any() -> None:
    paths = [
        PACKAGE_ROOT / "service.py",
        PACKAGE_ROOT / "service_component.py",
        PACKAGE_ROOT / "service_transactions.py",
        PACKAGE_ROOT / "queries.py",
        PACKAGE_ROOT / "query_component.py",
        *sorted((PACKAGE_ROOT / "service_domains").glob("*.py")),
        *sorted((PACKAGE_ROOT / "query_domains").glob("*.py")),
    ]
    classes = {
        node.name: node
        for path in paths
        for node in parsed(path).body
        if isinstance(node, ast.ClassDef)
    }

    assert classes["TradingService"].bases == []
    assert classes["TradingQueries"].bases == []
    assert all(
        node.name != "__getattr__"
        for path in paths
        for node in ast.walk(parsed(path))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert not any("Mixin" in node.name for node in classes.values())


def test_lifecycle_service_components_remain_bounded() -> None:
    domain_root = PACKAGE_ROOT / "service_domains"
    expected = {
        "capital_automation.py",
        "capital_direct.py",
        "capital_notilt.py",
        "capital_reconciliation.py",
        "capital_transfer.py",
        "execution_campaign.py",
        "execution_facts.py",
        "execution_freqtrade.py",
        "execution_intent.py",
        "execution_venue.py",
        "risk_authorization.py",
        "risk_policy.py",
        "risk_reconciliation.py",
        "risk_recovery.py",
    }

    assert expected <= {path.name for path in domain_root.glob("*.py")}
    assert max(len(path.read_text().splitlines()) for path in domain_root.glob("*.py")) <= 2100


def test_api_dependencies_and_route_registrars_are_explicit_and_bounded() -> None:
    api_source = (PACKAGE_ROOT / "api.py").read_text()
    context_source = (PACKAGE_ROOT / "api_routes" / "context.py").read_text()
    assert "dict(locals())" not in api_source
    assert "dependencies: dict[str, Any]" not in context_source

    registrars = [
        node
        for path in (PACKAGE_ROOT / "api_routes").glob("*.py")
        for node in ast.walk(parsed(path))
        if isinstance(node, ast.FunctionDef) and "register_" in node.name
    ]
    assert registrars
    assert max(node.end_lineno - node.lineno + 1 for node in registrars) <= 500
