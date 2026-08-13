from __future__ import annotations

import ast
from pathlib import Path


def test_every_active_risk_policy_query_is_team_scoped() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "trading_control_plane"
    violations: list[str] = []
    active_queries = 0
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "where"
            ):
                continue
            predicates = " ".join(ast.unparse(item) for item in node.args)
            if "RiskPolicy.active" not in predicates:
                continue
            active_queries += 1
            if "RiskPolicy.team_id" not in predicates:
                violations.append(f"{path.relative_to(source_root)}:{node.lineno}")

    assert active_queries > 0
    assert violations == []
