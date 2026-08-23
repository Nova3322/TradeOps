from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src/trading_control_plane"
SERVICE_DOMAINS = PACKAGE / "service_domains"
QUERY_DOMAINS = PACKAGE / "query_domains"
TARGETS = tuple(
    path
    for path in (
        PACKAGE / "service.py",
        PACKAGE / "service_component.py",
        PACKAGE / "service_transactions.py",
        PACKAGE / "queries.py",
        PACKAGE / "query_component.py",
        *SERVICE_DOMAINS.glob("*.py"),
        *QUERY_DOMAINS.glob("*.py"),
    )
    if path.exists()
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _imports(path: Path) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_core_namespaces_are_explicit_and_type_suppressions_are_absent() -> None:
    for path in TARGETS:
        source = path.read_text()
        tree = _tree(path)
        assert "F403" not in source and "F405" not in source, path
        assert "attr-defined" not in source, path
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module
            in {
                "trading_control_plane.service_core",
                "trading_control_plane.query_core",
            }
            and any(alias.name == "*" for alias in node.names)
            for node in ast.walk(tree)
        ), path


def test_domains_do_not_import_api_adapters_or_transport_sdks() -> None:
    forbidden = {
        "ccxt",
        "ccxt.pro",
        "trading_control_plane.adapters",
        "trading_control_plane.api",
        "trading_control_plane.api_core",
        "trading_control_plane.api_routes",
        "trading_control_plane.freqtrade",
    }
    for path in (*SERVICE_DOMAINS.glob("*.py"), *QUERY_DOMAINS.glob("*.py")):
        imports = _imports(path)
        assert not any(
            imported == boundary or imported.startswith(f"{boundary}.")
            for imported in imports
            for boundary in forbidden
        ), path


def test_facades_have_no_dynamic_proxy_or_registry_framework() -> None:
    forbidden_definitions = {
        "__getattr__",
        "__getattribute__",
        "BaseManager",
        "CommonManager",
        "EventBus",
        "GenericRepository",
        "GenericService",
        "HandlerRegistry",
        "QueryBus",
        "ServiceLocator",
        "ServiceRegistry",
    }
    for path in TARGETS:
        definitions = {
            node.name
            for node in ast.walk(_tree(path))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert definitions.isdisjoint(forbidden_definitions), path
        assert "transactions._" not in path.read_text(), path


def test_facade_mro_has_no_duplicate_methods_or_base_count_regression() -> None:
    assert len(TradingService.__bases__) <= 21
    assert len(TradingQueries.__bases__) <= 7
    for facade in (TradingService, TradingQueries):
        providers: dict[str, list[str]] = defaultdict(list)
        for base in facade.__bases__:
            for name, value in vars(base).items():
                if callable(value):
                    providers[name].append(base.__name__)
        assert {name: owners for name, owners in providers.items() if len(owners) > 1} == {}


def test_domains_have_no_cross_domain_private_self_calls() -> None:
    for directory, component in (
        (SERVICE_DOMAINS, PACKAGE / "service_component.py"),
        (QUERY_DOMAINS, PACKAGE / "query_component.py"),
    ):
        paths = tuple(directory.glob("*.py"))
        provided_by: dict[str, set[Path]] = defaultdict(set)
        local: dict[Path, set[str]] = {}
        for path in paths:
            names = {
                node.name
                for node in ast.walk(_tree(path))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("_")
            }
            local[path] = names
            for name in names:
                provided_by[name].add(path)
        shared = {
            node.name
            for node in ast.walk(_tree(component))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for path in paths:
            calls = {
                node.func.attr
                for node in ast.walk(_tree(path))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr.startswith("_")
            }
            hidden = {name for name in calls - local[path] - shared if provided_by[name] - {path}}
            assert hidden == set(), (path, hidden)


def test_core_god_namespaces_are_retired() -> None:
    assert not (PACKAGE / "service_core.py").exists()
    assert not (PACKAGE / "query_core.py").exists()


def test_query_runtime_exposes_only_read_only_authorization() -> None:
    component = PACKAGE / "query_component.py"
    queries = PACKAGE / "queries.py"
    assert "trading_control_plane.service" not in _imports(component)
    assert "trading_control_plane.service" not in _imports(queries)
    runtime = next(
        node
        for node in _tree(component).body
        if isinstance(node, ast.ClassDef) and node.name == "QueryRuntime"
    )
    annotations = {
        node.target.id: ast.unparse(node.annotation)
        for node in runtime.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert annotations == {"database": "Database", "access_policy": "QueryAccessPolicy"}
    for path in QUERY_DOMAINS.glob("*.py"):
        source = path.read_text()
        assert "self.service" not in source, path


def test_internal_import_graph_has_no_cycle() -> None:
    modules = {
        "trading_control_plane." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts): path
        for path in PACKAGE.rglob("*.py")
        if path.name != "__init__.py"
    }
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module, path in modules.items():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                candidates = [node.module]
                candidates.extend(f"{node.module}.{alias.name}" for alias in node.names)
            else:
                continue
            graph[module].update(candidate for candidate in candidates if candidate in modules)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, path: tuple[str, ...]) -> None:
        if module in visiting:
            raise AssertionError(" -> ".join((*path, module)))
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency, (*path, module))
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        visit(module, ())
