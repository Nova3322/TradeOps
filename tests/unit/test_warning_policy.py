from __future__ import annotations

import importlib
import tomllib
import warnings
from pathlib import Path

from starlette.exceptions import StarletteDeprecationWarning


def test_fastapi_testclient_deprecation_allowlist_is_exact() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = importlib.import_module("starlette.testclient")
        if not caught:
            importlib.reload(module)

    assert len(caught) == 1
    warning = caught[0]
    assert warning.category is StarletteDeprecationWarning
    assert str(warning.message) == (
        "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead."
    )
    config = tomllib.loads(Path("pyproject.toml").read_text())
    filters = config["tool"]["pytest"]["ini_options"]["filterwarnings"]
    assert filters[0] == "error"
    assert filters[1] == (
        "ignore:Using `httpx` with `starlette\\.testclient` is deprecated; "
        "install `httpx2` instead\\.:starlette.exceptions."
        "StarletteDeprecationWarning:fastapi\\.testclient"
    )
    assert filters[2:] == [
        'ignore:Module "zipline\\.assets" not found; multipliers will not be applied to '
        "position notionals\\.:UserWarning:pyfolio\\.pos",
        "ignore:The 'generic' unit for NumPy timedelta is deprecated, and will raise an error "
        "in the future\\. This includes implicit conversion of bare integers "
        "\\(e\\.g\\. `\\+ 1`\\)\\.Please use a specific unit instead\\.:"
        "DeprecationWarning:pyfolio\\.round_trips",
    ]
