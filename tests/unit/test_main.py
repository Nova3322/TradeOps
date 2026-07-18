from types import SimpleNamespace

import pytest

import trading_control_plane.__main__ as entrypoint


def test_main_starts_configured_api_without_access_log(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    settings = SimpleNamespace(api_host="127.0.0.1", api_port=8123)

    monkeypatch.setattr(entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(
        entrypoint.uvicorn,
        "run",
        lambda app, **kwargs: calls.append((app, kwargs)),
    )

    entrypoint.main()

    assert calls == [
        (
            "trading_control_plane.api:create_app",
            {
                "factory": True,
                "host": "127.0.0.1",
                "port": 8123,
                "log_config": None,
                "access_log": False,
            },
        )
    ]
