import asyncio
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings


class FakeDatabase:
    def __init__(self, ready: bool = True, error_code: str | None = None) -> None:
        self.ready = ready
        self.error_code = error_code
        self.disposed = False

    def is_ready(self) -> tuple[bool, str | None]:
        return self.ready, self.error_code

    def dispose(self) -> None:
        self.disposed = True


def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+psycopg://test:test@localhost/test",
        _env_file=None,
    )


async def async_get(app: FastAPI, path: str) -> Response:
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path)


def get(app: FastAPI, path: str) -> Response:
    return asyncio.run(async_get(app, path))


def test_liveness_does_not_claim_database_readiness() -> None:
    database = FakeDatabase(ready=False, error_code="DATABASE_UNAVAILABLE")

    response = get(create_app(settings(), database), "/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "live"
    assert database.disposed is True


def test_readiness_requires_durable_store_and_control_gates() -> None:
    response = get(create_app(settings(), FakeDatabase()), "/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "durable_store": "postgresql"}


def test_readiness_fails_closed_with_stable_error_code() -> None:
    database = FakeDatabase(ready=False, error_code="CONTROL_GATES_MISSING")

    response = get(create_app(settings(), database), "/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "not_ready",
        "error_code": "CONTROL_GATES_MISSING",
    }


def test_metrics_endpoint_exposes_control_plane_metrics() -> None:
    response = get(create_app(settings(), FakeDatabase()), "/metrics")

    assert response.status_code == 200
    assert "trading_database_ready" in response.text


def test_web_shell_is_served_without_claiming_business_readiness() -> None:
    app = create_app(settings(), FakeDatabase(ready=False))
    response = get(app, "/")

    assert response.status_code == 200
    assert "Trading Console" in response.text
    assert "/assets/app.js?v=12" in response.text
    assert 'id="mobile-nav-toggle"' in response.text
    assert 'id="confirm-dialog"' in response.text

    app_javascript = get(app, "/assets/app.js")
    assert app_javascript.status_code == 200
    assert "history.replaceState({}, '', loginDestination());" in app_javascript.text
    assert "const destination = `${location.pathname}${location.search}`;" in app_javascript.text
    assert "timeoutError.code = 'REQUEST_TIMEOUT'" in app_javascript.text
    assert "networkError.code = 'NETWORK_ERROR'" in app_javascript.text
    assert "const REQUEST_TIMEOUT_MS = 15000" in app_javascript.text
    assert "error.handled = response.status === 401" in app_javascript.text
    assert "function handleUnauthorizedResponse" in app_javascript.text
    assert "function confirmAction" in app_javascript.text

    stylesheet = get(app, "/assets/styles.css")
    assert stylesheet.status_code == 200
    assert ".sidebar[hidden] ~ .main-content" in stylesheet.text
    assert ".table-scroll-hint" in stylesheet.text

    service_worker = get(app, "/sw.js")
    assert service_worker.status_code == 200
    assert "await fetch(event.request)" in service_worker.text


def test_web_request_lifecycle_in_node() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const extract = (start, end) => {
          const from = source.indexOf(start);
          const to = source.indexOf(end, from);
          assert.notEqual(from, -1);
          assert.notEqual(to, -1);
          return source.slice(from, to);
        };
        const apiSource = extract("async function api", "\nfunction showToast");
        const pendingSource = extract(
          "async function withPending",
          "\nfunction formNumber",
        );
        const unauthorizedSource = extract(
          "function handleUnauthorizedResponse",
          "\nfunction showApiError",
        );

        const realSetTimeout = globalThis.setTimeout;
        let configuredDelay = 0;
        const timeoutContext = vm.createContext({
          AbortController,
          REQUEST_TIMEOUT_MS: 15000,
          clearTimeout,
          handleUnauthorizedResponse: () => false,
          setTimeout(callback, delay) {
            configuredDelay = delay;
            return realSetTimeout(callback, 5);
          },
          fetch: (_path, { signal }) =>
            new Promise((_resolve, reject) => {
              signal.addEventListener("abort", () => {
                const error = new Error("aborted");
                error.name = "AbortError";
                reject(error);
              }, { once: true });
            }),
        });
        vm.runInContext(apiSource, timeoutContext);
        await assert.rejects(
          timeoutContext.api("/mutation", { method: "POST" }),
          (error) => {
            assert.equal(error.code, "REQUEST_TIMEOUT");
            assert.equal(error.outcomeUnknown, true);
            assert.match(error.message, /按钮已恢复/);
            return true;
          },
        );
        assert.equal(configuredDelay, 15000);

        const networkContext = vm.createContext({
          AbortController,
          REQUEST_TIMEOUT_MS: 15000,
          clearTimeout,
          handleUnauthorizedResponse: () => false,
          setTimeout,
          fetch: async () => {
            throw new TypeError("offline");
          },
        });
        vm.runInContext(apiSource, networkContext);
        await assert.rejects(
          networkContext.api("/mutation", { method: "POST" }),
          (error) => {
            assert.equal(error.code, "NETWORK_ERROR");
            assert.equal(error.outcomeUnknown, true);
            assert.match(error.message, /按钮已恢复/);
            return true;
          },
        );

        let unauthorizedCalls = 0;
        const response401Context = vm.createContext({
          AbortController,
          REQUEST_TIMEOUT_MS: 15000,
          clearTimeout,
          handleUnauthorizedResponse() {
            unauthorizedCalls += 1;
            return true;
          },
          setTimeout,
          fetch: async () => ({
            status: 401,
            ok: false,
            json: async () => ({
              error: { code: "SESSION_EXPIRED", message: "expired" },
            }),
          }),
        });
        vm.runInContext(apiSource, response401Context);
        await assert.rejects(
          response401Context.api("/mutation", { method: "POST" }),
          (error) => {
            assert.equal(error.status, 401);
            assert.equal(error.handled, true);
            return true;
          },
        );
        assert.equal(unauthorizedCalls, 1);

        const lifecycle = {
          enhance: 0,
          login: 0,
          shell: 0,
          clearedTimer: null,
          removedToastClasses: [],
        };
        const toastAttributes = new Map([
          ["role", "alert"],
          ["aria-live", "assertive"],
        ]);
        const unauthorizedContext = vm.createContext({
          authFailureActive: false,
          clearTimeout(timer) { lifecycle.clearedTimer = timer; },
          confirmDialog: { open: false, close() {} },
          dialog: { open: false, close() {} },
          enhanceRenderedPage() { lifecycle.enhance += 1; },
          renderLogin() { lifecycle.login += 1; },
          session: { username: "operator" },
          sessionNotice: "",
          setShell() { lifecycle.shell += 1; },
          toast: {
            classList: {
              remove(...names) { lifecycle.removedToastClasses = names; },
            },
            setAttribute(name, value) { toastAttributes.set(name, value); },
            textContent: "stale request timeout",
          },
          toastTimer: 41,
        });
        vm.runInContext(unauthorizedSource, unauthorizedContext);
        assert.equal(unauthorizedContext.handleUnauthorizedResponse(), true);
        assert.equal(unauthorizedContext.handleUnauthorizedResponse(), true);
        assert.equal(unauthorizedContext.session, null);
        assert.equal(unauthorizedContext.toastTimer, null);
        assert.equal(unauthorizedContext.toast.textContent, "");
        assert.equal(toastAttributes.get("role"), "status");
        assert.equal(toastAttributes.get("aria-live"), "polite");
        assert.deepEqual(lifecycle, {
          enhance: 1,
          login: 1,
          shell: 1,
          clearedTimer: 41,
          removedToastClasses: ["show", "error"],
        });

        const pendingContext = vm.createContext({});
        vm.runInContext(pendingSource, pendingContext);
        const attributes = new Map();
        const button = {
          dataset: {},
          disabled: false,
          isConnected: false,
          textContent: "刷新 PnL",
          removeAttribute(name) { attributes.delete(name); },
          setAttribute(name, value) { attributes.set(name, value); },
        };
        let rejectAction;
        let actionCalls = 0;
        const first = pendingContext.withPending(
          button,
          "刷新中…",
          () => {
            actionCalls += 1;
            return new Promise((_resolve, reject) => { rejectAction = reject; });
          },
        );
        const duplicate = pendingContext.withPending(
          button,
          "刷新中…",
          () => { actionCalls += 1; },
        );
        assert.equal(await duplicate, undefined);
        assert.equal(actionCalls, 1);
        assert.equal(button.disabled, true);
        assert.equal(button.dataset.pending, "true");
        assert.equal(attributes.get("aria-busy"), "true");
        rejectAction(new Error("timeout"));
        await assert.rejects(first, /timeout/);
        assert.equal(button.disabled, false);
        assert.equal(button.dataset.pending, undefined);
        assert.equal(attributes.has("aria-busy"), false);
        assert.equal(button.textContent, "刷新 PnL");

        console.log(JSON.stringify({
          configuredDelay,
          duplicateCalls: actionCalls,
          lifecycle: {
            enhance: lifecycle.enhance,
            login: lifecycle.login,
            shell: lifecycle.shell,
          },
          pendingRestored: !button.disabled,
        }));
        """
    )

    result = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "configuredDelay": 15000,
        "duplicateCalls": 1,
        "lifecycle": {"enhance": 1, "login": 1, "shell": 1},
        "pendingRestored": True,
    }


def test_mock_login_is_not_available_unless_explicitly_enabled() -> None:
    async def post() -> Response:
        app = create_app(settings(), FakeDatabase())
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post("/api/auth/mock/login", json={"username": "admin"})

    response = asyncio.run(post())

    assert response.status_code == 404
