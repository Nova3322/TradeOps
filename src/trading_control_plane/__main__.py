from __future__ import annotations

import uvicorn

from trading_control_plane.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "trading_control_plane.api:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
